"""Post-narration Haiku sidecar extractor — ADR-150 step 2 (Story 151-2).

The foundation of the sidecar-accounting epic. A post-narration Haiku
``emit_tool`` pass (AsideResolver / IntentRouter-shaped) reads the narrator's
emitted prose and derives the EXTRACTIVE *bucket-B* sidecar fields (nine, after the
RENDER-NO-SUBJECT amendment carved the two generative fields — visual_scene,
footnotes — back out to narrator-owned). It ships in SHADOW mode: it computes the
fields and emits OTEL, but applies nothing — the lie-detector watches from day one
before any field cuts over (151-4 / 151-5).

Two layers mirror the live ADR-113 lineage:

* :class:`SidecarExtractor` — the core pass (the analogue of
  ``IntentRouter.decompose``): a single forced-``emit_tool`` Haiku call
  (ADR-102, structured input back, no JSON parsing), one bounded retry, and an
  explicit :class:`SidecarExtractionFailure` raise on persistent failure
  (No Silent Fallbacks). Emits ``sidecar_extraction.run`` + per-field spans.
* :func:`run_sidecar_extraction_watcher` — the shadow runner (the analogue of
  ``run_dispatch_engagement_watcher``): a NON-FATAL post-narration wrapper that
  runs the core pass, emits the ``sidecar_extraction.mismatch`` lie-detector
  span, and never raises into the WS turn pipeline (the per-field catch-loops
  remain the loud net).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, Field, ValidationError, WithJsonSchema

from sidequest.agents.model_routing import CallType, resolve_model
from sidequest.telemetry.spans.sidecar_extraction import (
    sidecar_extraction_failed_span,
    sidecar_extraction_field_span,
    sidecar_extraction_mismatch_span,
    sidecar_extraction_run_span,
    sidecar_extraction_watcher_crashed_span,
)

logger = logging.getLogger(__name__)

# The nine EXTRACTIVE bucket-B prose-readout fields (ADR-150 §Decision + the
# 2026-06-20 RENDER-NO-SUBJECT amendment, which excludes the two generative fields
# visual_scene/footnotes — those are narrator-owned). Verified against
# NarrationTurnResult field names (orchestrator.py:474-587); ``scene_mood`` is
# the field the ADR refers to as "mood". This tuple is the SINGLE source of
# truth the 151-4 / 151-5 cutover stories reference — do not drift a parallel
# string list.
BUCKET_B_FIELDS: tuple[str, ...] = (
    "items_gained",
    "items_lost",
    "items_discarded",
    "items_consumed",
    "gold_change",
    "companions_added",
    "companions_dismissed",
    "npcs_present",
    "scene_mood",
    # visual_scene + footnotes are GENERATIVE/authorial narrator-owned fields
    # (ADR-150 amendment 2026-06-20, RENDER-NO-SUBJECT) — a never-invent reader
    # cannot produce a render subject or a knowledge-feed entry, so they are NOT
    # read here. bucket-B is extractive-only.
)

# One attempt plus one bounded retry (ADR-150 no-fallbacks discipline, mirroring
# the IntentRouter producer).
_MAX_TOTAL_ATTEMPTS = 2
_RAW_PREVIEW_LIMIT = 200

# Bound the player-influenced prose before the per-turn live SDK call so a
# runaway-long narration cannot grind unbounded input-token cost (CWE-400,
# python.md #11). The bucket-B signal (items, NPCs, mood) is in the opening
# prose, not the ten-thousandth char. Mirrors
# post_narration_classifier._MAX_NARRATION_CHARS; truncation is logged LOUD,
# never silent (No Silent Fallbacks).
_MAX_NARRATION_CHARS = 4_000

_TOOL_NAME = "emit_sidecar_fields"
_TOOL_DESCRIPTION = (
    "Emit the structured bucket-B sidecar fields you READ from the narration "
    "prose: items gained/lost/discarded/consumed, gold change, companions "
    "added/dismissed, NPCs present, and scene mood. "
    "When the prose establishes a gained item as SIGNIFICANT (magical, "
    "story-important, or character-defining), set that item's optional "
    "grants_aspect to a short invokable aspect phrase capturing why it matters; "
    "leave it unset for ordinary items (a hat is a hat). "
    "For NPCs present, list only PEOPLE and CREATURES — if a proper noun names a "
    "PLACE or location (a hold, cavern, town, region), set that entry's is_place "
    "to true so it is not mistaken for a person. "
    "For each NPC present, classify their stance toward the player characters in "
    "this narration as that entry's role: 'hostile' (attacks, threatens, or "
    "opposes the party), 'friendly' (aids or sides with it), 'bystander' "
    "(present but uninvolved), or 'neutral' (no stance stated). "
    "Report only what the prose states; never invent. An empty field is correct "
    "when the prose says nothing about it."
)

# Story 126-35: the JSON schema the extractor LLM sees for ONE items_gained entry.
# Surfaced via WithJsonSchema so the narrator-contract DOCUMENTS the optional
# grants_aspect lever (Keith design call 2026-06-20, PATH c — narrator-authored
# promotion) while the runtime type stays a free-form ``dict`` (the merge +
# apply-path consumer keep reading entries as dicts; No half-wired features).
# ``additionalProperties`` stays open: ``_narrator_item_dict`` mints id/category/
# etc. downstream, so the reader only needs name (+ the optional significance flag).
_ITEMS_GAINED_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The gained item's name, as the prose states it.",
        },
        "grants_aspect": {
            "type": "string",
            "description": (
                "OPTIONAL. Set ONLY when the narration establishes this item as a "
                "SIGNIFICANT find — magical, story-important, or character-defining "
                "(the drifter's surveyor's map, a dead witch's silver shoes). The "
                "value is a short invokable Fate aspect phrase naming why it matters "
                "(e.g. 'Knows the Hidden Trails'). Leave UNSET for ordinary items — "
                "do NOT mark every item; most gear is pure flavor."
            ),
        },
    },
    "additionalProperties": True,
}

# Story 158-4: the JSON schema the extractor LLM sees for ONE npcs_present entry.
# Surfaced via WithJsonSchema so the reader is TOLD to flag a proper noun that
# names a PLACE/LOCATION (a hold, cavern, town, region) with is_place=true rather
# than listing it as a person — closing the beneath_sunden place-name leak where
# narrator-invented "Torchdeep"/"Torchhold" registered as phantom NPCs (disp=0,
# creature_id=None). ``additionalProperties`` stays OPEN: ``NpcMention.from_value``
# still reads name/pronouns/appearance/is_new/is_creature free-form, so this
# only ADDS the documented discriminators; it does not constrain the other fields.
# ``side`` is deliberately undocumented — it is ENGINE-owned
# (merge_sidecar_extraction_npcs_present), not a thing the reader should claim.
#
# ADR-156 §6 (task-5 review fix): ``role`` is documented as a stance enum so the
# attach-before-mint hostile gate (`narration_apply._mention_is_hostile`, which
# reads ``mention.role in ("hostile", "enemy", "opponent")``) receives a REAL
# signal on real traffic. Pre-fix, role was read free-form by ``from_value`` but
# never prompted — the extractor structurally never emitted it, so the
# seated-Other attach leg could not fire outside hand-built tests. Distinct from
# ``side``: side is the engine's ADJUDICATED seat membership (exact-name match
# against already-seated actors — a NEW prose epithet always resolves "neutral");
# role is the reader's PROSE-stance classification, which is exactly the signal
# a first-mention hostile epithet carries.
_NPCS_PRESENT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The NPC's name, as the prose states it.",
        },
        "role": {
            "type": "string",
            "enum": ["hostile", "friendly", "bystander", "neutral"],
            "description": (
                "This NPC's stance toward the player characters IN THIS "
                "narration: 'hostile' when they attack, threaten, or actively "
                "oppose the party; 'friendly' when they aid or side with it; "
                "'bystander' for a present-but-uninvolved figure; 'neutral' "
                "when the prose gives no stance signal. Classify from what the "
                "prose STATES this turn — do not infer beyond it."
            ),
        },
        "is_place": {
            "type": "boolean",
            "description": (
                "Set TRUE when this proper noun names a PLACE or LOCATION (a hold, "
                "cavern, town, region — e.g. 'Torchdeep', 'Torchhold') rather than a "
                "person or creature. A place is NOT an NPC: flag it so the engine "
                "keeps it out of the roster. Leave UNSET/false for any person or "
                "creature — most named entities are people, so do NOT over-flag."
            ),
        },
    },
    "additionalProperties": True,
}
_SYSTEM_PROMPT = (
    "You are a post-narration READER at a tabletop session. The narrator already "
    "wrote the prose; your only job is to extract the structured bucket-B sidecar "
    "fields the prose states, as a single tool call. You do not narrate and you "
    "do not invent — an empty field is the correct answer when the prose is "
    "silent about it."
)


class SidecarExtraction(BaseModel):
    """The nine extractive bucket-B fields as validated structured output (ADR-102).

    Shadow-mode skeleton: the list fields stay raw ``dict`` payloads (the shape
    ``emit_tool`` returns) because no field is applied this story — the 151-4 /
    151-5 cutover stories type them as they migrate into ``narration_apply``.
    """

    # items_gained entries stay runtime dicts (the consumer + merge read .get(...)),
    # but the EMITTED tool-schema documents the optional grants_aspect lever so the
    # narrator/reader is told it may mark a significant item (story 126-35, AC1).
    items_gained: list[Annotated[dict[str, Any], WithJsonSchema(_ITEMS_GAINED_ITEM_SCHEMA)]] = (
        Field(default_factory=list)
    )
    items_lost: list[dict[str, Any]] = Field(default_factory=list)
    items_discarded: list[dict[str, Any]] = Field(default_factory=list)
    items_consumed: list[dict[str, Any]] = Field(default_factory=list)
    gold_change: int | None = None
    companions_added: list[dict[str, Any]] = Field(default_factory=list)
    companions_dismissed: list[str] = Field(default_factory=list)
    # npcs_present entries stay runtime dicts (from_value reads .get(...)), but the
    # EMITTED tool-schema documents the optional is_place discriminator so the reader
    # is told to flag a LOCATION proper-noun out of the roster (story 158-4).
    npcs_present: list[Annotated[dict[str, Any], WithJsonSchema(_NPCS_PRESENT_ITEM_SCHEMA)]] = (
        Field(default_factory=list)
    )
    scene_mood: str | None = None
    # visual_scene + footnotes deliberately absent — generative/authorial fields the
    # narrator owns (ADR-150 amendment 2026-06-20, RENDER-NO-SUBJECT). The forced
    # tool schema derives from this model, so dropping them stops the reader being
    # asked to invent them.


class SidecarExtractionFailure(Exception):
    """The extractor failed after the bounded retry (ADR-150 No Silent Fallbacks).

    Raised explicitly so the shadow runner surfaces a loud GM-panel error rather
    than degrading silently to a narrator-only continuation. Carries the
    human-readable reason of the last failed attempt.
    """


class SidecarExtractorLLM(Protocol):
    """Single-shot forced-tool-use LLM contract (ADR-102), the same shape as
    ``IntentRouterLLM``: the adapter forces one tool call and returns the
    ``tool_use`` block's already-structured ``input`` dict — there is no
    free-text JSON to parse. Tests inject an ``AsyncMock`` returning a dict;
    ``llm_factory.build_sidecar_extractor_llm`` is the live implementation.
    """

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


def _extraction_tool_schema() -> dict[str, Any]:
    """The forced tool's input_schema IS the bucket-B field shape, so Haiku
    returns structured input instead of fenced JSON (ADR-102)."""
    return SidecarExtraction.model_json_schema()


def _build_user_prompt(narration: str) -> str:
    return f"NARRATION:\n{narration}\n\nEmit the bucket-B sidecar fields the prose states."


def _is_emitted(value: Any) -> bool:
    """A field is 'emitted' when it carries content: a non-None scalar, or a
    non-empty collection/string. Distinguishes 'extractor looked, found nothing'
    (``emitted=False``) from 'extractor never ran'."""
    if value is None:
        return False
    if isinstance(value, (list, tuple, str, dict)):
        return len(value) > 0
    return True


class SidecarExtractor:
    """Core post-narration extraction pass (ADR-150 step 2)."""

    def __init__(self, *, llm: SidecarExtractorLLM) -> None:
        self._llm = llm

    async def extract(self, *, narration: str, snapshot: Any) -> SidecarExtraction:
        """Read the prose into the bucket-B fields via one forced ``emit_tool`` call.

        Raises :class:`SidecarExtractionFailure` if the initial attempt and the
        bounded retry both fail (timeout / transport / schema-invalid). There is
        no degraded-shape return — failure surfaces as an exception (ADR-150 No
        Silent Fallbacks). ``snapshot`` is read-only and is reserved for future
        grounding; the shadow skeleton derives fields from prose alone and
        deliberately mutates nothing.
        """
        if len(narration) > _MAX_NARRATION_CHARS:
            logger.warning(
                "sidecar_extraction narration truncated from %d to %d chars",
                len(narration),
                _MAX_NARRATION_CHARS,
            )
            narration = narration[:_MAX_NARRATION_CHARS]
        tool_schema = _extraction_tool_schema()
        user_prompt = _build_user_prompt(narration)
        start_ns = time.perf_counter_ns()
        last_failure: tuple[str, str] | None = None
        for attempt_index in range(_MAX_TOTAL_ATTEMPTS):
            retry_count = attempt_index  # 0 on first try, 1 on retry.
            try:
                tool_input = await self._llm.emit_tool(
                    system=_SYSTEM_PROMPT,
                    user=user_prompt,
                    tool_name=_TOOL_NAME,
                    tool_description=_TOOL_DESCRIPTION,
                    tool_schema=tool_schema,
                )
            except TimeoutError as exc:
                last_failure = ("timeout", str(exc))
                self._emit_failed(reason="timeout", preview=str(exc), retry_count=retry_count)
                continue
            except Exception as exc:  # noqa: BLE001 — transport boundary (ADR-113 §5 parity)
                last_failure = ("transport", str(exc))
                self._emit_failed(reason="transport", preview=str(exc), retry_count=retry_count)
                continue

            try:
                extraction = SidecarExtraction.model_validate(tool_input)
            except ValidationError as exc:
                # Keep the full pydantic field detail in last_failure so the raised
                # SidecarExtractionFailure carries it (parity with timeout/transport,
                # which keep str(exc)). _emit_failed already logs+spans this attempt,
                # so no second log line here.
                last_failure = ("schema_invalid", str(exc))
                self._emit_failed(
                    reason="schema_invalid",
                    preview=str(tool_input),
                    retry_count=retry_count,
                )
                continue

            self._emit_run_and_fields(extraction=extraction, narration=narration, start_ns=start_ns)
            return extraction

        assert last_failure is not None  # _MAX_TOTAL_ATTEMPTS >= 1
        reason, detail = last_failure
        raise SidecarExtractionFailure(f"sidecar extractor failed after retry: {reason} ({detail})")

    def _emit_failed(self, *, reason: str, preview: str, retry_count: int) -> None:
        with sidecar_extraction_failed_span(
            reason=reason,
            raw_preview=preview[:_RAW_PREVIEW_LIMIT],
            retry_count=retry_count,
        ):
            pass
        logger.warning(
            "sidecar_extraction.failed reason=%s attempt=%d preview=%r",
            reason,
            retry_count,
            preview[:_RAW_PREVIEW_LIMIT],
        )

    def _emit_run_and_fields(
        self, *, extraction: SidecarExtraction, narration: str, start_ns: int
    ) -> None:
        emitted_flags = {
            field: _is_emitted(getattr(extraction, field)) for field in BUCKET_B_FIELDS
        }
        latency_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
        with sidecar_extraction_run_span(
            model=resolve_model(CallType.CLASSIFICATION),
            prose_length=len(narration),
            field_count=sum(1 for emitted in emitted_flags.values() if emitted),
            latency_ms=int(latency_ms),
        ):
            pass
        for field_name, emitted in emitted_flags.items():
            with sidecar_extraction_field_span(field=field_name, emitted=emitted):
                pass


@dataclass(frozen=True)
class SidecarMismatch:
    """One divergence between the extractor's output and engine-owned state."""

    field: str
    evidence: str


