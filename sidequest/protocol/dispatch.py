"""DispatchPackage — the Local DM decomposer's structured output.

Spec: docs/superpowers/specs/2026-04-23-local-dm-decomposer-design.md §5

The decomposer reads (action, state, submissions) and emits a DispatchPackage
per turn. Downstream consumers:
  - Subsystem bank — executes SubsystemDispatch entries, feeds back to state
  - Narrator prompt builder — injects NarratorDirective entries into <game_state>
  - Group G (future) — reads VisibilityTag via Perception Rewriter + ProjectionFilter

Group B emits stub values for LethalityVerdict (Group C fills in) and
VisibilityTag (Group G wires the consumer pipeline).

No tool-calling. No prose. Structured JSON only — spec §3.2.

All models inherit `ProtocolBase`:
  - `extra="forbid"` — unknown fields from LLM output raise ValidationError
    (routes to the degraded path rather than silently dropping hallucinated keys)
  - `populate_by_name=True` — accepts Python names and wire aliases
  - Serializer drops None / empty-matching-default containers for wire compactness
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import Field, model_validator

from sidequest.protocol.base import ProtocolBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

PerceptionFidelity = Literal[
    "full",
    "audio_only",
    "audio_only_muffled",
    "visual_only",
    "periphery_only",
    "inferred_from_aftermath",
]


class VisibilityTag(ProtocolBase):
    """Authoritative ground-truth visibility for a dispatch/directive/verdict.

    Consumed by ADR-028 Perception Rewriter and Plan 03 ProjectionFilter.
    Group B always emits `visible_to="all"` with empty fidelity; Group G
    fills in asymmetric values.
    """

    visible_to: list[str] | Literal["all"] = Field(
        description="Recipients; 'all' is a conscious choice, not a fallback."
    )
    perception_fidelity: dict[str, PerceptionFidelity] = Field(default_factory=dict)
    secrets_for: list[str] = Field(default_factory=list)
    redact_from_narrator_canonical: bool = False


# ---------------------------------------------------------------------------
# Referent resolution
# ---------------------------------------------------------------------------


class Referent(ProtocolBase):
    token: str = Field(
        description="The surface token from raw_action, e.g. 'him', 'let's', 'that'."
    )
    # Pingpong 2026-04-26 S2-OBS: the decomposer LLM occasionally emits a
    # ``list[str]`` of player IDs when a token like "the party" resolves to
    # multiple PCs (e.g. ``resolved_to=['Paul','John','George','Ringo']``).
    # Schema accepts either form so multi-target turns survive validation.
    resolved_to: str | list[str] | None = Field(
        default=None,
        description="Entity id, list of entity ids (multi-target), or None for absence.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    alternatives: list[str] = Field(default_factory=list)
    resolution_note: str | None = None


# ---------------------------------------------------------------------------
# Subsystem dispatch
# ---------------------------------------------------------------------------


class SubsystemDispatch(ProtocolBase):
    subsystem: str = Field(description="Subsystem name — must be registered at runtime.")
    params: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(
        default_factory=list,
        description="List of sibling idempotency_keys this dispatch depends on.",
    )
    idempotency_key: str
    visibility: VisibilityTag
    # ADR-113 confidence gate (Story 71-16). Required — no silent default: the
    # Intent Router scores how certain it is that THIS specific mechanical
    # engagement is what the player intended. ``run_dispatch_bank`` engages the
    # subsystem engine only when ``confidence >= threshold`` (per-subsystem,
    # default 0.6); below threshold the dispatch degrades to a narrator hint
    # rather than firing the engine. A defaulted score would let a router bug
    # silently engage or gate an engine on a fabricated value.
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Per-dispatch engagement confidence (0.0-1.0) from the Intent Router. "
            "Gated against the per-subsystem threshold in run_dispatch_bank."
        ),
    )


# ---------------------------------------------------------------------------
# Narrator directives
# ---------------------------------------------------------------------------


NarratorDirectiveKind = Literal[
    "must_narrate",
    "must_not_narrate",
    "distinctive_detail_for_referent",
    "canonical_only_do_not_reveal_to_others",
]


class NarratorDirective(ProtocolBase):
    kind: NarratorDirectiveKind
    payload: str
    visibility: VisibilityTag


# ---------------------------------------------------------------------------
# Lethality — full contract, stub values in Group B
# ---------------------------------------------------------------------------


LethalityVerdictKind = Literal[
    "dead",
    "dying",
    "maimed",
    "defeated",
    "captured",
    "humiliated",
    "unscathed",
]

Reversibility = Literal["permanent", "reversible_with_cost", "narrative_only"]


class LethalityVerdict(ProtocolBase):
    entity: str
    verdict: LethalityVerdictKind
    cause: str
    reversibility: Reversibility
    narrator_directive: str
    soul_md_constraint: str
    witness_scope: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-player dispatch
# ---------------------------------------------------------------------------


class PlayerDispatch(ProtocolBase):
    player_id: str
    raw_action: str
    resolved: list[Referent] = Field(default_factory=list)
    dispatch: list[SubsystemDispatch] = Field(default_factory=list)
    lethality: list[LethalityVerdict] = Field(default_factory=list)
    narrator_instructions: list[NarratorDirective] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Cross-player (Group G extends; Group B leaves empty)
# ---------------------------------------------------------------------------


class CrossAction(ProtocolBase):
    participants: list[str]
    witnesses: list[str]
    dispatch: list[SubsystemDispatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _witnesses_include_participants(self) -> CrossAction:
        """Normalize: every participant in a cross-player interaction also
        witnesses it. Union missing participants into ``witnesses`` rather
        than rejecting.

        Previously this REJECTED when ``participants ⊄ witnesses``, which
        sank the entire ``DispatchPackage`` on every shared-target MP turn
        (playtest 2026-05-27, coyote_star turns 3/4/5: both PCs engage the
        same NPC → the router emits ``participants=[acting_pc, npc]`` with
        ``witnesses`` omitting the other PC → validation error → the Intent
        Router retries, fails again, and degrades to ``dispatch_package=None``
        — the whole mechanical spine goes dark while narration still reads
        fine, the classic Illusionism the OTEL panel exists to catch).

        Auto-unioning is semantically correct (you cannot hide an interaction
        from someone who is in it) and cannot breach the ADR-104/105
        perception firewall — it only ever ADDS a participant to the set of
        those who perceive their own interaction, never removes a witness.
        Participant order is preserved; ``witnesses`` already-present entries
        are not duplicated.
        """
        missing = [p for p in self.participants if p not in self.witnesses]
        if missing:
            self.witnesses = [*self.witnesses, *missing]
        return self


# ---------------------------------------------------------------------------
# Top-level package
# ---------------------------------------------------------------------------


class DispatchPackage(ProtocolBase):
    turn_id: str
    per_player: list[PlayerDispatch] = Field(default_factory=list)
    cross_player: list[CrossAction] = Field(default_factory=list)
    confidence_global: float = Field(ge=0.0, le=1.0)

    # Note: the historical ``degraded`` / ``degraded_reason`` fields and the
    # ``_degraded_requires_reason`` validator were removed by Story 59-2
    # per ADR-113 and memory rule ``feedback_no_fallbacks_hard``. The
    # Intent Router producer raises ``IntentRouterFailure`` on retry-fail
    # instead of returning a degraded shape; downstream consumers no longer
    # branch on a "degraded" flag.

    @model_validator(mode="before")
    @classmethod
    def _coerce_stringified_lists(cls, data: Any) -> Any:
        """Coerce JSON-encoded-string list fields back into lists.

        Known Haiku tool-use failure mode (sq-playtest 2026-06-07, 5×
        ``schema_invalid`` across spaghetti_western + heavy_metal, one fully
        crunch-dropped turn): the model emits ``per_player`` /
        ``cross_player`` as a JSON-*encoded string* (``'[{"player_id": ...'``)
        instead of a list. The content is well-formed — only the encoding is
        wrong — so rejecting it costs a retry (and on a double miss, the
        whole turn's mechanical spine). Parse the string; if it yields a
        list, take it (same normalize-don't-reject doctrine as
        ``CrossAction._witnesses_include_participants``). Anything else —
        unparseable, or parses to a non-list — is left as-is for pydantic to
        reject loudly.
        """
        if not isinstance(data, dict):
            return data
        for field in ("per_player", "cross_player"):
            value = data.get(field)
            if not isinstance(value, str):
                continue
            try:
                parsed = json.loads(value)
            except ValueError:
                continue  # let pydantic reject the string loudly
            if isinstance(parsed, list):
                logger.info(
                    "dispatch_package.coerced_stringified_list field=%s items=%d",
                    field,
                    len(parsed),
                )
                data[field] = parsed
        return data

    @model_validator(mode="after")
    def _unique_idempotency_keys(self) -> DispatchPackage:
        """Idempotency keys must be unique across per_player AND cross_player dispatches.

        The subsystem bank uses these keys as a single per-turn namespace, so
        the uniqueness constraint spans both fields.
        """
        seen: set[str] = set()
        for pd in self.per_player:
            for d in pd.dispatch:
                if d.idempotency_key in seen:
                    raise ValueError(f"duplicate idempotency_key: {d.idempotency_key}")
                seen.add(d.idempotency_key)
        for ca in self.cross_player:
            for d in ca.dispatch:
                if d.idempotency_key in seen:
                    raise ValueError(f"duplicate idempotency_key: {d.idempotency_key}")
                seen.add(d.idempotency_key)
        return self


__all__ = [
    "CrossAction",
    "DispatchPackage",
    "LethalityVerdict",
    "LethalityVerdictKind",
    "NarratorDirective",
    "NarratorDirectiveKind",
    "PerceptionFidelity",
    "PlayerDispatch",
    "Referent",
    "Reversibility",
    "SubsystemDispatch",
    "VisibilityTag",
]