def _known_npc_names(snapshot: Any) -> set[str]:
    """The engine-owned cast: identity-channel ``npc_pool`` members plus any
    promoted ``Npc`` rows. Read defensively so a minimal snapshot is fine."""
    names: set[str] = set()
    for member in getattr(snapshot, "npc_pool", None) or []:
        name = getattr(member, "name", "")
        if name:
            names.add(name)
    for npc in getattr(snapshot, "npcs", None) or []:
        name = getattr(npc, "name", "")
        if name:
            names.add(name)
    return names


def detect_sidecar_extraction_mismatch(
    *, extraction: SidecarExtraction, snapshot: Any
) -> list[SidecarMismatch]:
    """Lie-detector witness (seed, Story 151-2).

    Membership is engine-owned (ADR-150 — the IntentRouter seats opponents
    pre-narrator). An ``npcs_present`` name the engine never seated (absent from
    the snapshot cast) is the canonical "two readers of one prose disagree". The
    later cutover stories (151-4 items, 151-5 npcs/cosmetic) add per-field
    witnesses; the skeleton seeds exactly this one.
    """
    known = _known_npc_names(snapshot)
    mismatches: list[SidecarMismatch] = []
    for mention in extraction.npcs_present:
        # Story 158-4: a mention the extractor flagged as a PLACE is correctly
        # declined from the roster by the _apply_npc_mentions place-guard
        # (npc.place_skipped). A place is never in the seated cast, so without this
        # skip the witness would false-positive on the place feature's own correct
        # behavior — polluting the lie-detector channel the GM panel (and the AC4
        # re-verify) reads. An is_place entry is not a phantom NPC; skip it here
        # exactly as the reconcile does.
        if isinstance(mention, dict) and mention.get("is_place"):
            continue
        name = (mention.get("name", "") if isinstance(mention, dict) else "") or ""
        if name and name not in known:
            mismatches.append(
                SidecarMismatch(
                    field="npcs_present",
                    evidence=f"extractor reported npcs_present {name!r} not seated by the engine",
                )
            )
    return mismatches


async def run_sidecar_extraction_watcher(
    *, narration: str, snapshot: Any, llm: SidecarExtractorLLM
) -> SidecarExtraction | None:
    """Shadow-mode post-narration runner (ADR-150 step 2).

    Runs the extractor, emits the ``sidecar_extraction.mismatch`` lie-detector
    span on divergence, and APPLIES NOTHING (cutover is 151-4 / 151-5). NON-FATAL
    by contract (mirrors ``run_dispatch_engagement_watcher``): a crash here would
    tear down WS turn delivery *after* the prose already broadcast, so every
    failure is caught and surfaced as a loud span — never raised, never silent.
    Returns the extraction (shadow only) or ``None`` on failure / empty prose.
    """
    # Cost Scales with Drama: a turn with no prose has nothing to read — skip the
    # live Haiku call rather than extract from nothing (matches the sibling
    # post-narration classifier's non-empty-narration gating). Logged at debug so
    # "skipped (cost gate)" is distinguishable from "runner never reached".
    if not narration.strip():
        logger.debug("sidecar_extraction: skipping empty narration (cost gate)")
        return None
    try:
        try:
            extraction = await SidecarExtractor(llm=llm).extract(
                narration=narration, snapshot=snapshot
            )
        except SidecarExtractionFailure as exc:
            # extract() already fired the loud ERROR sidecar_extraction.failed
            # span per attempt; stay non-fatal so the turn pipeline continues —
            # the per-field catch-loops are the net (ADR-150 No Silent Fallbacks).
            logger.error(
                "sidecar_extraction.failed reason=extract_failed exc=%s "
                "(turn pipeline continues; catch-loops are the net)",
                exc,
            )
            return None
        for mismatch in detect_sidecar_extraction_mismatch(
            extraction=extraction, snapshot=snapshot
        ):
            with sidecar_extraction_mismatch_span(field=mismatch.field, evidence=mismatch.evidence):
                pass
        return extraction
    except Exception as exc:  # noqa: BLE001 — observability must never abort the turn
        logger.error(
            "sidecar_extraction.watcher_crashed error_type=%s error=%s (turn pipeline continues)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        # Loud span so the GM panel shows a crashed lie-detector, not a clean turn
        # (mirrors run_dispatch_engagement_watcher's crashed-span; OTEL principle).
        with sidecar_extraction_watcher_crashed_span(error_type=type(exc).__name__, error=str(exc)):
            pass
        return None


__all__ = [
    "BUCKET_B_FIELDS",
    "SidecarExtraction",
    "SidecarExtractionFailure",
    "SidecarExtractor",
    "SidecarExtractorLLM",
    "SidecarMismatch",
    "detect_sidecar_extraction_mismatch",
    "run_sidecar_extraction_watcher",
]
