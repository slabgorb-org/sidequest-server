"""Orchestrator — Phase 1 narration turn pipeline.

Port of sidequest-agents/src/orchestrator.rs (Phase 1 slice only).
ADR-082: Python server narration vertical slice.

Phase 1 covers the narration-only turn path:
  Player action (raw text)
    → build context (world state + character + genre prompts)
    → call narrator agent via ClaudeClient
    → parse narrator response (narration text + game_patch JSON block)
    → return NarrationTurnResult

Phase boundaries are marked with:
  # Phase 1 slice: <subsystem> deferred to Story 41-<N>
and raise NotImplementedError when a deferred code path would be reached
(per CLAUDE.md "No Stubbing").

Out of scope (Phase 2+):
  - Combat encounter dispatch (Phase 3)
  - Dice request handling (Phase 2)
  - Scenario progression (Phase 5)
  - Advancement / beat firing (Phase 6)
  - Media/image/audio triggers (Phase 7)
  - Intent routing beyond state-override (Phase 1 uses exploration only, per ADR-067)
  - Continuity validation, lore filtering, world-builder injection (Phase 2+)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sidequest.agents.npc_context import NpcWorkingSet
    from sidequest.agents.subsystems import BankResult
    from sidequest.game.lore_store import LoreStore
    from sidequest.game.monster_manual import MonsterManual
    from sidequest.game.session import GameSnapshot

# Importing this package wires the 26 tool adapters onto default_registry at
# module import time. Required for the SDK path; the sync ClaudeClient
# path does not depend on the registry.
import sidequest.agents.tools  # noqa: F401  (registration side effect)
from sidequest.agents.anthropic_cost import cost_band
from sidequest.agents.aside_resolver import AsidePromptStash
from sidequest.agents.claude_client import (
    ClaudeClient,
    ClaudeResponse,
    LlmClient,
)
from sidequest.agents.claude_client import (
    TimeoutError as _ClaudeTimeoutError,
)
from sidequest.agents.narrator import (
    NarratorAgent,
    resolve_narrator_iteration_cap,
)
from sidequest.agents.narrator_directives import render_narrator_directives
from sidequest.agents.narrator_guardrails import (
    CONFRONTATION_TRIGGER_CONSTRAINT,
    GUARDRAIL_NAMES,
    LOCATION_PATCH_CONSTRAINT,
    NPC_EXTRACTION_CONSTRAINT,
    NPC_INTRO_VISUAL_CONSTRAINT,
    TOTAL_PROSE_BYTES,
)
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import (
    AttentionZone,
    PromptSection,
    SectionCategory,
)
from sidequest.agents.tooling_protocol import (
    CacheableBlock,
    Message,
    ToolingLlmClient,
    ToolingResult,
    ToolResultBlock,
    ToolUseBlock,
)
from sidequest.game.chassis import ChassisInstance
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import NarrativeEntry, Npc, PartyPeer
from sidequest.game.tension_tracker import PacingHint
from sidequest.game.weather import WeatherState
from sidequest.genre.models.lethality import LethalityPolicy
from sidequest.genre.models.narrative import Prompts
from sidequest.genre.models.rules import ResolutionMode
from sidequest.protocol.dice import RollOutcome
from sidequest.protocol.dispatch import DispatchPackage, NarratorDirective
from sidequest.telemetry.leak_audit import audit_canonical_prose
from sidequest.telemetry.phase_timing import PhaseTimings
from sidequest.telemetry.spans import (
    orchestrator_process_action_span,
    recent_narrative_context_injected_span,
    turn_agent_llm_inference_span,
    verbosity_tier_span,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt-tab cache attribution (Story 60-2 — the GM-panel "eyes")
# ---------------------------------------------------------------------------
#
# The SDK path assembles ``system_blocks[0]`` (cache=True) from the
# System-bucket sections in the Primacy + Early zones (see
# ``_run_narration_turn_sdk`` and ``compose_split_by_zone``). A section therefore
# rides the cached prefix iff BOTH its bucket is System AND its zone is
# Primacy/Early. The zone alone is NOT sufficient: Primacy/Early also hold
# User-bucket guardrails that land in the per-turn user message (uncached).
# These helpers compute that attribution from the SAME inputs the SDK path uses,
# so the panel cannot drift from reality.

# Zones whose System-bucket content rides the cached ``system_blocks[0]`` prefix.
_CACHED_ZONE_VALUES: frozenset[str] = frozenset(
    {AttentionZone.Primacy.value, AttentionZone.Early.value}
)


def _content_digest(text: str) -> str:
    """Short content digest used for per-turn cache-drift detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _section_rides_cache(name: str, zone_value: str) -> bool:
    """True iff a section's content lands in the cached ``system_blocks[0]``.

    Requires BOTH a System bucket (in ``STABLE_SECTION_NAMES``) and a
    Primacy/Early zone — mirrors the SDK assembly exactly.
    """
    from sidequest.agents.prompt_framework.bucket import (
        SectionBucket,
        default_bucket_for_section,
    )

    return (
        default_bucket_for_section(name) == SectionBucket.System
        and zone_value in _CACHED_ZONE_VALUES
    )


def _compute_zones_payload(sections: list[PromptSection]) -> list[dict[str, Any]]:
    """Build the Prompt-tab Zone Breakdown rows with cache attribution.

    Each zone carries ``cached`` (does this zone feed the cached block region)
    and each section carries ``cached`` (does THIS section actually ride
    ``system_blocks[0]`` — bucket-aware) plus ``mis_zoned`` (a ``state``-category
    section that ACTUALLY rides the cached block — bucket-aware).

    Story 60-4 (2026-05-23) corrected ``mis_zoned`` to AND-with-bucket:
    the flag now fires iff ``_section_rides_cache(name, zone) AND
    category == "state"`` — i.e., the section both rides ``system_blocks[0]``
    (System-bucket + Primacy/Early zone) AND is volatile state. The old
    bucket-blind shape (``zone_cached AND category == "state"``) produced
    false positives on the three User-bucket suspect sections
    (``narrator_available_confrontations``, ``trope_beat_directives``,
    ``npc_roster``), which misled Epic 60's original "three mis-zoned state
    sections churn block 0" hypothesis. 60-3 disproved that hypothesis by
    measurement (User-bucket → uncached user message → never touches block 0);
    60-4 closes the false-positive loop. See ``sprint/archive/60-3-session.md``
    and ``sprint/archive/60-4-session.md``.
    """
    zone_buckets: dict[str, list[PromptSection]] = {}
    for s in sections:
        zone_buckets.setdefault(s.zone.value, []).append(s)

    payload: list[dict[str, Any]] = []
    for zone_value in (
        AttentionZone.Primacy.value,
        AttentionZone.Early.value,
        AttentionZone.Valley.value,
        AttentionZone.Late.value,
        AttentionZone.Recency.value,
    ):
        bucket = zone_buckets.get(zone_value, [])
        if not bucket:
            continue
        zone_cached = zone_value in _CACHED_ZONE_VALUES
        payload.append(
            {
                "zone": zone_value.title(),
                "total_tokens": sum(s.token_estimate() for s in bucket),
                "cached": zone_cached,
                "sections": [
                    {
                        "name": s.name,
                        "token_estimate": s.token_estimate(),
                        "category": s.category.value,
                        "content": s.content,
                        "cached": _section_rides_cache(s.name, zone_value),
                        # Story 60-4: AND-with-bucket. Only flag sections that
                        # ACTUALLY ride the cached block (bucket=System AND
                        # zone in {Primacy, Early}) AND are volatile state.
                        "mis_zoned": (
                            _section_rides_cache(s.name, zone_value) and s.category.value == "state"
                        ),
                    }
                    for s in bucket
                ],
            }
        )
    return payload


def _compute_cache_blocks(
    *, stable_text: str, valley_text: str, recency_text: str, tools_payload: str
) -> list[dict[str, Any]]:
    """Per-cacheable-block content digests for the provided block texts.

    ``stable`` and ``tools`` carry cache markers; valley/recency ride uncached
    follow-on blocks (and are omitted when empty, mirroring ``system_blocks``
    assembly). The single-source-of-truth guarantee — that these are the SAME
    texts the SDK client received — lives at the call site
    (``_run_narration_turn_sdk``), which passes the assembled block strings."""
    blocks: list[dict[str, Any]] = [
        {"label": "stable", "digest": _content_digest(stable_text), "cached": True}
    ]
    if valley_text:
        blocks.append({"label": "valley", "digest": _content_digest(valley_text), "cached": False})
    if recency_text:
        blocks.append(
            {"label": "recency", "digest": _content_digest(recency_text), "cached": False}
        )
    blocks.append({"label": "tools", "digest": _content_digest(tools_payload), "cached": True})
    return blocks


# ---------------------------------------------------------------------------
# Narrator constants
# ---------------------------------------------------------------------------

NARRATOR_MODEL: str = "opus"
# Story 61-3 promoted this from a soft warning to a hard refuse-or-truncate cap.
# Renamed in 61-8 §C1 to stop the "lying name" the Reviewer flagged (the
# behavior gate is HARD: cross this and the SDK call is refused before billing).
# ~500K tokens, half of Opus 4.8's 1M window (ADR-098).
PROMPT_BUDGET_BYTES_HARD = 2_000_000

# Recency-zone narrative-window tunables (Story 49-1; tightened to K=2 in 57-1).
# K=2 = 1 player turn + 1 narrator turn. Cap (not floor): any non-empty
# window registers the section with all available entries — a partial window
# on turn 1 of a fresh save still rides into Recency.
# PER_ENTRY_CAP_BYTES bounds a single entry's rendered content; oversized
# entries are truncated and tagged with TRUNCATION_MARKER so the cut is
# visible on the GM panel (Sebastien) and to anyone debugging a save (Keith).
RECENT_NARRATIVE_WINDOW_K: int = 2
RECENT_NARRATIVE_PER_ENTRY_CAP: int = 2048
RECENT_NARRATIVE_TRUNCATION_MARKER: str = "[truncated]"


# ---------------------------------------------------------------------------
# Structured extraction types
# ---------------------------------------------------------------------------


@dataclass
class BeatSelection:
    """A single beat selection from the narrator's output (story 28-6).

    ``outcome`` is the resolved tier the prose describes. On free-text
    turns the narrator emits it; on dice-replay turns the engine
    overwrites it with the dice resolver's tier.

    Port of orchestrator.rs::BeatSelection.
    """

    actor: str
    beat_id: str
    outcome: RollOutcome = RollOutcome.Success  # default for legacy callers
    target: str | None = None
    # Story 47-10 — when beat_id == "cast_spell", the narrator nominates a
    # specific spell from the actor's prepared list. None on non-cast beats
    # or when the narrator omits it (legacy paths). The cast handler in
    # narration_apply uses this to look up the Spell in the world's catalog
    # and route the save branch.
    spell_id: str | None = None
    # Story 102-7 — when the applied beat carries the AWN Plan 2 §6.3
    # ``mutation_resolution`` marker, the narrator nominates WHICH owned
    # mutation via this sidecar (the spell_id mirror). None on every
    # non-mutation beat; the mutation handler in narration_apply routes it
    # through sidequest.mutation.use_ops.
    mutation_id: str | None = None
    # Story 102-6 — when the applied beat is a ``psionic_activation``, the
    # narrator nominates WHICH discipline via this sidecar (the spell_id /
    # mutation_id mirror). None on every non-psionic beat; the activation handler
    # routes it through ``WithoutNumberRulesetModule.activate_discipline`` (ADR-142:
    # any WN sibling that ships a discipline catalog, swn or wwn).
    discipline_id: str | None = None
    # Table confrontations (poker/auction): raise/bet chips. None on every
    # non-table beat. The existing ``target`` field carries the Read/Accuse
    # target seat_id.
    amount: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BeatSelection:
        raw_outcome = d.get("outcome")
        if raw_outcome is None or raw_outcome == "":
            outcome = RollOutcome.Success
        else:
            try:
                outcome = RollOutcome(str(raw_outcome))
                # RollOutcome._missing_ returns Unknown instead of raising,
                # so check if we got Unknown from an invalid literal
                if outcome == RollOutcome.Unknown:
                    from sidequest.telemetry.spans import (
                        encounter_invalid_outcome_tier_span,
                    )

                    with encounter_invalid_outcome_tier_span(
                        beat_id=str(d.get("beat_id", "")),
                        actor=str(d.get("actor", "")),
                        declared_tier=str(raw_outcome),
                        valid_set="CritFail|Fail|Tie|Success|CritSuccess",
                    ):
                        pass
                    raise ValueError(
                        f"BeatSelection declared_tier={raw_outcome!r} not in RollOutcome"
                    )
            except ValueError as exc:
                # Re-raise our custom ValueError, not RollOutcome's
                if "declared_tier" in str(exc):
                    raise
                from sidequest.telemetry.spans import (
                    encounter_invalid_outcome_tier_span,
                )

                with encounter_invalid_outcome_tier_span(
                    beat_id=str(d.get("beat_id", "")),
                    actor=str(d.get("actor", "")),
                    declared_tier=str(raw_outcome),
                    valid_set="CritFail|Fail|Tie|Success|CritSuccess",
                ):
                    pass
                raise ValueError(
                    f"BeatSelection declared_tier={raw_outcome!r} not in RollOutcome"
                ) from exc
        spell_id_raw = d.get("spell_id")
        # Defensive coercion (parity with gold_change / declared_tier): a
        # malformed amount degrades to None rather than throwing out of
        # from_dict. The table branch reads `int(sel.amount or 0)`.
        amount_raw = d.get("amount")
        try:
            amount = int(amount_raw) if amount_raw is not None else None
        except (TypeError, ValueError):
            amount = None
        mutation_id_raw = d.get("mutation_id")
        discipline_id_raw = d.get("discipline_id")
        return cls(
            actor=str(d.get("actor", "")),
            beat_id=str(d.get("beat_id", "")),
            outcome=outcome,
            target=d.get("target"),
            spell_id=str(spell_id_raw) if spell_id_raw else None,
            mutation_id=str(mutation_id_raw) if mutation_id_raw else None,
            discipline_id=str(discipline_id_raw) if discipline_id_raw else None,
            amount=amount,
        )


@dataclass
class VisualScene:
    """Visual scene description extracted from narrator JSON block.

    Port of orchestrator.rs::VisualScene.
    """

    subject: str
    tier: str = ""
    mood: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VisualScene:
        return cls(
            subject=str(d.get("subject", "")),
            tier=str(d.get("tier", "")),
            mood=str(d.get("mood", "")),
            tags=[str(t) for t in d.get("tags", [])],
        )


@dataclass
class NpcMention:
    """An NPC mentioned in the narrator's structured output.

    Accepts either a full struct or a bare string name.
    Fix: playtest-2026-04-12 — bare string NPC names caused serde rejection.

    Port of orchestrator.rs::NpcMention.
    """

    name: str
    pronouns: str = ""
    role: str = ""
    appearance: str = ""
    side: str = "neutral"
    is_new: bool = False
    # ping-pong #74: the narrator marks a mention as a wild animal / beast /
    # monster that belongs to NO culture or faction. When true, the invented-
    # name seam must NOT route the name through the culture-bound person namer
    # (which would mint a person-name + a random culture — "a lion called
    # Keeper Goldbraid of the Emerald City"). Defaults False so every existing
    # mention stays a person, fully backward-compatible.
    is_creature: bool = False
    # sq-playtest 2026-06-10 (long_foundry zombie negotiation): the narrator
    # marks a seated opponent that has LEFT the confrontation (walked away from a
    # negotiation, fled a parley). ADR-116 §4 end-on-no-Other needs a grounded
    # signal for the SOCIAL path — ``opponents_disposition`` is morale-only and
    # nothing else flips a social opponent's ``withdrawn``. When true on a
    # ``side="opponent"`` mention, the engine withdraws the matching opponent
    # actor so the end-on-no-Other sweep resolves the encounter instead of
    # trapping the player. Defaults False (No Silent Fallbacks — absence is never
    # read as departure).
    disengaged: bool = False

    @classmethod
    def from_value(cls, value: Any) -> NpcMention:
        valid_sides = {"player", "opponent", "neutral"}
        if isinstance(value, str):
            logger.debug("npc_mention.bare_string_fallback npc_name=%s", value)
            return cls(name=value, side="neutral")
        if isinstance(value, dict):
            side = str(value.get("side", "") or "neutral")
            if side not in valid_sides:
                from sidequest.telemetry.spans import encounter_invalid_side_span

                with encounter_invalid_side_span(
                    actor_name=str(value.get("name", "?")),
                    declared_side=side,
                    valid_set="player|opponent|neutral",
                ):
                    pass
                raise ValueError(f"NpcMention declared_side={side!r} not in {valid_sides}")
            return cls(
                name=str(value.get("name", "")),
                pronouns=str(value.get("pronouns", "")),
                role=str(value.get("role", "")),
                appearance=str(value.get("appearance", "")),
                side=side,
                is_new=bool(value.get("is_new", False)),
                is_creature=bool(value.get("is_creature", False)),
                disengaged=bool(value.get("disengaged", False)),
            )
        return cls(name=str(value), side="neutral")


@dataclass
class ActionRewrite:
    """Action rewrite from the narrator's game_patch JSON block.

    Port of orchestrator.rs::ActionRewrite.
    """

    you: str = ""
    named: str = ""
    intent: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActionRewrite:
        return cls(
            you=str(d.get("you", "")),
            named=str(d.get("named", "")),
            intent=str(d.get("intent", "")),
        )


@dataclass
class NarrationTurnResult:
    """Result of processing a player action through the Phase 1 narration pipeline.

    Contains the prose narration and all structured fields extracted from the
    narrator's game_patch block. The orchestrator consumer (Story 41-6 server
    dispatch) reads these fields to emit protocol messages and apply state deltas.

    Phase 1 fields: narration, game_patch extraction, OTEL telemetry.
    Phase 2+ fields are absent — dispatch must not assume their presence.
    """

    # Core narration output
    narration: str
    is_degraded: bool = False

    # game_patch extracted fields (all optional — narrator may omit any)
    location: str | None = None
    scene_mood: str | None = None
    visual_scene: VisualScene | None = None
    confrontation: str | None = None
    beat_selections: list[BeatSelection] = field(default_factory=list)
    npcs_present: list[NpcMention] = field(default_factory=list)
    items_gained: list[dict[str, Any]] = field(default_factory=list)
    items_lost: list[dict[str, Any]] = field(default_factory=list)
    # Story 45-14: items dropped/abandoned in-world. Differs from items_lost
    # (gone from continuity — given away, destroyed, stolen) — discarded
    # items remain in inventory with state="Discarded" so they can be
    # narratively recovered. Plumbed through the same narration_apply seam.
    items_discarded: list[dict[str, Any]] = field(default_factory=list)
    # Story 45-15: items used up as consumables (patch-foam applied, ration
    # eaten, charge expended). Removed from inventory through the same
    # narration_apply seam as items_lost — distinguished as a separate lane
    # so the OTEL span can surface "consumable spent" vs. "given away" for
    # the GM panel lie-detector. Playtest 3 Felix found maintenance_kit
    # remained in inventory at quantity=1 after patch-foam use because no
    # extractor lane existed for the consume verb.
    items_consumed: list[dict[str, Any]] = field(default_factory=list)
    footnotes: list[dict[str, Any]] = field(default_factory=list)
    sfx_triggers: list[str] = field(default_factory=list)
    action_rewrite: ActionRewrite | None = None
    # ADR-105 B3 — per-PC private narration prose the narrator
    # partitioned OUT of the public ``narration`` blob at generation
    # time. Each entry: ``{"text": <private prose>, "anchor_pc": <PC
    # name>}``. The public ``narration`` field is public-safe by the
    # amended SDK-narrator output contract; these segments are emitted
    # as NARRATION_SEGMENT events routed by _visibility.visible_to and
    # firewalled by the visibility-gated CoreInvariant (B1). Presentation
    # field with NO successor tool — sidecar-sourced on BOTH backends
    # (NOT in _SDK_TOOL_OWNED_FIELDS). Empty on the overwhelming common
    # case (fully-public turn) — solo/atmospheric byte-unchanged.
    private_prose_segments: list[dict[str, Any]] = field(default_factory=list)
    affinity_progress: list[tuple[str, int]] = field(default_factory=list)
    gold_change: int | None = None
    lore_established: list[str] | None = None
    status_changes: list[dict[str, Any]] = field(default_factory=list)
    # Magic system (Coyote Star iter 3 — Task 3.3). When the narrator
    # emits a ``magic_working`` field on its game_patch, this carries the
    # raw dict through to ``narration_apply.apply_magic_working`` for
    # validation + ledger application. ``None`` on every turn the
    # narrator does NOT invoke a magic working (the common case).
    magic_working: dict[str, Any] | None = None

    # Companion roster mutations (playtest 2026-05-06 wiring fix). When
    # the narrator hires an NPC into the party (\"Donut joins as
    # torchbearer\"), it emits ``companions_added`` so the apply seam
    # mutates ``snapshot.companions`` and a ``party.recruit`` watcher
    # span fires. ``companions_dismissed`` is the symmetric remove path
    # (by name). Empty on every turn no recruit/dismiss happens.
    companions_added: list[dict[str, Any]] = field(default_factory=list)
    companions_dismissed: list[str] = field(default_factory=list)

    # Story 50-4 — in-game day advancement signal from narrator.
    # When > 0, narration_apply calls trope_tick with this value so
    # Pass A2 advances every progressing trope by rate_per_day * clamp(N, 0, 14).
    # Sub-day passage stays 0 (time_of_day handles intra-day cues).
    days_advanced: int = 0

    # Raw game_patch dict (plot-a-course Bundle 5). Carries the full parsed
    # game_patch JSON so narration_apply can dispatch sidecar intents (e.g.
    # plot_course / cancel_course) that aren't individually extracted fields.
    # Empty dict when the narrator emits no game_patch block or it fails to
    # parse (both treated as "no sidecar" by downstream handlers).
    game_patch_dict: dict[str, Any] = field(default_factory=dict)

    # OTEL / telemetry
    agent_name: str | None = None
    agent_duration_ms: int | None = None
    token_count_in: int | None = None
    token_count_out: int | None = None
    prompt_tier: str = ""  # ADR-098: tier system removed
    prompt_text: str | None = None
    raw_response_text: str | None = None

    # Group G Task 5 — entries stripped from the DispatchPackage during
    # structural hiding. Items are ``SubsystemDispatch`` / ``NarratorDirective`` /
    # ``LethalityVerdict``; the session handler consumes these to emit
    # SECRET_NOTE events to their intended recipients (Task 6). Empty whenever
    # the decomposer did not run, or no entries were flagged with
    # ``redact_from_narrator_canonical``.
    secret_routes: list[Any] = field(default_factory=list)

    # Task E1.5-B — SDK-path tool-invocation ledger for the GM-panel
    # lie-detector (ADR-103). On the SDK narration path the 26 WRITE tools
    # apply + persist game state during the tool-dispatch loop; this ledger
    # records every tool the model actually called this turn so Sebastien's
    # GM panel can correlate mechanical mutations against the prose (the
    # narrator can no longer "wing it" — a state change with no ledger entry
    # is a lie). Each entry is ``{"id", "name", "arguments"}`` mirroring the
    # ``ToolUseBlock`` the SDK emitted. EMPTY on every non-SDK path
    # (sync ClaudeClient) — no tool loop runs there, so there is
    # nothing to ledger.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TurnContext — Phase 1 slice of orchestrator.rs::TurnContext
# ---------------------------------------------------------------------------


@dataclass
class TurnContext:
    """State flags and context passed into the narration turn pipeline.

    Phase 1 fields only. Phase 2+ fields (roll_outcome, tactical_grid_summary,
    world_graph, history_chapters, etc.) are not present — raise NotImplementedError
    if a caller attempts to use them before they are ported.

    Port of orchestrator.rs::TurnContext (Phase 1 slice).
    """

    # Encounter state (Phase 1: read to inject encounter rules)
    in_combat: bool = False
    in_chase: bool = False
    in_encounter: bool = False

    # Serialized game state summary for grounding narration (Valley zone)
    state_summary: str | None = None

    # Verbosity / vocabulary (Recency zone)
    narrator_verbosity: str = "standard"  # concise | standard | verbose
    narrator_vocabulary: str = "literary"  # accessible | literary | epic

    # Spec 2026-05-20 confrontation-intent-validator — per-call directive
    # injected by the reprompt loop. When set, prompt assembly registers a
    # recency-zone guardrail telling the narrator what to fix from the
    # first attempt. None on the normal first-call path.
    extra_directive: str | None = None

    # Genre identity (Primacy zone — every tier)
    genre: str | None = None

    # Genre-specific prompt templates from prompts.yaml
    genre_prompts: Prompts | None = None

    # Player character name (Recency zone — action attribution)
    character_name: str = "Player"

    # Interaction count from `snapshot.turn_manager.interaction` at dispatch
    # time. Surfaced on the `prompt_assembled` watcher event so the
    # dashboard's Prompt tab can label the per-turn dropdown ("T3 · narrator
    # · 11k tokens"). Pre-fix the field was unset and the dropdown read
    # "T? · ? · 0 tokens" (playtest 2026-04-30 #1A).
    turn_number: int = 0

    # Production-call-site identity + state references for the SDK narrator
    # path (Phase E wiring — completes the deferral the ``ToolContext``
    # docstring flags as *"Phase E wires this at the production call
    # site"*). ``_build_turn_context`` (session_helpers.py) copies these off
    # ``_SessionData`` so ``_run_narration_turn_sdk`` can build a
    # ``ToolContext`` with real ids + a live ``lore_store``/``monster_manual``
    # instead of degrading to ``world_id=unknown session_id=adhoc`` and a
    # ``lore_store=None`` (which made ``query_lore`` return hit_count=0 and
    # the narrator confabulate canon). Defaulted to None so existing
    # constructors/tests (which build a Phase-1 TurnContext) keep working.
    # ``world_id`` ← ``sd.world_slug``; ``session_id`` ← ``sd.game_slug``
    # (the slug-based session id, e.g. "2026-05-14-caverns_sunden-28").
    world_id: str | None = None
    session_id: str | None = None
    # SaveRepository — kept ``Any`` to avoid a circular import (mirrors
    # ``ToolContext.repository``'s "kept Any to avoid coupling" rationale).
    repository: Any = None
    # Active GenrePack — kept ``Any`` (same circular-import rationale as
    # ``confrontation_def``/``encounter`` above). Story 59-1: the SDK
    # ToolContext stamps this so ``begin_confrontation`` can VALIDATE the
    # requested confrontation type against the genre. The tool does NOT
    # instantiate the encounter — it signals, ``_assemble_turn_result_sdk``
    # routes the type to ``result.confrontation``, and ``narration_apply``
    # creates the encounter on the canonical snapshot. ``None`` on legacy
    # fixture paths that never went through ``_build_turn_context``; the tool
    # fails loudly when it is missing.
    pack: Any = None
    # Narrator-private LoreStore (lives on the session handler, not on the
    # save layer) — query_lore reads this. Quoted/TYPE_CHECKING import:
    # ``from __future__ import annotations`` keeps this annotation a string,
    # so the runtime import is deferred (no circular import via
    # sidequest.game.lore_store).
    lore_store: LoreStore | None = None
    # Per-genre/world MonsterManual (also session-handler-resident) — the
    # lookup_monster tool reads this. Same Phase-E seam as ``lore_store``.
    monster_manual: MonsterManual | None = None

    # Multiplayer merged-turn payload. When the per-room barrier fires and
    # multiple PCs' actions are dispatched as a single narration turn, the
    # session handler stores `(character_name, action_text)` per submitter
    # here so build_narrator_prompt can render a multi-PC declaration block
    # instead of a single `"<one PC> says: <merged blob>"` line — that
    # framing both attributed every PC's words to the dispatch winner and
    # invited the LLM to generate dialogue for PCs whose players had only
    # declared physical actions (SOUL.md "Agency" violation flagged in the
    # 2026-04-29 multiplayer playtest). When `None`, the prompt falls back
    # to the single-player format.
    merged_player_actions: list[tuple[str, str]] | None = None

    # Current location (for degraded response)
    current_location: str = "Unknown"

    # SFX library (Valley zone, Full tier only)
    available_sfx: list[str] = field(default_factory=list)

    # Trope beat directives from previous turn (Early zone)
    pending_trope_context: str | None = None

    # Opening-turn narrator directive (Early zone, turn 0 only).
    # Story 2.3 Slice H / ADR-082: the session handler resolves an
    # opening hook at connect time and stashes the rendered directive
    # here for the first narration turn. Subsequent turns re-build the
    # TurnContext without it (the handler zeroes opening_directive
    # after consumption), matching Rust's `opening_directive.take()`.
    opening_directive: str | None = None

    # Pingpong 2026-06-05 [BAR-1] (MP doubled-opening): True only on an
    # opening turn whose ``action`` is the authored
    # ``first_turn_invitation`` that the cold-open path has ALREADY
    # emitted to the player as a NARRATION. The prompt builder then
    # frames the recency-zone action block as "already shown — continue,
    # do not restate" instead of ``"<PC> says: <invitation>"``, which
    # cued the narrator (via the action-rewrite contract) to novelize
    # the invitation back into its narration — every seeded opening
    # rendered near-duplicate prose twice. False for joiner-orientation
    # openings and the no-seed fallback (their actions are not
    # already-displayed prose).
    opening_seed_shown: bool = False

    # Persistent narrator world context (Valley zone, every turn).
    # Story 41-11 / ADR-082 Phase 2.2 IOU: resolved once at connect time
    # in the session handler. Currently contains the ``AVAILABLE
    # CULTURES`` block produced by
    # :func:`sidequest.server.dispatch.culture_context.resolve_culture_reference`
    # (with lore-only cultures filtered out via ``Culture.chargen``).
    # Phase 3 work will prepend the setting + world-lore blocks here.
    # Rust parity: ``world_context`` string threaded through
    # ``connect.rs`` and consumed by ``WorldBuilder::inject_world_context``.
    world_context: str | None = None

    # Active trope summary for background context (Valley zone)
    active_trope_summary: str | None = None

    # NPC pool — identity-only members the narrator can cite (Wave 2A).
    # Replaces the legacy ``npc_registry`` (dropped in story 45-52) as
    # the cast-pool projection channel. The prompt-rendering path reads
    # from ``npc_pool`` + ``npcs.last_seen_*``.
    npc_pool: list[NpcPoolMember] = field(default_factory=list)

    # Full NPC structs (for merchant context injection — Phase 1 slice: skipped)
    npcs: list[Npc] = field(default_factory=list)

    # Story 75-2: budgeted NPC working-set — the relevance-selected projection
    # of ``npc_pool`` + ``npcs`` that actually enters the narrator prompt
    # (scene-present full floor / off-stage brief or compact). The full roster
    # above persists for other consumers; this is the bounded prompt view that
    # ``register_npc_roster_section`` renders. ``None`` only on contexts built
    # before the budgeting seam ran (legacy/direct construction).
    npc_working_set: NpcWorkingSet | None = None

    # Chassis registry — chassis-as-speaker voice data (register, vocal tics,
    # bond-tier address-form). Defensive copy from session.chassis_registry
    # since TurnContext is a snapshot. Empty for non-rig genres.
    chassis_registry: dict[str, ChassisInstance] = field(default_factory=dict)

    # Party peer identity packets (Story 37-36). Canonical name/pronouns/
    # race/class/level for every non-self PC in the session. Empty on
    # solo sessions — in that case the injector registers no section so
    # we keep the zero-byte-leak discipline (see NPC roster for parallel).
    party_peers: list[PartyPeer] = field(default_factory=list)

    # Chassis-interior positions for every PC in the session
    # (``character_name -> current_room``). Renders into the narrator
    # prompt as the "CREW POSITIONS" section so the narrator knows where
    # each PC is on the Kestrel and can state-patch movements. Empty dict
    # (no chassis aboard) registers no section — zero-byte-leak.
    pc_positions: dict[str, str | None] = field(default_factory=dict)

    # PacingHint from TensionTracker (Late zone — Rust parity at
    # sidequest-api/crates/sidequest-agents/src/prompt_framework/mod.rs:108).
    # Story 42-3 / ADR-082 Phase 3. When ``None``, no pacing section is
    # registered into the narrator prompt — zero byte leak.
    #
    # Spec deviation logged in 42-3 session: context-doc says ``str | None``,
    # but a string field would discard ``escalation_beat`` and force the
    # caller to pre-render the directive. Storing the typed object lets the
    # call site marshal exactly what the Python ``register_pacing_section``
    # helper requires — ``(narrator_directive: str, escalation_beat: str | None)``.
    # Note: Rust's helper takes ``&PacingHint`` directly and does the
    # marshalling internally; Python's helper takes two derived strings, so
    # the call site at ``build_narrator_prompt`` does the marshalling. The
    # *field* mirrors Rust's typed seam; the *helper signatures* differ.
    pacing_hint: PacingHint | None = None

    # Encounter state summary rendered for the Valley zone (Story 3.4).
    # When ``None``, no encounter section is registered. Mutually consistent
    # with ``in_combat``/``in_chase``/``in_encounter`` — if any of those is
    # True, ``encounter_summary`` should be set.
    encounter_summary: str | None = None

    # The matched ConfrontationDef for the active encounter (Story 3.4).
    # Typed as ``Any`` to avoid a circular import through sidequest.genre;
    # runtime shape is ``sidequest.genre.models.rules.ConfrontationDef``.
    # The narrator uses this to render available beats + actors into the
    # Early zone so the LLM can emit valid ``beat_selections``.
    confrontation_def: Any = None

    # Genre pack's full menu of confrontation types — list of
    # ``(type, label, category)`` triples drawn from
    # ``pack.rules.confrontations``. Rendered into the narrator prompt
    # (when no encounter is active) so the LLM picks the most specific
    # type rather than defaulting to generic ``combat``. Playtest
    # 2026-04-25 regression: in space_opera, the narrator picked
    # ``combat`` (Firefight) for a starship dogfight even though the
    # genre's ``rules.yaml`` declares ``ship_combat`` (vessel scale)
    # and ``dogfight`` side-by-side. The menu was implicit; the LLM
    # couldn't see what was on offer.
    available_confrontations: list[tuple[str, str, str]] = field(default_factory=list)

    # Live encounter object (Story 3.4). Typed as ``Any`` to avoid a
    # circular import through sidequest.game. Runtime type:
    # ``sidequest.game.encounter.StructuredEncounter``.
    encounter: Any = None

    # Retrieved lore fragments for the current turn (Valley zone, Story
    # 37-33). Pre-rendered by the session handler via
    # :func:`sidequest.game.lore_embedding.retrieve_lore_context` before
    # the turn fires. ``None`` means no lore section is registered —
    # keeps the prompt zone-clean when the daemon is unavailable or the
    # store is empty. The retrieval helper never returns an empty string
    # (all non-producing paths return ``None``; the producing path
    # returns a non-empty ``<lore>`` block).
    lore_context: str | None = None

    # Retrieved entity fill (Valley zone) — Story 75-5, ADR-118 §D4. The
    # semantic top-k NPC/location/faction cards from the universal index,
    # pre-rendered into typed blocks by ``_build_turn_context`` from
    # :func:`sidequest.game.retrieval_orchestration.retrieve_turn_context`.
    # Each is ``None`` when that type retrieved nothing, so no empty section is
    # registered (zero-byte-leak). The scene-present floor is NOT carried here —
    # it already reaches the prompt via ``npc_working_set`` (no double-injection).
    retrieved_entity_npcs: str | None = None
    retrieved_entity_locations: str | None = None
    retrieved_entity_factions: str | None = None
    # Story 84-3 (WI-4, ADR-118 §A2): the §A2 floor-companion relationship section
    # — the PC↔NPC standing + key beats for a present/named NPC. ``None`` when no
    # relationship card surfaced (zero-byte-leak), like the others.
    retrieved_entity_relationships: str | None = None
    # Story 84-5 (WI-2, ADR-118 §A2): the DORMANT quest / trope recall sections —
    # a completed quest / resolved trope the player referenced this turn. ``None``
    # when none surfaced. Distinct from the ACTIVE quest/trope paths (state_summary /
    # trope foreground), which are unchanged — these carry DORMANT recall only.
    retrieved_entity_quests: str | None = None
    retrieved_entity_tropes: str | None = None

    # Group B (Local DM decomposer) — session handler populates before calling
    # run_narration_turn. Consumed by build_narrator_prompt to register the
    # narrator_directives PromptSection. Default None = decomposer did not run.
    dispatch_package: DispatchPackage | None = None

    # The BankResult from the SINGLE pre-narrator dispatch-bank run
    # (intent_router_pass). build_narrator_prompt consumes this for the
    # narrator_directives section + the lethality arbiter instead of
    # re-running the bank (which would engage every engine twice). Default
    # None = no pre-narrator pass ran (decomposer absent / degraded path).
    bank_result: BankResult | None = None

    # Group C — LethalityArbiter inputs. Session handler populates all three
    # from the active GenrePack + live snapshot before run_narration_turn.
    # When ``lethality_policy`` is non-None, build_narrator_prompt runs the
    # arbiter after run_dispatch_bank and merges its paired must/must-not
    # directives into the same narrator_directives PromptSection.
    lethality_policy: LethalityPolicy | None = None
    pc_cores_by_player: dict[str, CreatureCore] = field(default_factory=dict)
    npc_cores_by_name: dict[str, CreatureCore] = field(default_factory=dict)

    # Per-actor status lists (Task 18 — dual-track momentum). Consumed by
    # the live encounter zone to render Status objects per actor. NOT
    # currently populated by ``_build_turn_context`` (session_helpers.py) —
    # the only code that ever populated this from ``session.characters``
    # was the module-level ``run_narration_turn`` wrapper deleted in
    # story 49-5. Field defaults to ``{}`` in production until the wiring
    # gap is closed; see 49-5 Delivery Findings for the follow-up.
    # Typed as dict[str, list[Any]] to avoid a circular import on Status
    # — matches the existing pattern for ``confrontation_def: Any`` and
    # ``encounter: Any``.
    statuses_by_actor: dict[str, list[Any]] = field(default_factory=dict)

    # Light & Darkness survival clock (Task 7.1). The ``light`` ResourcePool
    # (current/max + threshold narrator_hints) and the acting PC's active
    # darkness statuses (the environment_clock −2 penalty), surfaced to the
    # narrator so guttering/dark prose is state-driven, not improvised. Both
    # are VOLATILE (light.current changes every burn) → registered in the
    # Valley zone, never the cached prefix. ``None``/empty on packs without a
    # light clock (zero-byte-leak). Typed as Any to avoid a ResourcePool/Status
    # circular import in this layer (same pattern as statuses_by_actor).
    light_pool: Any = None
    darkness_statuses: list[Any] = field(default_factory=list)

    # Per-PC class + spell-slot lookup for the live encounter zone (Task 7,
    # C&C B/X class beats). Maps PC actor name → (ClassDef, spell_slots_remaining).
    # When non-empty, build_encounter_context renders class-distinct beat menus
    # via beats_available_for. Empty dict (single-class genre or non-encounter
    # turn) registers no per-PC block — zero-byte-leak. Typed as dict[str, Any]
    # to avoid a circular ClassDef import in this layer.
    pc_classes_by_name: dict[str, Any] = field(default_factory=dict)

    # One-shot ResolutionSignal (Task 18 — dual-track momentum). Consumed in
    # build_narrator_prompt: passed to build_encounter_context to fire the
    # ``[ENCOUNTER RESOLVED]`` zone and the encounter_resolution_signal_consumed
    # span. NOT currently populated by ``_build_turn_context`` (session_helpers.py)
    # — the only code that ever copied ``snapshot.pending_resolution_signal``
    # into this field, and the only code that cleared the signal from the snapshot
    # after consumption, was the module-level ``run_narration_turn`` wrapper
    # deleted in story 49-5. ``snapshot.pending_resolution_signal`` is still
    # set by ``narration_apply.py`` and ``dispatch/yield_action.py`` on
    # encounter resolution but never reaches this field; the [ENCOUNTER
    # RESOLVED] zone is therefore dormant in production. See 49-5 Delivery
    # Findings for the follow-up that must (a) thread the signal through
    # ``_build_turn_context`` and (b) clear it at the session-handler call
    # site after the orchestrator returns. Typed as Any to avoid a circular
    # import on ResolutionSignal.
    pending_resolution_signal: Any = None

    # Magic state for the current world (Valley zone).
    # When non-None, build_narrator_prompt injects the magic-context block so
    # the narrator knows the active plugins, hard_limits, and per-actor ledger
    # bars before composing narration for any magic working.
    magic_state: Any = None  # runtime type: sidequest.magic.state.MagicState | None

    # Fate state projection (ADR-144 F2b, Story 116-2). When non-None,
    # build_narrator_prompt injects the ``fate_state`` section so the narrator sees the
    # PCs' aspects, skills, fate points, and live scene aspects — and the invokable-aspect
    # directive. Populated by the session handler from
    # ``game.ruleset.fate_projection.build_fate_projection(snapshot)`` (the one source of
    # truth it shares with the intent router). None on non-Fate packs — they pay zero tokens.
    fate_state: dict[str, Any] | None = None

    # AWN mutation surface (story 102-7, Plan 2 §5.4). When BOTH are
    # non-None, build_narrator_prompt injects the mutation-context block so
    # the narrator sees owned mutations, costs, and live MP/usage — the same
    # single-chokepoint economics as magic_state (non-mutation worlds pay
    # zero tokens).
    mutation_state: Any = None  # runtime: sidequest.mutation.state.MutationState | None
    mutation_catalog: Any = None  # runtime: sidequest.mutation.models.MutationCatalog | None

    # World-tier items catalog (Story 47-5). When non-None, the
    # reliquaries section drives the Cleric's <available-reliquaries>
    # block in build_magic_context_block. Typed Any to keep this dataclass
    # free of the items model — the builder receives the typed list directly.
    world_items: Any = None  # runtime type: sidequest.genre.models.items.WorldItemsCatalog | None

    # Per-turn phase-timing accumulator (Story: phase-timing instrumentation).
    # Defaults to PhaseTimings.NULL so legacy fixtures and partial mocks
    # continue to work without provisioning a real timer. Real instances
    # are populated by ``_execute_narration_turn`` at action receipt.
    phase_timings: PhaseTimings = field(default_factory=lambda: PhaseTimings.NULL)

    # Orbital tier fields (plot-a-course). Populated by _build_turn_context
    # when the world has an orbital tier (orbital_content is not None).
    # When None/empty, build_narrator_prompt skips the <courses> block —
    # zero byte leak on non-orbital worlds.
    #
    # Types are Any to avoid a circular import on OrbitalContent/Scope;
    # runtime types are:
    #   orbital_content: sidequest.orbital.loader.OrbitalContent | None
    #   orbital_scope:   sidequest.orbital.render.Scope | None
    #   party_body_id:   str | None  (from snapshot.party_body_id)
    #   recent_body_mentions: collections.deque[str]  (from Session)
    #   quest_anchors:   list[str]  (from snapshot.quest_anchors)
    orbital_content: Any = None
    orbital_scope: Any = None
    party_body_id: str | None = None
    recent_body_mentions: Any = field(default_factory=list)  # deque[str] or list[str]
    quest_anchors: list[str] = field(default_factory=list)

    # Recent narrative-log window (Recency zone, Story 49-1; K tightened to 2 in 57-1).
    # Last K=2 narrative_log entries (one player turn + one narrator turn),
    # populated by _build_turn_context from the live snapshot. Rendered as a
    # high-attention prose block by build_narrator_prompt to give the narrator
    # the recent-narration context that ADR-098 lost when --resume was dropped.
    # Empty list = section not registered (zero-byte-leak).
    recent_narrative_log: list[NarrativeEntry] = field(default_factory=list)

    # Live snapshot reference (Story 50-4). Used by build_narrator_prompt to
    # consume + clear ``snapshot.pending_time_skip_summary`` as part of the
    # TIME-SKIP CONTEXT block (one-shot lifecycle — render then clear).
    # None on legacy/fixture paths that never went through ``_build_turn_context``.
    snapshot: GameSnapshot | None = None

    # Beneath Sünden per-turn region projection (the BETTER fix, seam 1+2).
    # Re-derived every turn in ``_build_turn_context`` from the live
    # ``DungeonStore.load_map`` (SQLite is the single source of truth — NOT
    # mirrored onto the persisted snapshot, which has a documented divergence
    # disease). When set, ``build_narrator_prompt`` renders it as a
    # high-attention "you are here" section (gaslight discipline: a
    # structured canonical-state section like the NPC roster, not appended
    # exits: text) carrying the region's theme flavor AND the concrete
    # adjacent region ids — the constrained move vocabulary that makes the
    # narrator's ``current_region`` patch target a VALID graph node so the
    # frontier look-ahead worker expands the dungeon. None for every
    # non-beneath_sunden turn (zero-byte leak). Typed Any to keep this
    # dataclass free of a sidequest.dungeon import (dungeon depends on game
    # models — mirrors the ``encounter: Any`` / ``snapshot`` precedent).
    region_projection: Any = (
        None  # runtime: sidequest.dungeon.region_projection.RegionProjection | None
    )

    # Story 24-10: world-grounding state, the per-turn carrier for the three
    # ToolContext grounding fields (24-6). Populated by ``_build_turn_context``
    # from ``_SessionData.weather_state`` / ``world_demographics`` /
    # ``world_calendar`` (loaded once at session bootstrap), passed straight
    # through to the ToolContext at the SDK construction site so
    # get_world_grounding returns real data. None for a pack/world that
    # authored no grounding (legitimate absence — no silent fallback). Typed
    # ``Any`` for the dicts is unnecessary; they are plain authored YAML.
    weather_state: WeatherState | None = None
    world_demographics: dict[str, Any] | None = None
    world_calendar: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# game_patch extraction helpers
# ---------------------------------------------------------------------------


def _extract_game_patch_json(raw: str) -> dict[str, Any]:
    """Extract and parse the ```game_patch``` block from a raw narrator response.

    Tries ```game_patch first, then falls back to ```json, then returns {}.
    Parse failures are non-fatal: warns and returns empty dict.

    Port of extract_game_patch() in orchestrator.rs.
    """
    # Primary: ```game_patch ... ```
    idx = raw.find("```game_patch")
    if idx != -1:
        after_label = idx + len("```game_patch")
        end_idx = raw.find("```", after_label)
        if end_idx != -1:
            json_str = raw[after_label:end_idx].strip()
            try:
                result = json.loads(json_str)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError as e:
                logger.warning("game_patch block found but failed to parse: %s", e)

    # Fallback: ```json ... ```
    idx = raw.find("```json")
    if idx != -1:
        after_label = idx + len("```json")
        end_idx = raw.find("```", after_label)
        if end_idx != -1:
            json_str = raw[after_label:end_idx].strip()
            try:
                result = json.loads(json_str)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

    return {}


def _strip_json_fence(text: str) -> str:
    """Remove fenced code blocks from narration so the player sees clean prose.

    Takes prose BEFORE the fence block; discards the block and everything after.
    Per narrator contract: prose-then-patch, nothing after.

    Port of strip_json_fence() in orchestrator.rs.
    """
    pattern = re.compile(r"(?s)```(?:json|game_patch)?\s*\n[\s\S]*?\n```")
    match = pattern.search(text)
    if match:
        prose_before = text[: match.start()]
        after_block = text[match.end() :]
        if after_block.strip():
            logger.warning(
                "strip_json_fence: discarding post-patch content "
                "(likely meta-commentary) after_len=%d preview=%r",
                len(after_block.strip()),
                after_block.strip()[:80],
            )
        return prose_before.strip()
    return text.strip()


# ADR-105 B3 public-safe ENFORCEMENT (oq-1 VERIFY-FAIL 2026-05-16).
#
# The narrator partitions private perception into game_patch.private_segments
# (reliable — structured JSON, the segment is emitted + B1-gated correctly)
# but ALSO duplicates it into the public PART-1 prose — including a
# self-labelled "⚠ Aside — Private (X only):" block. The shared NARRATION
# blob is visible_to:"all" by contract, so the duplicate bypasses the
# firewall in parallel. The B3 prose-prompt is not honored by the live SDK
# narrator; prompt adherence is exactly the "Claude wings it" failure mode
# the OTEL principle exists to catch. This is the MECHANICAL backstop:
# whatever the narrator does, the public blob is scrubbed of explicitly
# self-marked private blocks and of near-duplicate private-segment prose,
# and every scrub is surfaced as a watcher event (a firing scrub == a
# narrator contract violation the GM panel must see).

# A line that introduces a narrator-self-labelled private aside. Matches
# the evidenced family: an optional ⚠ / md emphasis, a privacy keyword
# (private|secret|aside|for your eyes|whisper|gm only) AND a parenthetical
# "(<who> only)" OR an explicit "kept to <pron>self" / "no outward sign".
# Case-insensitive, line-anchored. Conservative on the keyword side
# (must co-occur with an "only)"/"kept to …self" privacy qualifier) so it
# cannot eat ordinary prose that merely contains the word "private".
_PRIVATE_ASIDE_LINE = re.compile(
    r"(?im)^[^\S\n]*"
    r"(?:[⚠❗✱*_>\-\s]*)"
    r"(?=.*\b(?:privat\w*|secret\w*|aside|whisper\w*|for your eyes|gm[ -]?only)\b)"
    r"(?:.*\(\s*[^)\n]*?\bonly\s*\)"
    r"|.*\bkept\b[^\n]*\b(?:to|for)\b[^\n]*self"
    r"|.*\bno\b[^\n]*\boutward\b[^\n]*\bsign\b)"
    r".*$"
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'\(“‘A-Z0-9])")
_WORD = re.compile(r"[a-z0-9]+")
# POV / function words that differ between a 2nd-person owner segment and
# a 3rd-person public duplicate — neutralized before overlap scoring so a
# verbatim-modulo-POV duplicate still scores as a duplicate.
_OVERLAP_STOP = frozenset(
    {
        "you",
        "your",
        "yours",
        "yourself",
        "he",
        "his",
        "him",
        "himself",
        "she",
        "her",
        "hers",
        "herself",
        "they",
        "their",
        "them",
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "the",
        "a",
        "an",
        "of",
        "to",
        "is",
        "it",
        "its",
        "and",
        "as",
        "at",
        "in",
        "on",
        "no",
        "not",
    }
)


def _overlap_tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _OVERLAP_STOP}


def _scrub_public_prose(
    prose: str, private_segments: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """Strip narrator-leaked private content from the public NARRATION blob.

    Two deterministic-to-bounded passes:
      1. Labelled-aside strip (ALWAYS): from the first self-labelled
         private-aside line to end-of-prose. The narrator appends private
         asides; nothing public follows a "Private (X only):" marker.
         Sound — this removes an explicitly self-marked block, exactly
         analogous to stripping the ```game_patch``` fence (not the
         "un-baking" ADR-105 forbids, which is unmarked interwoven prose).
      2. Duplicate-sentence strip (when private_segments present): drop any
         public sentence whose content-token Jaccard with a private
         segment is >= _DUP_OVERLAP — catches verbatim and POV-modulo
         paraphrase the narrator copied into PART 1.

    Degrades safely: if pass 2 would empty the prose (narrator wrote zero
    public content — pathological), pass 2 is skipped (pass 1 still
    applied) so the turn still surfaces SOME public text rather than an
    unrenderable empty NARRATION or a raw-bypass re-leak.

    Returns (scrubbed_prose, report). The report is for the
    ``narration.public_scrub`` lie-detector: a non-zero report means the
    narrator violated the B3 public-safe contract this turn.
    """
    _DUP_OVERLAP = 0.5
    report: dict[str, Any] = {
        "labelled_blocks_removed": 0,
        "dup_sentences_removed": 0,
        "chars_removed": 0,
        "orphan_private_block": False,
        "degraded": False,
    }
    original_len = len(prose)
    work = prose

    # Pass 1 — labelled private-aside block (always; even with no segment).
    m = _PRIVATE_ASIDE_LINE.search(work)
    if m is not None:
        work = work[: m.start()].rstrip()
        report["labelled_blocks_removed"] = 1
        # The narrator self-marked private content but emitted no
        # structured segment for it → it is being dropped (lost to the
        # owner), never leaked. Loud signal of double non-compliance.
        report["orphan_private_block"] = not private_segments

    # Pass 2 — near-duplicate of a private segment copied into PART 1.
    if private_segments:
        seg_token_sets = [_overlap_tokens(str(s.get("text", ""))) for s in private_segments]
        seg_token_sets = [ts for ts in seg_token_sets if ts]
        if seg_token_sets:
            sentences = _SENT_SPLIT.split(work)
            kept: list[str] = []
            dropped = 0
            for sent in sentences:
                st = _overlap_tokens(sent)
                is_dup = False
                if st:
                    for ts in seg_token_sets:
                        inter = len(st & ts)
                        union = len(st | ts)
                        if union and inter / union >= _DUP_OVERLAP:
                            is_dup = True
                            break
                if is_dup:
                    dropped += 1
                else:
                    kept.append(sent)
            candidate = " ".join(p.strip() for p in kept if p.strip()).strip()
            if dropped and not candidate:
                # Skipping pass 2 keeps SOME public text rather than an
                # unrenderable empty blob / raw re-leak. Loud.
                report["degraded"] = True
            elif dropped:
                work = candidate
                report["dup_sentences_removed"] = dropped

    scrubbed = work.strip()
    report["chars_removed"] = original_len - len(scrubbed)

    fired = (
        report["labelled_blocks_removed"] or report["dup_sentences_removed"] or report["degraded"]
    )
    if fired:
        try:
            from sidequest.telemetry.watcher_hub import publish_event as _wp

            _wp(
                "state_transition",
                {"field": "narration.public_scrub", **report},
                component="projection",
                severity="warning",
            )
        except Exception:  # noqa: BLE001 — telemetry must never crash a turn
            logger.warning("narration.public_scrub watcher publish failed")
        logger.warning(
            "narration.public_scrub fired: labelled=%d dup_sentences=%d "
            "chars_removed=%d orphan=%s degraded=%s — the SDK narrator "
            "duplicated private perception into the public NARRATION blob "
            "(B3 public-safe contract violation)",
            report["labelled_blocks_removed"],
            report["dup_sentences_removed"],
            report["chars_removed"],
            report["orphan_private_block"],
            report["degraded"],
        )
    return scrubbed, report


def extract_structured_from_response(raw: str) -> dict[str, Any]:
    """Extract the narrator's prose and all structured fields from a raw response.

    The narrator emits a ```game_patch { ... }``` block every turn containing
    footnotes, items, NPCs, mood, etc. This function parses that block and
    maps it to a plain dict, then strips the fence from the returned prose.

    Returns a dict with keys:
      prose, footnotes, items_gained, items_lost, npcs_present,
      visual_scene, scene_mood, sfx_triggers, action_rewrite,
      beat_selections, confrontation, location, affinity_progress, gold_change,
      lore_established.

    Port of extract_structured_from_response() in orchestrator.rs.
    """
    # Parse game_patch before stripping
    patch = _extract_game_patch_json(raw)

    # Log extraction counts for OTEL visibility
    logger.info(
        "game_patch.extracted "
        "footnotes=%d items_gained=%d items_lost=%d items_discarded=%d "
        "items_consumed=%d "
        "npcs_present=%d sfx_triggers=%d "
        "has_visual_scene=%s has_scene_mood=%s has_action_rewrite=%s "
        "beat_selections=%d confrontation=%r "
        "has_location=%s gold_change=%r status_changes=%d "
        "companions_added=%d companions_dismissed=%d",
        len(patch.get("footnotes", [])),
        len(patch.get("items_gained", [])),
        len(patch.get("items_lost", [])),
        len(patch.get("items_discarded", [])),
        len(patch.get("items_consumed", [])),
        len(patch.get("npcs_present", [])),
        len(patch.get("sfx_triggers", [])),
        patch.get("visual_scene") is not None,
        patch.get("mood") is not None or patch.get("scene_mood") is not None,
        patch.get("action_rewrite") is not None,
        len(patch.get("beat_selections", [])),
        patch.get("confrontation"),
        patch.get("location") is not None,
        patch.get("gold_change"),
        len(patch.get("status_changes", [])),
        len(patch.get("companions_added", [])),
        len(patch.get("companions_dismissed", [])),
    )

    prose = _strip_json_fence(raw)

    # ADR-105 B3: private per-PC prose the narrator partitioned out of the
    # public blob. Keep only well-formed entries (a non-empty ``text``
    # string); a malformed entry is dropped here rather than risking an
    # un-routable segment downstream. anchor_pc is optional (the
    # per-segment POV swap no-ops without it).
    _private_segments: list[dict[str, Any]] = [
        {
            "text": str(seg["text"]).strip(),
            "anchor_pc": (str(seg["anchor_pc"]).strip() or None) if seg.get("anchor_pc") else None,
        }
        for seg in patch.get("private_segments", [])
        if isinstance(seg, dict) and isinstance(seg.get("text"), str) and seg["text"].strip()
    ]

    # ADR-105 B3 ENFORCEMENT: scrub the public blob of any private content
    # the narrator (non-compliantly) duplicated into PART 1 — labelled
    # "Private (X only)" asides + near-duplicate private-segment prose.
    # The mechanical guarantee; the prompt is only the soft first layer.
    prose, _ = _scrub_public_prose(prose, _private_segments)

    return {
        "prose": prose,
        # RENDER-NO-SUBJECT fix (ADR-150 amendment, 2026-06-20): ``footnotes`` is the
        # player's knowledge/journal feed — a GENERATIVE/authorial narrator output the
        # post-narration never-invent reader cannot produce (it returned [] every turn
        # → known_facts=0 on mystery worlds). Restored to narrator-owned, reversing the
        # 151-5 cutover for THIS field (the same exception ``private_segments`` holds).
        "footnotes": patch.get("footnotes", []),
        # Story 151-4 (ADR-150 step 4): the seven TRANSACTIONAL fields (items×4,
        # gold_change, companions×2) are RETIRED from the narrator game_patch.
        # The post-narration sidecar extractor produces them and
        # ``narration_apply.merge_sidecar_extraction_transactional`` sources them
        # onto the result before apply. Surface EMPTY here — do NOT read the
        # game_patch even if a (non-compliant) narrator still emits them (the
        # extraction is the sole source; mirrors action_rewrite retirement, 151-3).
        # Keys stay present (empty) so the shared assembler's subscript access is
        # safe.
        #
        # Story 151-5 (ADR-150 step 4, cutover II) retired npcs_present + scene_mood
        # to the extractor too. The 2026-06-20 ADR-150 amendment (RENDER-NO-SUBJECT)
        # CARVES BACK OUT the two GENERATIVE/authorial fields — ``visual_scene`` and
        # ``footnotes`` (above) — to narrator-owned, because a never-invent reader
        # structurally cannot produce them. bucket-B is now EXTRACTIVE-only:
        # items/gold/companions/npcs_present/scene_mood extractor-sourced;
        # visual_scene/footnotes/private_segments narrator-owned.
        "items_gained": [],
        "items_lost": [],
        "items_discarded": [],
        "items_consumed": [],
        "npcs_present": [],
        # Generative art-direction directive (what to PAINT) — narrator-owned, see above.
        "visual_scene": patch.get("visual_scene"),
        "scene_mood": None,
        "sfx_triggers": patch.get("sfx_triggers", []),
        # Story 151-3 (ADR-150 step 3): action_rewrite is RETIRED from the
        # narrator game_patch — it is produced by the pre-narrator IntentRouter
        # now (sourced onto the result from context.dispatch_package). Do NOT
        # surface it here even if a (non-compliant) narrator still emits it.
        "private_segments": _private_segments,
        "beat_selections": patch.get("beat_selections", []),
        "confrontation": patch.get("confrontation"),
        "location": patch.get("location"),
        "affinity_progress": [
            (str(d["name"]), int(d.get("delta", 1)))
            for d in patch.get("affinity_progress", [])
            if isinstance(d, dict) and "name" in d
        ],
        "gold_change": None,  # 151-4: retired from game_patch (extractor-sourced)
        "lore_established": patch.get("lore_established"),
        "status_changes": patch.get("status_changes", []),
        # Magic system (Coyote Star iter 3 — Task 3.3). Forwarded as a
        # raw dict; pydantic validation happens in
        # ``narration_apply.apply_magic_working`` so the parse error is
        # raised at the apply seam (where ``MagicWorkingParseError`` is
        # defined) rather than during extraction.
        "magic_working": patch.get("magic_working"),
        # 151-4: companions retired from game_patch (extractor-sourced, empty here).
        "companions_added": [],
        "companions_dismissed": [],
        # Story 50-4: Coerce to non-negative int. Anything else (string, float,
        # negative, missing) maps to 0 — same silent-drop pattern as items.
        "days_advanced": (
            raw_days
            if isinstance(raw_days := patch.get("days_advanced", 0), int) and raw_days >= 0
            else 0
        ),
    }


# ---------------------------------------------------------------------------
# Task E1.5-B — SDK-path tool-owned / presentation partition.
#
# On the SDK narration path the 26 WRITE tools mutate AND persist
# (``ctx.repository.save``) game state during the tool-dispatch loop. The
# narrator ALSO emits a sidecar ``game_patch`` block (the prompt still
# injects ``narrator_output_only``). Feeding that sidecar through the
# normal assembler would make ``narration_apply`` re-apply every
# tool-owned mutation a SECOND time (double-apply bug). So on the SDK
# path the tool-owned fields are ZEROED — the tools are the single
# authority for those categories — while presentation/signal fields with
# NO successor tool stay sidecar-sourced.
#
# Each entry below maps a ``NarrationTurnResult`` field to the
# ``COVERAGE_MAP`` row(s) (see tests/agents/test_sidecar_coverage_map.py)
# whose successor tool now owns + persists that state during dispatch.
# When zeroed on the SDK-path result, the corresponding
# ``narration_apply._apply_narration_result_to_snapshot`` branch (and the
# websocket_session_handler trope/affinity/clue seams) becomes a no-op,
# so the tool's dispatch-time write is the only write.
#
# game_patch_dict is zeroed too: it carries the escape-hatch sidecar
# intents (plot_course / morale_event / raw world-patch) that
# ``apply_world_patch`` (patches_other) and ``update_npc_disposition``
# (patches_disposition) now own.
#
# items_* / gold_change / lore_established / companions_*
# are deliberately NOT in this partition: NO registered tool in
# sidequest/agents/tools/ mutates inventory, gold, lore, or
# the companion roster (verified — query_character/query_encounter only
# READ inventory), and none has a COVERAGE_MAP row. They stay
# sidecar-sourced so narration_apply remains their single applier on BOTH
# paths. (``quest_updates`` was retired in 77-4 — record_quest is its typed
# successor; a stale key is auto-forwarded by the narration-apply guard.)
#
# KNOWN GAP (out of scope, follow-up): zeroing ``location`` means
# narration_apply's region canonicalization / room-graph promotion
# (narration_apply.py ~1763-1817) no longer runs for SDK turns —
# apply_world_patch only sets the raw string. Tracked, not fixed here.
_SDK_TOOL_OWNED_FIELDS: dict[str, str] = {
    # patches_status (apply_status) + patches_hp (apply_damage) both land
    # as Status entries via narration_apply's status_changes branch.
    "status_changes": "patches_status / patches_hp",
    # patches_other (apply_world_patch /location escape hatch).
    "location": "patches_other",
    # magic_effects (apply_spell_effect) + patches_resource_pool
    # (update_resource_pool) — narration_apply.apply_magic_working.
    "magic_working": "magic_effects / patches_resource_pool",
    # NOTE: ``confrontation`` (encounter START) is intentionally NOT owned
    # here, and post-Story 59-4 the field is not narrator-emitted at all.
    # The router-driven dispatch handler at
    # ``sidequest.agents.subsystems.confrontation.run_confrontation_dispatch``
    # engages the encounter on the canonical snapshot pre-narrator (the
    # Intent Router spine, ADR-113). Story 59-1's ``begin_confrontation``
    # tool + its sidecar lift in ``_assemble_turn_result_sdk`` are retired;
    # ``result.confrontation`` is not set on the SDK path and the
    # ``narration_apply`` consumer that read it is gone. If a future
    # narrator-tool ever sets engagement again, add the key here so the
    # fail-loud backstop catches double-application.
    # encounter_advances (advance_encounter_beat) +
    # confrontation_advances (advance_confrontation) — beat apply loop.
    "beat_selections": "encounter_advances / confrontation_advances",
    # trope_tick (tick_tropes) — session handler tick_tropes(days_advanced=).
    "days_advanced": "trope_tick",
    # patches_resource_pool (update_resource_pool) — session handler
    # apply_resource_patches(affinity_progress=).
    "affinity_progress": "patches_resource_pool",
    # patches_other (apply_world_patch) + patches_disposition
    # (update_npc_disposition) — the escape-hatch sidecar intents
    # (plot_course / morale_event / raw world patch) dispatched off
    # game_patch_dict by _apply_course_sidecar / _apply_morale_sidecar.
    "game_patch_dict": "patches_other / patches_disposition",
}

# Named sentinel for the SDK-path fail-loud invariant: the dataclass
# defaults for every field, computed ONCE at import (the check ran in a
# per-turn hot path before). Compared field-by-field against the assembled
# SDK result so a tool-owned key that drifted off its default crashes
# loudly instead of silently double-applying (CLAUDE.md no silent
# fallbacks). Read-only — never mutate this instance.
_NTR_DEFAULTS = NarrationTurnResult(narration="")

# In-fiction stall substituted when a narrator turn comes back with empty
# player-facing prose (sq-playtest 2026-06-19 BLOCKER). Matches the default
# ``_degraded_result`` stall so an empty-prose turn reads like every other
# unrecoverable-narrator turn rather than a blank successful one.
_EMPTY_NARRATION_STALL = "The world holds its breath."


# ---------------------------------------------------------------------------
# Prompt assembly helpers (ContextBuilder equivalent — inlined per spec)
# ---------------------------------------------------------------------------


def _render_recent_narrative_window(entries: list[NarrativeEntry]) -> str:
    """Render the Recency-zone narrative window as readable prose.

    Each entry becomes ``[Round N — author]\\n<content>`` (no JSON keys),
    blocks joined by blank lines. Content longer than
    :data:`RECENT_NARRATIVE_PER_ENTRY_CAP` is truncated and tagged with
    :data:`RECENT_NARRATIVE_TRUNCATION_MARKER` so the cut is visible to
    anyone reading the prompt (Sebastien on the GM panel, Keith debugging
    a save). Truncation keeps the entry head so the early prose — the part
    most likely to set scene state — survives.

    Sole rendering path for ``recent_narrative_context`` — the body
    returned here is the body registered and the body counted for the
    OTEL span (no-lie invariant).
    """
    blocks: list[str] = []
    for e in entries:
        content = e.content
        if len(content) > RECENT_NARRATIVE_PER_ENTRY_CAP:
            content = (
                content[:RECENT_NARRATIVE_PER_ENTRY_CAP]
                + f" … {RECENT_NARRATIVE_TRUNCATION_MARKER}"
            )
        blocks.append(f"[Round {e.round} — {e.author}]\n{content}")
    return "\n\n".join(blocks)


def _consume_next_turn_directives(snapshot: GameSnapshot) -> str:
    """Render snapshot.next_turn_directives into a recency-zone string and clear.

    Returns empty string when the queue is empty. Spec 2026-05-20: the
    list is populated by the validator's soft_suggest branch
    (narration_apply._apply_narration_result_to_snapshot) and consumed
    once per turn here. The same one-shot pattern as
    snapshot.pending_time_skip_summary.
    """
    if not snapshot.next_turn_directives:
        return ""
    rendered = "\n".join(f"- {d}" for d in snapshot.next_turn_directives)
    snapshot.next_turn_directives.clear()
    return rendered


def _build_fate_state_section(projection: dict[str, Any]) -> str:
    """Render the Fate projection into the narrator's ``fate_state`` prompt section
    (ADR-144 F2b, Story 116-2).

    Surfaces, per PC, the skills + current fate points + invokable character aspects, plus
    the live scene aspects, then the invokable-aspect directive. Returns ``""`` when no PC
    has a Fate sheet (loud-absent — the caller skips the section, never a blank header).

    Agency invariant (SOUL "The Test"): the directive instructs the narrator to PROPOSE
    invokes/compels and never to spend a player's fate point or invoke an aspect on their
    behalf — invoking is the player's choice (the engine debits the point on the player's
    command; the F3 UI surfaces it).
    """
    skills: dict[str, dict[str, int]] = projection.get("skills", {})
    fate_points: dict[str, int] = projection.get("fate_points", {})
    character_aspects: dict[str, list[str]] = projection.get("character_aspects", {})
    scene_aspects: list[str] = projection.get("scene_aspects", [])

    pcs = sorted(set(skills) | set(fate_points) | set(character_aspects))
    if not pcs:
        return ""

    lines: list[str] = ["<fate-state>"]
    for pc in pcs:
        fp = fate_points.get(pc, 0)
        lines.append(f"{pc} — Fate points: {fp}")
        pc_skills = skills.get(pc) or {}
        if pc_skills:
            rendered = ", ".join(f"{name} {rating:+d}" for name, rating in pc_skills.items())
            lines.append(f"  Skills: {rendered}")
        aspects = character_aspects.get(pc) or []
        if aspects:
            lines.append("  Invokable aspects:")
            lines.extend(f"    - {a}" for a in aspects)
    if scene_aspects:
        lines.append("Scene aspects (invokable by anyone):")
        lines.extend(f"  - {a}" for a in scene_aspects)
    lines.append(
        "Directive: you MAY remind the player which aspects are invokable and propose a "
        "compel rooted in one of their aspects. PROPOSE / OFFER only — do NOT spend a "
        "player's fate point or invoke an aspect on their behalf. Invoking is the player's "
        "choice."
    )
    lines.append("</fate-state>")
    return "\n".join(lines)


# Story 126-11 (SOUL "Cost Scales with Drama"): the player's verbosity mode sets
# the BASE length cap; the turn's drama weight scales quiet<->climax AROUND that
# base, for every mode. The ``normal`` tier IS the base (the develop literal an
# underivable-drama turn renders). Mode ordering concise < standard < verbose is
# preserved at every tier. ``(cap_sentences, cap_chars)`` per (mode, tier).
_VERBOSITY_CAP_TABLE: dict[str, dict[str, tuple[int, int]]] = {
    "concise": {"quiet": (3, 300), "normal": (4, 400), "climax": (6, 600)},
    "standard": {"quiet": (6, 600), "normal": (8, 800), "climax": (12, 1200)},
    "verbose": {"quiet": (8, 800), "normal": (10, 1000), "climax": (14, 1400)},
}

# Drama-weight band -> tier. Low drama (a quiet walk) tightens the cap; high
# drama (a climactic reveal) widens it; the middle band holds the base.
_DRAMA_QUIET_MAX = 0.34
_DRAMA_CLIMAX_MIN = 0.67


def _drama_tier(drama_weight: float) -> str:
    """Map a drama weight (0.0–1.0) to a verbosity tier name."""
    if drama_weight >= _DRAMA_CLIMAX_MIN:
        return "climax"
    if drama_weight < _DRAMA_QUIET_MAX:
        return "quiet"
    return "normal"


def _resolve_verbosity_cap(verbosity: str, drama_weight: float | None) -> tuple[str, int, int]:
    """Resolve ``(tier, cap_sentences, cap_chars)`` for a verbosity mode + drama.

    ``drama_weight is None`` means the drama signal could not be derived (a
    legacy / bare ``TurnContext`` with no ``pacing_hint``) — fall back to the
    mode's exact base cap under the explicit ``baseline`` tier (No Silent
    Fallbacks: the baseline is a documented decision, recorded on the span, not
    a hidden snap). Unknown modes resolve to ``standard``.
    """
    mode = str(verbosity)
    if mode not in _VERBOSITY_CAP_TABLE:
        mode = "standard"
    if drama_weight is None:
        sentences, chars = _VERBOSITY_CAP_TABLE[mode]["normal"]
        return "baseline", sentences, chars
    tier = _drama_tier(drama_weight)
    sentences, chars = _VERBOSITY_CAP_TABLE[mode][tier]
    return tier, sentences, chars


def _build_verbosity_section(verbosity: str, cap_sentences: int, cap_chars: int) -> str:
    """Build the narrator verbosity constraint text for the given mode, with the
    drama-scaled hard cap (``cap_sentences`` / ``cap_chars``) injected.

    The cap is resolved by :func:`_resolve_verbosity_cap` (mode base scaled by
    drama weight); this function only renders the mode-appropriate template with
    those numbers. Port of the verbosity match block in
    build_narrator_prompt_tiered(), extended for Story 126-11.
    """
    if verbosity == "concise":
        return (
            "<critical>\n"
            "<length-limit>\n"
            f"HARD LIMIT: Maximum {cap_sentences} sentences of prose. "
            f"DO NOT EXCEED {cap_chars} characters of narrative text.\n"
            "This overrides all other length guidance. If a trope beat or genre instruction "
            "would push you past this limit, cut description — never cut the limit.\n"
            "Action and consequence only. No atmosphere. No sensory detail.\n"
            "The game_patch JSON does not count toward this limit.\n"
            "</length-limit>\n"
            "</critical>"
        )
    if verbosity == "verbose":
        return (
            "<critical>\n"
            "<length-limit>\n"
            f"HARD LIMIT: Maximum {cap_sentences} sentences of prose. "
            f"DO NOT EXCEED {cap_chars} characters of narrative text.\n"
            "This overrides all other length guidance. If a trope beat or genre instruction "
            "would push you past this limit, cut description — never cut the limit.\n"
            "Rich atmosphere for arrivals and reveals. Shorter for simple actions.\n"
            "The game_patch JSON does not count toward this limit.\n"
            "</length-limit>\n"
            "</critical>"
        )
    # Default: standard (also handles unknown values)
    return (
        "<critical>\n"
        "<length-limit>\n"
        f"HARD LIMIT, per acting PC this turn: maximum {cap_sentences} sentences and "
        f"{cap_chars} characters of prose.\n"
        "If no PCs are acting (scene anchor, transition, or pure narrator beat), the same\n"
        f"limit applies to the whole response: {cap_sentences} sentences / {cap_chars} characters total.\n"
        "This overrides all other length guidance. If a trope beat, genre voice instruction, "
        "or MUST-weave directive would push you past this limit, cut description — never cut the limit.\n"
        "Each acting PC gets one short paragraph for simple actions, "
        "or two short paragraphs for arrivals or reveals. Give every PC their own beat — "
        "do not collapse two PCs' actions into a single sentence to save room.\n"
        "The game_patch JSON block does not count toward this limit.\n"
        f"Count sentences per PC before responding. If any PC has more than {cap_sentences}, "
        "cut that PC's beat.\n"
        "</length-limit>\n"
        "</critical>"
    )


def _build_vocabulary_section(vocabulary: str) -> str:
    """Build the narrator vocabulary instruction text for the given setting.

    Port of the vocabulary match block in build_narrator_prompt_tiered().
    """
    if vocabulary == "accessible":
        return (
            "[NARRATION VOCABULARY]\n"
            "Use simple, direct language. Prefer common words over obscure "
            "ones. Keep sentences short and clear. Aim for approximately "
            "8th-grade reading level. No archaic constructions or elaborate "
            "metaphors."
        )
    if vocabulary == "epic":
        return (
            "[NARRATION VOCABULARY]\n"
            "Use elevated, archaic, or mythic diction. Embrace elaborate "
            "sentence structures, rare words, and poetic constructions. "
            "Channel the cadence of sagas, epics, and high fantasy prose. "
            "Unrestricted complexity."
        )
    # Default: literary (also handles unknown values)
    return (
        "[NARRATION VOCABULARY]\n"
        "Use rich but clear prose. Employ varied vocabulary and literary "
        "devices where they serve the narrative. Balance elegance with "
        "accessibility — vivid but not purple."
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Phase 1 narration orchestrator.

    Routes player input → context assembly → narrator agent → game_patch extraction.
    This is the Python port of the Phase 1 path through orchestrator.rs::Orchestrator.

    The narrator is the unified agent (ADR-067). All intents route to narrator.
    Session management follows ADR-066 (persistent Opus sessions via --resume).

    Phase 2+ subsystems (combat dispatch, dice, world-builder injection, lore
    filtering, merchant context) are deferred. See phase-marker comments below.
    """

    def __init__(
        self,
        client: LlmClient | ToolingLlmClient | None = None,
        soul_data: object | None = None,
    ) -> None:
        """Create an orchestrator.

        Args:
            client: LlmClient or ToolingLlmClient for LLM invocations.
                    If None, creates a default ClaudeClient. When the client
                    is a ToolingLlmClient (AnthropicSdkClient),
                    ``run_narration_turn`` routes through
                    ``complete_with_tools`` with the registered tool catalog.
            soul_data: Optional SoulData for SOUL.md principle injection.
                       If None, SOUL.md is loaded from CWD (if present).
        """
        self._client: LlmClient | ToolingLlmClient = (
            client if client is not None else ClaudeClient()
        )
        self._narrator = NarratorAgent()

        # SOUL.md principles (optional)
        if soul_data is not None:
            self._soul_data = soul_data
        else:
            # Attempt to load SOUL.md from CWD
            import pathlib

            from sidequest.agents.prompt_framework.soul import parse_soul_md

            soul_path = pathlib.Path("SOUL.md")
            loaded = parse_soul_md(soul_path)
            self._soul_data = loaded if loaded else None

        # Group G Task 5 — secret routes captured during the most recent
        # ``build_narrator_prompt`` call. Populated by ``redact_dispatch_package``
        # when the incoming DispatchPackage contains entries flagged
        # ``redact_from_narrator_canonical``. Read by ``run_narration_turn`` to
        # attach onto the NarrationTurnResult so the session handler can route
        # them as SECRET_NOTE events (Task 6).
        self._last_secret_routes: list[object] = []

        # Aside-rides-the-cache (playtest 2026-06-07): the most recent SDK
        # turn's exact system blocks + tools + model, refreshed every
        # ``_run_narration_turn_sdk`` call. None until the first SDK turn —
        # the player_action handler falls back to the legacy thin read-view
        # (logged) in that window.
        self._aside_prompt_stash: AsidePromptStash | None = None

    @property
    def aside_prompt_stash(self) -> AsidePromptStash | None:
        """The narrator's stashed SDK prompt artifacts (read-only surface)."""
        return self._aside_prompt_stash

    @property
    def aside_cache_client(self) -> ToolingLlmClient | None:
        """The tooling client a narrator-cache aside can ride, or ``None``.

        ``None`` when the configured backend has no tool loop (legacy
        ``claude -p`` / Ollama) — the aside handler falls back to the thin
        read-view path in that case.
        """
        return self._client if isinstance(self._client, ToolingLlmClient) else None

    # ------------------------------------------------------------------
    # Group G Task 7 — entity token resolver for the leak audit
    # ------------------------------------------------------------------

    def _entity_tokens_for_registry(
        self,
        context: TurnContext,
    ) -> dict[str, list[str]]:
        """Build ``entity_id -> [tokens]`` from the session's NPC stores.

        Wave 2A (story 45-47) / story 45-52: reads from ``npc_pool`` plus
        ``npcs`` rather than the dropped ``npc_registry``. In the current
        data model the ``target`` field in a SubsystemDispatch is the NPC
        name (there is no separate entity_id on :class:`NpcPoolMember` yet).
        We key the token map by ``member.name`` and populate with
        ``[name, role]`` where ``role`` is a non-empty role noun. No alias
        field exists today — a partial token set is still a working audit.
        """
        tokens: dict[str, list[str]] = {}
        for member in context.npc_pool:
            toks: list[str] = []
            if member.name:
                toks.append(member.name)
            if member.role:
                toks.append(member.role)
            if toks:
                tokens[member.name] = toks
        for npc in context.npcs:
            name = npc.core.name if npc.core else None
            if not name or name in tokens:
                continue
            toks = [name]
            tokens[name] = toks
        return tokens

    def _maybe_register_legacy_guardrail(
        self,
        registry: PromptRegistry,
        agent_name: str,
        name: str,
        content: str,
    ) -> None:
        """Register a Recency-zone guardrail PromptSection on the legacy backend only.

        ADR-111 (story 57-4) + story 61-18: on the SDK tool-use path
        (``isinstance(self._client, ToolingLlmClient)``) the guardrail prose
        lives at per-guardrail migration targets, NOT in the Recency zone:

          - ``npc_intro_visual`` / ``npc_extraction`` → the slimmed-sidecar
            Primacy/Stable cached prose (``NARRATOR_OUTPUT_ONLY``).
          - ``location_patch`` → the ``apply_world_patch`` tool ``description``.
          - ``confrontation_trigger`` → its framing-neutral
            ``CONFRONTATION_TRIGGER_CORE`` is composed into the IntentRouter
            ``_SYSTEM_PROMPT`` (``intent_router.py``). On the SDK path the
            narrator does not emit the ``confrontation`` patch field — the
            IntentRouter (ADR-113) decides the trigger pre-narrator — so the
            recognition steering rides the router, not a narrator surface.
            (Story 61-18 corrected the earlier docstring, which wrongly
            implied this guardrail reached the SDK model via a tool
            description; it reached nothing — it was dead prose on the SDK
            path until the core moved to the router.)

        On the legacy ``claude -p`` / Ollama paths (opt-in, non-default) the
        narrator still emits a ``game_patch``, so the Recency-zone
        registration stays byte-identical to pre-111. This helper centralises
        the gate so the registration sites in ``build_narrator_prompt``
        collapse from ~10 lines each to one call.
        """
        if not isinstance(self._client, ToolingLlmClient):
            registry.register_section(
                agent_name,
                PromptSection.new(
                    name,
                    content,
                    AttentionZone.Recency,
                    SectionCategory.Guardrail,
                ),
            )

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    async def build_narrator_prompt(
        self,
        action: str,
        context: TurnContext,
    ) -> tuple[str, PromptRegistry]:
        """Build the narrator prompt for a turn (without invoking the LLM).

        Returns (prompt_text, registry) so callers can inspect zone breakdown.
        This is the Phase 1 port of build_narrator_prompt_tiered() in orchestrator.rs.

        ADR-098: Full/Delta tier gating removed. Every turn builds the same
        prompt shape. The only turn-number-gated section is
        ``opening_scene_constraint`` (turn 0 only).

        Phase 1 omissions (all deferred):
          - LoreFilter world-graph injection (Phase 2 — story 23-4)
          - WorldBuilderAgent history chapter injection (Phase 3 — story 15-18)
          - Merchant context injection (Phase 2 — story 15-16)
          - Tactical grid summary (Phase 3 — story 29-11)
          - Script tool injection (Phase 7 — ADR-056)
          - RollOutcome injection (Phase 2 — story 34-9)
          - Backstory capture directive (Phase 1 only for Backstory intent, which
            is not yet classified in Phase 1)
        """
        registry = PromptRegistry()
        agent_name = self._narrator.name()

        # Group G Task 5 — Structural hiding. Strip every DispatchPackage
        # entry flagged ``redact_from_narrator_canonical`` BEFORE anything
        # downstream reads it. The narrator prompt never sees a redacted
        # entry; ``removed`` is stashed on the orchestrator so
        # ``run_narration_turn`` can forward it to the session handler for
        # SECRET_NOTE routing (Task 6).
        visible_dispatch_package = context.dispatch_package
        if context.dispatch_package is not None:
            from sidequest.agents.prompt_redaction import redact_dispatch_package

            visible_dispatch_package, removed = redact_dispatch_package(context.dispatch_package)
            self._last_secret_routes = list(removed)
        else:
            self._last_secret_routes = []

        # === STATIC SECTIONS (every turn — ADR-098 drops Full/Delta tier gating) ===

        # ADR-067: Always narrator identity (unified agent)
        self._narrator.build_context(registry)

        # Always inject dialogue rules — short and NPCs can appear anytime
        self._narrator.build_dialogue_context(registry)

        # SOUL principles (Early zone)
        if self._soul_data is not None:
            from sidequest.agents.prompt_framework.soul import SoulData

            if isinstance(self._soul_data, SoulData):
                filtered = self._soul_data.as_prompt_text_for(agent_name)
                if filtered:
                    registry.register_section(
                        agent_name,
                        PromptSection.new(
                            "soul_principles",
                            filtered,
                            AttentionZone.Early,
                            SectionCategory.Soul,
                        ),
                    )

        # === OUTPUT FORMAT (narrator must always know the game_patch schema) ===
        # Story 61-9 retired the legacy ``claude -p`` / Ollama narrator path;
        # ``build_output_format`` is now backend-agnostic and always emits the
        # SDK tool-use prose. ``llm_factory.build_llm_client`` fails loud if
        # a non-SDK backend is selected.
        self._narrator.build_output_format(registry)

        # === GENRE IDENTITY (every tier — narrator MUST always know the genre) ===
        # Fix: playtest-2026-04-05 — narrator broke fourth wall asking "What genre is Ashgate Square in?"
        if context.genre:
            genre_display = context.genre.replace("_", " ")
            logger.info(
                "orchestrator.genre_identity_injection genre=%s",
                context.genre,
            )
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "genre_identity",
                    (
                        f"<genre>\nYou are narrating a {genre_display} game. This is the genre — "
                        "use its tone, vocabulary, tropes, and conventions in every response. "
                        "Never ask the player what genre, setting, or system they are playing. "
                        "You already know.\n</genre>"
                    ),
                    AttentionZone.Primacy,
                    SectionCategory.Identity,
                ),
            )

        # === GENRE PROMPT TEMPLATES (from prompts.yaml) ===
        if context.genre_prompts is not None:
            gp = context.genre_prompts

            # Narrator voice — every tier (story 30-2)
            if gp.narrator:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_narrator_voice",
                        f"<genre-voice>\n{gp.narrator}\n</genre-voice>",
                        AttentionZone.Primacy,
                        SectionCategory.Identity,
                    ),
                )

            # NPC behavior — every tier (story 30-2)
            if gp.npc:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_npc_voice",
                        f"<genre-npc>\n{gp.npc}\n</genre-npc>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            # World state tracking — every tier (story 30-2)
            if gp.world_state:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_world_state",
                        f"<genre-world-state>\n{gp.world_state}\n</genre-world-state>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            # Combat — every tier (combat can start mid-session)
            if context.in_combat and gp.combat:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_combat_voice",
                        f"<genre-combat>\n{gp.combat}\n</genre-combat>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            # Chase — every tier (chase can start mid-session)
            if context.in_chase and gp.chase:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_chase_voice",
                        f"<genre-chase>\n{gp.chase}\n</genre-chase>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            # Extraction — every tier. ADR-112 / Story 57-3 re-zoned from
            # Valley → Early so the content lands in ``system_blocks[0]``
            # (the cache-marked block) rather than the uncached Valley
            # follow-on block, intended to realise the cache rebate ADR-112
            # promises. (NOTE per Story 60-3: that rebate is not yet realized
            # in practice — the tool-use loop continuation re-mints the prefix
            # at 5m; 60-4 fixes it. This zoning is still correct and required.)
            if gp.extraction:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_extraction",
                        f"<genre-extraction>\n{gp.extraction}\n</genre-extraction>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            # ADR-098: formerly Full-tier-only; now fire every turn.
            # ADR-112 / Story 57-3 re-zoned Valley → Early — see comment above.
            if gp.keeper_monologue:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_keeper_monologue",
                        f"<genre-keeper>\n{gp.keeper_monologue}\n</genre-keeper>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            # ADR-112 / Story 57-3 re-zoned Valley → Early — see comment above.
            if gp.town:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_town",
                        f"<genre-town>\n{gp.town}\n</genre-town>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            # Story 61-11 (ADR-112 amendment): chargen prose is scene-
            # gated on the existing ``TurnContext.opening_directive`` —
            # the only one of the four ADR-112 promotions whose scene
            # scope (post-chargen opening turn) was cleanly expressible
            # via existing runtime state. The directive is populated by
            # ``_populate_opening_directive_on_chargen_complete``
            # (``websocket_session_handler.py:181-346``) at chargen
            # confirmation and cleared after the opening turn fires, so
            # this block fires at most once per session — eliminating
            # the ~150-tok per-turn carry on every subsequent neutral
            # turn. The section was also demoted from
            # ``STABLE_SECTION_NAMES`` so it rides the User bucket
            # (uncached) instead of glueing onto the cached System
            # prefix; both halves are required.
            if gp.chargen and context.opening_directive is not None:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_chargen",
                        f"<genre-chargen>\n{gp.chargen}\n</genre-chargen>",
                        AttentionZone.Early,
                        SectionCategory.Genre,
                    ),
                )

            if gp.transition_hints:
                hints = [f'  {k}: "{v}"' for k, v in gp.transition_hints.items()]
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "genre_transition_hints",
                        "transition_hints:\n" + "\n".join(hints),
                        AttentionZone.Late,
                        SectionCategory.Format,
                    ),
                )

        # === STATE-DEPENDENT SECTIONS (every tier) ===

        # Available confrontation menu — render when no encounter is
        # active, so the narrator's ``confrontation`` field maps to the
        # most specific type the genre offers (e.g., ``ship_combat`` /
        # ``dogfight`` instead of generic ``combat``). The narrator
        # prompt at ``narrator.py:135-148`` already references
        # "AVAILABLE ENCOUNTER TYPES in game_state" — this section is
        # what fulfills that contract. Suppressed when an encounter is
        # already live (the encounter-live zone enumerates the active
        # type's beats + actors; alternates aren't relevant per the
        # narrator rule "Only include on the turn the encounter STARTS").
        # Playtest 2026-04-25 regression: in space_opera the narrator
        # picked ``combat`` (Firefight) for a starship dogfight even
        # though the genre's rules.yaml declares ship_combat (vessel
        # scale) and dogfight side-by-side.
        if (
            context.available_confrontations
            and not context.in_combat
            and not context.in_chase
            and not context.in_encounter
            and context.pending_resolution_signal is None
        ):
            menu_lines = "\n".join(
                f"- {cdef_type}: {cdef_label}" + (f" (category={cdef_cat})" if cdef_cat else "")
                for cdef_type, cdef_label, cdef_cat in context.available_confrontations
            )
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "narrator_available_confrontations",
                    (
                        "<available-encounter-types>\n"
                        "AVAILABLE ENCOUNTER TYPES (for the ``confrontation`` "
                        "field — pick the MOST SPECIFIC type that matches the "
                        "fiction; never default to a generic ``combat`` if a "
                        "more specific type is on the list):\n"
                        f"{menu_lines}\n"
                        "</available-encounter-types>"
                    ),
                    AttentionZone.Early,
                    SectionCategory.State,
                ),
            )

        # Encounter rules for ANY active encounter type. The narrator's
        # build_encounter_context call renders live beats + actors + both dials
        # + per-actor statuses + tags directly into the registry.
        # Also fires when only pending_resolution_signal is set — the encounter
        # flags may have been cleared by the engine on the resolution turn, but
        # the [ENCOUNTER RESOLVED] zone must still be emitted this turn.
        if (
            context.in_combat
            or context.in_chase
            or context.in_encounter
            or context.pending_resolution_signal is not None
        ):
            self._narrator.build_encounter_context(
                registry,
                encounter=context.encounter,
                cdef=context.confrontation_def,
                encounter_summary=context.encounter_summary,
                statuses_by_actor=context.statuses_by_actor,
                resolution_signal=context.pending_resolution_signal,
                pc_classes_by_name=context.pc_classes_by_name or None,
            )
            if context.pending_resolution_signal is not None:
                from sidequest.telemetry.spans import (
                    encounter_resolution_signal_consumed_span,
                )
                from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

                sig = context.pending_resolution_signal
                with encounter_resolution_signal_consumed_span(
                    outcome=sig.outcome,
                    final_player_metric=sig.final_player_metric,
                    final_opponent_metric=sig.final_opponent_metric,
                ):
                    pass
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "encounter",
                        "op": "resolution_signal_consumed",
                        "outcome": sig.outcome,
                        "final_player_metric": sig.final_player_metric,
                        "final_opponent_metric": sig.final_opponent_metric,
                    },
                    component="encounter",
                )

        # Phase 1 slice: tactical grid injection deferred to Story 41-9
        # Phase 1 slice: lore filter (world_graph) deferred to Story 41-7
        # Phase 1 slice: world-builder history chapter injection deferred to Story 41-8
        # Phase 1 slice: merchant context injection deferred to Story 41-7

        # Opening-turn directive (Early zone, turn 0 only).
        # Story 2.3 Slice H: the session handler feeds the resolved
        # opening hook's prompt into the narrator's Early zone for
        # the opening turn, then clears the field so later turns run
        # directive-free. Placed ahead of trope directives because the
        # opening always wins when both are set — there shouldn't be
        # trope carryover on turn 0 anyway.
        if context.opening_directive:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "opening_directive",
                    context.opening_directive,
                    AttentionZone.Early,
                    SectionCategory.State,
                ),
            )

        # Trope beat directives (Early zone)
        if context.pending_trope_context:
            logger.info("orchestrator.trope_beat_injection beats_injected=1")
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "trope_beat_directives",
                    context.pending_trope_context,
                    AttentionZone.Early,
                    SectionCategory.State,
                ),
            )

        # NPC roster — canonical identity anchor (Early zone). Story 37-44.
        # Without this the narrator cannot see who exists and reinvents
        # pronouns/role each turn (playtest 3: Frandrew she/her captain →
        # he/him grease monkey in 10 turns).
        # Wave 2A (story 45-47): reads from ``npc_pool`` + ``npcs`` rather
        # than the deprecated ``npc_registry``; gaslight-preserving format
        # makes pool members and stateful Npcs indistinguishable to the
        # narrator.
        # Story 75-2: render the budgeted working-set (scene-present full floor /
        # off-stage brief or compact) instead of dumping the full roster every
        # turn. The working-set is always populated by the live turn-build path;
        # the npc_pool/npcs branch is the legacy fallback for contexts built
        # without the budgeting seam (direct construction / older tests).
        if context.npc_working_set is not None:
            registry.register_npc_roster_section(
                agent_name,
                working_set=context.npc_working_set,
            )
        elif context.npc_pool or context.npcs:
            registry.register_npc_roster_section(
                agent_name,
                npc_pool=context.npc_pool,
                npcs=context.npcs,
            )

        # Beneath Sünden per-turn region projection (BETTER fix, seam 1+2).
        # Same Early-zone canonical-identity discipline as the NPC roster:
        # "you are here" + the EXACT adjacent region ids (constrained move
        # vocabulary). None for every non-beneath_sunden turn — zero-byte
        # leak (register_region_section early-returns on None).
        if context.region_projection is not None:
            registry.register_region_section(
                agent_name,
                region_projection=context.region_projection,
            )

        # Light & Darkness survival clock (Task 7.1). Surface the compact light
        # state (current/max + active threshold hint) and the acting PC's
        # darkness penalty so guttering/dark prose is state-driven. Valley
        # (volatile) zone — light.current changes every burn, must not ride the
        # cached prefix. None pool → no section (zero-byte-leak).
        if context.light_pool is not None:
            registry.register_light_section(
                agent_name,
                pool=context.light_pool,
                statuses=context.darkness_statuses,
            )

        # Chassis voices — chassis as named speakers with bond-tier name-form.
        # See register_chassis_voice_section docstring; mirrors npc_roster
        # discipline. Slice scope: addresses-form derived from the active
        # character's name; bond_seed placeholder id "player_character"
        # rebinds to real player_id at chargen wiring (follow-up).
        if context.chassis_registry:
            registry.register_chassis_voice_section(
                agent_name,
                context.chassis_registry,
                context.character_name,
            )

        # Party-peer roster — canonical identity anchor for other PCs.
        # Story 37-36 (port-drift reopen). In sealed-letter multiplayer,
        # Player A's narrator turn needs ground truth about Players B/C/...
        # or their pronouns/race/class drift save-to-save (playtest 3:
        # Blutka he/him in own save became she/her in Orin's save).
        if context.party_peers:
            # ``peer_count`` is the count of OTHER PCs (party_peers excludes
            # self). Pre-fix the field was named ``party_size`` which read as
            # full-party headcount; in a 2-player session the log line said
            # ``party_size=1`` (1 peer) and looked like a count-off-by-one
            # bug to anyone tailing the log without the context. Renamed
            # 2026-05-03 [OBS]; the test in
            # ``tests/server/test_party_peer_identity.py`` accepts both old
            # and new spellings during the cutover so external GM-panel
            # filters keep working until they're audited and updated to
            # the new key.
            logger.info(
                "orchestrator.party_peer_injection peer_count=%d current_player=%s",
                len(context.party_peers),
                context.character_name,
            )
            registry.register_party_peer_section(agent_name, context.party_peers)

        # Party-scale seat-count signal (Story 126-36). ALWAYS fires, unlike
        # the peer roster above (which is MP-only). A SOLO session leaves the
        # peer list empty, so before this the narrator had no party-size
        # ground truth and improvised a party-scaled quest brief ("fifty each
        # for two riders") in a one-player dust_and_lead game. ``party_peers``
        # excludes self, so seat count = 1 + len(peers). The span fires every
        # build — even solo (player_count=1) — so the GM panel can prove the
        # narrator was told the count (mirrors narrator.seed_context below).
        from sidequest.telemetry.spans import SPAN_NARRATOR_PARTY_SCALE, Span

        _player_count = 1 + len(context.party_peers)
        with Span.open(SPAN_NARRATOR_PARTY_SCALE, {"player_count": _player_count}):
            registry.register_party_scale_section(agent_name, _player_count)

        # Chassis interior positions — renders the Ship-tab source of truth
        # into the narrator prompt + the state-patch instruction.
        if context.pc_positions:
            registry.register_chassis_position_section(agent_name, context.pc_positions)

        # Game state (Valley zone)
        if context.state_summary:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "game_state",
                    f"<game_state>\n{context.state_summary}\n</game_state>",
                    AttentionZone.Valley,
                    SectionCategory.State,
                ),
            )

        # World context — persistent across turns. Carries the AVAILABLE
        # CULTURES block with ``Culture.chargen=False`` entries filtered out
        # (Story 41-11, closing the Phase 2.2 IOU). Strip the leading newline
        # the helper emits for Rust-style concat — the registry handles
        # section separation.
        #
        # Story 61-20 (ADR-112 zone-promotion): zoned ``Early`` (was Valley)
        # and added to ``STABLE_SECTION_NAMES`` so the session-static culture
        # roster rides the cache-marked system prefix and is written once per
        # session instead of re-written into the volatile tail every turn.
        if context.world_context:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "world_context",
                    context.world_context.lstrip("\n"),
                    AttentionZone.Early,
                    SectionCategory.State,
                ),
            )

        # Retrieved lore (Valley zone) — Story 37-33. Semantic-search
        # results from the player's action embedded against the lore
        # store. Only registered when a non-empty block was produced;
        # ``None`` means the daemon was unavailable or no fragments
        # cleared the similarity floor, and the prompt stays quiet.
        if context.lore_context:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "retrieved_lore",
                    context.lore_context,
                    AttentionZone.Valley,
                    SectionCategory.State,
                ),
            )

        # Retrieved entity fill (Valley zone) — Story 75-5, ADR-118 §D4. Typed
        # NPC/location/faction sections from the universal index, registered only
        # when non-empty (zero-byte-leak — an empty type is ``None`` and registers
        # nothing). Sibling of the lore block above; the scene-present floor is
        # NOT registered here (it rides ``npc_working_set``, no double-injection).
        for section_name, section_body in (
            ("retrieved_npcs", context.retrieved_entity_npcs),
            ("retrieved_locations", context.retrieved_entity_locations),
            ("retrieved_factions", context.retrieved_entity_factions),
            # Story 84-3 (WI-4, §A2, Reviewer blocker): inject the relationship
            # section so the narrator sees the present/named NPC's standing + beats.
            ("retrieved_relationships", context.retrieved_entity_relationships),
            # Story 84-5 (WI-2, §A2): inject the DORMANT quest / trope recall
            # sections so the narrator can answer "what happened with X?". Active
            # quests/tropes ride their existing paths — not registered here.
            ("retrieved_quests", context.retrieved_entity_quests),
            ("retrieved_tropes", context.retrieved_entity_tropes),
        ):
            if section_body:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        section_name,
                        section_body,
                        AttentionZone.Valley,
                        SectionCategory.State,
                    ),
                )

        # Magic context (Valley zone) — injected when a world has magic.yaml loaded.
        # Tells the narrator which plugins are active, what the hard_limits are,
        # and the per-actor ledger bars so it can emit magic_working correctly.
        #
        # Story 61-12: the CRITICAL MAGIC EFFECT / RULE / NEGATIVE CASE banners
        # are gated behind the same chokepoint so non-magic worlds
        # (road_warrior, pulp_noir, tea_and_murder, spaghetti_western) never
        # pay the ~400 tok these rules cost. Single gate, no parallel mechanism.
        if context.magic_state is not None:
            from sidequest.agents.narrator_prompts import NARRATOR_MAGIC_OUTPUT_RULES
            from sidequest.magic.context_builder import (
                build_magic_static_block,
                build_magic_volatile_block,
            )

            registry.register_section(
                agent_name,
                PromptSection.new(
                    "magic_output_rules",
                    f"<critical>\n{NARRATOR_MAGIC_OUTPUT_RULES}\n</critical>",
                    AttentionZone.Primacy,
                    SectionCategory.Guardrail,
                ),
            )

            reliquaries = None
            if context.world_items is not None:
                reliquaries = list(context.world_items.reliquaries)

            # Story 61-20 (ADR-112 zone-promotion): split the old single
            # ``magic_context`` block into its session-static head (world config
            # + hard_limits) and its volatile per-actor tail (live ledger bar
            # values, learned-magic, reliquaries). The static head rides the
            # cache-marked system prefix (Early + STABLE_SECTION_NAMES) and is
            # written once; the volatile tail stays in Valley (User bucket) so
            # the per-turn ledger churn never pollutes the 1h prefix (the 61-19
            # regression). Both still reach the narrator, just in different
            # cacheable blocks.
            magic_static = build_magic_static_block(magic_state=context.magic_state)
            if magic_static:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "magic_hard_limits",
                        f"<magic-context>\n{magic_static}\n</magic-context>",
                        AttentionZone.Early,
                        SectionCategory.State,
                    ),
                )

            magic_volatile = build_magic_volatile_block(
                magic_state=context.magic_state,
                actor_id=context.character_name or None,
                reliquaries=reliquaries,
            )
            if magic_volatile:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "magic_context",
                        f"<magic-ledger>\n{magic_volatile}\n</magic-ledger>",
                        AttentionZone.Valley,
                        SectionCategory.State,
                    ),
                )

        # Story 102-7 (Plan 2 §5.4) — AWN mutation context. The magic-block
        # pattern retold for the pack whose magic IS mutation: static owned
        # surface at Early (changes only on acquisition; NOT added to
        # STABLE_SECTION_NAMES — cache promotion is a separate ADR-112 pass),
        # live MP/usage ledger in Valley. Worlds without a mutation surface
        # register nothing and pay nothing.
        if context.mutation_state is not None and context.mutation_catalog is not None:
            from sidequest.mutation.context_builder import (
                build_mutation_static_block,
                build_mutation_volatile_block,
            )

            mutation_static = build_mutation_static_block(
                mutation_state=context.mutation_state,
                catalog=context.mutation_catalog,
            )
            if mutation_static:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "mutation_context_static",
                        f"<mutation-context>\n{mutation_static}\n</mutation-context>",
                        AttentionZone.Early,
                        SectionCategory.State,
                    ),
                )

            mutation_volatile = build_mutation_volatile_block(
                mutation_state=context.mutation_state,
                catalog=context.mutation_catalog,
            )
            if mutation_volatile:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "mutation_context",
                        f"<mutation-ledger>\n{mutation_volatile}\n</mutation-ledger>",
                        AttentionZone.Valley,
                        SectionCategory.State,
                    ),
                )

        # Fate state (ADR-144 F2b, Story 116-2). The narrator sees the SAME projection the
        # router built (one source of truth — build_fate_projection), rendered with the
        # invokable-aspect directive. Dynamic per turn (fate points + aspects mutate in play)
        # → Valley/State zone, NOT cache-promoted (ADR-112: only session-static sections ride
        # the cache). Presence of the injected projection IS the gate (the session handler only
        # populates it for Fate packs) — same discipline as magic_state above.
        if context.fate_state is not None:
            fate_block = _build_fate_state_section(context.fate_state)
            if fate_block:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "fate_state",
                        fate_block,
                        AttentionZone.Valley,
                        SectionCategory.State,
                    ),
                )

        # Story 50-4 — TIME-SKIP CONTEXT block. When the prior narrator turn
        # advanced multiple in-game days, Pass A2 has queued beat events on
        # ``snapshot.pending_time_skip_summary``; render and consume them.
        snapshot = context.snapshot
        if snapshot is not None and snapshot.pending_time_skip_summary:
            from sidequest.agents.narrator import _render_time_skip_context  # noqa: PLC0415

            time_skip_block = _render_time_skip_context(
                snapshot.pending_time_skip_summary,
                snapshot.days_elapsed,
            )
            if time_skip_block:
                # One-shot lifecycle — clear BEFORE registering so a register
                # failure cannot cause double-delivery of beats
                # to the narrator on the next turn.
                snapshot.pending_time_skip_summary = []
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "time_skip_context",
                        time_skip_block,
                        AttentionZone.Early,
                        SectionCategory.State,
                    ),
                )

        # Active trope summary (Valley zone)
        if context.active_trope_summary:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "active_tropes",
                    context.active_trope_summary,
                    AttentionZone.Valley,
                    SectionCategory.State,
                ),
            )

        # Seed tropes (Valley zone) — Epic 22, Story 22-3. Active seeds
        # surface their full authored prose; expired seeds surface as
        # [Faded] callbacks. The span fires every build (even on empty
        # state) so the GM panel can distinguish "no seeds this turn"
        # from "renderer not invoked".
        from sidequest.agents.seed_context_builder import build_seed_context_block
        from sidequest.telemetry.spans import SPAN_NARRATOR_SEED_CONTEXT, Span

        snapshot_for_seeds = context.snapshot
        active_seeds_list = (
            list(snapshot_for_seeds.active_seeds) if snapshot_for_seeds is not None else []
        )
        seed_ghosts_list = (
            list(snapshot_for_seeds.seed_ghosts) if snapshot_for_seeds is not None else []
        )
        with Span.open(
            SPAN_NARRATOR_SEED_CONTEXT,
            {
                "active_count": len(active_seeds_list),
                "ghost_count": len(seed_ghosts_list),
            },
        ):
            if active_seeds_list or seed_ghosts_list:
                seed_trope_by_id = {
                    s.id: s
                    for s in (getattr(context.pack, "seed_tropes", []) or [])
                    if s.id is not None
                }
                seed_block = build_seed_context_block(
                    active_seeds_list, seed_ghosts_list, seed_trope_by_id
                )
                if seed_block:
                    registry.register_section(
                        agent_name,
                        PromptSection.new(
                            "seed_context",
                            seed_block,
                            AttentionZone.Valley,
                            SectionCategory.State,
                        ),
                    )

        # SFX library (Valley zone) — ADR-098: fires every turn
        if context.available_sfx:
            sfx_list = ", ".join(context.available_sfx)
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "sfx_library",
                    (
                        "[AVAILABLE SFX]\n"
                        "When your narration describes a sound-producing action, include matching "
                        "SFX IDs in sfx_triggers. Pick based on what HAPPENED, not what was mentioned.\n"
                        f"Available: {sfx_list}"
                    ),
                    AttentionZone.Valley,
                    SectionCategory.State,
                ),
            )

        # Phase 1 slice: RollOutcome injection deferred to Story 41-6 (dice protocol, Phase 2)

        # Narrator verbosity (Recency zone — every turn). Story 126-11: the hard
        # <length-limit> cap rides the turn's drama weight (already computed on
        # pacing_hint) — the player's mode sets the base, drama scales it
        # quiet<->climax. The verbosity_tier span is the GM-panel lie detector:
        # it records the tier + cap so the dev can confirm the cap tracks drama
        # rather than the narrator improvising a length. drama_weight is None
        # when no pacing_hint was derived (legacy/bare ctx) -> baseline cap.
        verbosity_drama = (
            context.pacing_hint.drama_weight if context.pacing_hint is not None else None
        )
        verbosity_tier, cap_sentences, cap_chars = _resolve_verbosity_cap(
            context.narrator_verbosity, verbosity_drama
        )
        with verbosity_tier_span(
            tier=verbosity_tier,
            weight=verbosity_drama if verbosity_drama is not None else 0.0,
            cap_sentences=cap_sentences,
            cap_chars=cap_chars,
            verbosity=str(context.narrator_verbosity),
        ):
            pass
        registry.register_section(
            agent_name,
            PromptSection.new(
                "narrator_verbosity",
                _build_verbosity_section(context.narrator_verbosity, cap_sentences, cap_chars),
                AttentionZone.Recency,
                SectionCategory.Guardrail,
            ),
        )

        # Opening scene constraint (Recency zone, turn 0 only — ADR-098)
        if context.turn_number == 0:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "opening_scene_constraint",
                    (
                        "<opening-scene>\n"
                        "This is the OPENING SCENE — the player's first moment in the world.\n"
                        "Set the scene in 3-4 SHORT paragraphs maximum:\n"
                        "1. Where they are (one vivid detail, not a catalogue).\n"
                        "2. What's immediately happening around them.\n"
                        "3. One sensory hook — sound, smell, weather.\n"
                        "4. End with a prompt for their first action (a question, a choice, a threat).\n"
                        "Do NOT write a novel opening. Do NOT describe the world's history. "
                        "Do NOT list every feature of the environment. Drop the player IN and "
                        "let them explore. Under 500 characters of prose total.\n"
                        "MANDATORY: Your game_patch MUST include a visual_scene for this opening "
                        "turn — it is the first illustration the player sees. Use tier "
                        '"landscape" and describe the opening vista.\n'
                        "</opening-scene>"
                    ),
                    AttentionZone.Recency,
                    SectionCategory.Guardrail,
                ),
            )

        # NPC introduction visual constraint (Recency zone, every turn).
        # Playtest 2026-05-03 [BUG] — render policy fired NPC_INTRO for two
        # newly auto-registered NPCs (Inspector Volkova, Drilled door clerk)
        # but the narrator emitted no visual_scene for either, so
        # ``render.eligible_no_subject reason=npc_intro`` warned and the
        # render dispatcher bailed. Two named NPCs introduced in detail with
        # zero portrait or scene image — exactly the OTEL-lie-detector
        # pattern from CLAUDE.md (the prose surface and the visual surface
        # disagreed on whether anything happened).
        #
        # This constraint runs EVERY turn (no ``is_full`` gate) because new
        # NPCs can appear on any turn, not just the opening. The Diamonds
        # and Coal principle (ADR-014) treats first-introduction prose as a
        # diamond — the visual is part of that diamond, not optional. The
        # render trigger policy (server/render_trigger.py) already
        # classifies this turn as NPC_INTRO whenever any NpcMention has
        # ``is_new=True``; this section makes the narrator hold up its end
        # of the contract by always including the matching visual_scene.
        #
        # ADR-111 (story 57-4): backend-gated. On the SDK tool-use path the
        # migration target is the slimmed-sidecar Primacy/Stable cached
        # prose at ``narrator_prompts/output_only_sdk.md`` (cached on every
        # turn). On the legacy ``claude -p`` path the Recency-zone
        # registration stays — that backend cannot host tool descriptions.
        self._maybe_register_legacy_guardrail(
            registry,
            agent_name,
            "npc_intro_visual_constraint",
            NPC_INTRO_VISUAL_CONSTRAINT,
        )

        # Plot-a-course (plot-a-course design). The narrator can plot a
        # course to any body in the prompted set; rejection is OTEL-loud
        # and chart-silent. Block is omitted entirely when the world has
        # no orbital tier or the party has no body anchor.
        if context.orbital_content is not None and context.party_body_id:
            from sidequest.orbital.course import (
                _bodies_in_scope,
                compute_courses,
                format_courses_block,
            )

            in_scope = _bodies_in_scope(
                context.orbital_content.orbits,
                context.orbital_scope,
            )
            course_rows = compute_courses(
                orbits=context.orbital_content.orbits,
                party_at=context.party_body_id,
                in_scope_body_ids=in_scope,
                recent_body_mentions=list(context.recent_body_mentions),
                quest_anchors=list(context.quest_anchors),
            )
            from sidequest.telemetry.spans.course import emit_course_compute

            in_scope_n = sum(1 for r in course_rows.values() if r.source.value == "in_scope")
            recent_n = sum(1 for r in course_rows.values() if r.source.value == "recent_mention")
            quest_n = sum(1 for r in course_rows.values() if r.source.value == "quest_objective")
            emit_course_compute(
                course_count=len(course_rows),
                in_scope=in_scope_n,
                recent=recent_n,
                quest=quest_n,
                dropped_by_cap=0,  # cap-counted in compute_courses if we extend the API
            )
            block_text = format_courses_block(course_rows)
            if block_text:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "courses",
                        block_text,
                        AttentionZone.Recency,
                        SectionCategory.Guardrail,
                    ),
                )

        # Pingpong 2026-05-03 [BUG] — narrator wrote a textbook chase-firing
        # beat ("patrol cutter spinning her reactor up from cold-soak. She
        # isn't moving yet. She's asking the tower whether to.") but the
        # game_patch carried ``confrontation=None`` — no encounter was
        # instantiated. The schema-block instruction in narrator.py:188-201
        # says "MUST emit confrontation" on trigger events, but lives deep
        # in the System zone where attention has decayed by turn 20.
        # Same disease as ``npc_intro_visual_constraint`` above; same cure:
        # restate the rule per-turn in Recency-zone Guardrail attention.
        # The lie-detector is now the ``confrontation.unengaged_turn`` OTEL
        # span (Story 59-1, narration_apply) — it fires when the narrator names
        # an opponent but engages nothing and emits no intent; together they
        # close the gap without server-side auto-firing (a silent fallback).
        # (The legacy keyword scanner ``_scan_for_confrontation_trigger_keywords``
        # was deleted in the Epic-50 declared-intent migration.)
        #
        # ADR-111 (story 57-4): backend-gated. On the SDK tool-use path the
        # migration target is the ``begin_confrontation`` tool description,
        # cached as part of the tools=array root. On the legacy ``claude -p``
        # path the Recency-zone registration stays.

        # Spec 2026-05-20 — soft_suggest directives from the prior turn.
        # Consumed + cleared in one shot (same pattern as time_skip_block).
        # Only fires when snapshot is available (production path); ignored on
        # legacy fixture paths that never went through _build_turn_context.
        if snapshot is not None:
            _intent_directive_block = _consume_next_turn_directives(snapshot)
            if _intent_directive_block:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "intent_directives",
                        # Header hardening (sq-playtest 2026-06-13 directive-leak):
                        # these notes are INTERNAL scaffolding. The narrator must
                        # apply their content to the prose but never surface the
                        # machinery — no quoting the note, no naming "directives",
                        # no citing dice rolls or AC. Paired with dropping
                        # next_turn_directives from the <game_state> JSON so the
                        # raw field name can no longer be echoed either.
                        "GM-NOTE (internal — apply silently, never quote): "
                        "mechanical facts and intent notes from the previous turn. "
                        "Weave their substance into your narration, but do NOT "
                        "mention this note, do NOT say 'directive(s)', and do NOT "
                        "cite dice rolls, to-hit, or AC in the player-facing prose. "
                        "You were NOT given any die result this turn, so NEVER write "
                        "'the roll of N', 'you rolled N', 'a roll of N', 'd20', "
                        "'vs AC', or any number-bearing mechanic — any such number is "
                        "fabricated. Narrate only the fictional outcome (the blow "
                        "lands or goes wide), never a mechanical summary line:"
                        f"\n{_intent_directive_block}",
                        AttentionZone.Recency,
                        SectionCategory.Guardrail,
                    ),
                )

        # Spec 2026-05-20 — per-call directive from the reprompt loop.
        # Set when run_narration_turn was invoked with extra_directive=... ;
        # injects a recency-zone guardrail telling the narrator what to
        # fix from the first attempt.
        if context.extra_directive:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "reprompt_directive",
                    f"REPROMPT DIRECTIVE: {context.extra_directive}",
                    AttentionZone.Recency,
                    SectionCategory.Guardrail,
                ),
            )

        self._maybe_register_legacy_guardrail(
            registry,
            agent_name,
            "confrontation_trigger_constraint",
            CONFRONTATION_TRIGGER_CONSTRAINT,
        )
        # Story 49-2 — NPC extraction constraint (Recency zone Guardrail).
        # Paired with the server-side prose-only auto-minter
        # (sidequest.server.session_helpers._auto_mint_prose_only_npcs).
        # 2026-05-11 Glenross narrator wrote dialogue about Father in
        # detail ("He's through the back passage", "Mrs. Gow laid him
        # after", "set the secateurs down on the blotter") but emitted
        # npcs_present covering only Reverend Murchison + the pinafore
        # girl. Father lived only in prose. Turn 6 then invented "the
        # wee one's mother / her" with no roster constraint to refuse.
        #
        # The extraction rule lives in the System-zone schema block but
        # attention has decayed there by turn 20+. Same disease as
        # confrontation_trigger_constraint above; same cure: restate the
        # rule per-turn in Recency-zone Guardrail attention. The
        # server-side auto-minter is the post-hoc safety net; this
        # section is the narration-time prevention.
        #
        # ADR-111 (story 57-4): backend-gated. SDK path migration target
        # is the slimmed-sidecar Primacy/Stable prose (cached); legacy
        # path keeps the Recency-zone registration.
        self._maybe_register_legacy_guardrail(
            registry,
            agent_name,
            "npc_extraction_constraint",
            NPC_EXTRACTION_CONSTRAINT,
        )

        # Story 49-3 — location-patch constraint (Recency zone Guardrail).
        # Paired with the server-side drift-repair backstop in
        # ``sidequest.server.narration_apply._apply_narration_result_to_snapshot``
        # which auto-promotes a leading ``**Room Title**`` into
        # ``character_locations`` and emits the
        # ``narrator.location_drift_repaired`` span.
        #
        # 2026-05-11 Glenross: across five turns the narrator wrote
        # ``**The Bee Garden** → **The Manse Garden** → **Front Parlour**
        # → **Study** → **Sickroom Passage**`` as room headers while
        # ``character_locations[Ziggy]='the_manse'`` lagged the prose
        # because ``game_patch.location`` was empty on turns 2-5
        # (has_location=False). SOUL.md "Illusionism": narrator and
        # state on different tracks, GM panel blind to actual position.
        # Same disease as the confrontation_trigger / npc_extraction
        # guardrails above; same cure: a Recency-zone restatement so
        # the rule lives in high-attention space every turn.
        #
        # ADR-111 (story 57-4): backend-gated. SDK path migration target
        # is the ``apply_world_patch`` tool's ``description`` field
        # (cached as part of the tools=array root); legacy path keeps
        # the Recency-zone registration.
        self._maybe_register_legacy_guardrail(
            registry,
            agent_name,
            "location_patch_constraint",
            LOCATION_PATCH_CONSTRAINT,
        )

        # ADR-111 §Observability — emit the migration cutover span so the
        # GM panel can verify on every turn that the Recency-zone
        # registrations are still being skipped. Constant-emit shape: the
        # span fires on every prompt-build so absence-of-span is unambiguous
        # (= the migration call site is missing entirely). Post-61-9 the
        # ``tool_backend`` attribute is gone (no longer carries information
        # — only SDK is wired); ``GUARDRAIL_NAMES`` and ``TOTAL_PROSE_BYTES``
        # are hard-wired constants derived from the static ``ALL_GUARDRAILS``
        # tuple at module load.
        from sidequest.telemetry.spans.span import Span as _GuardrailSpan

        with _GuardrailSpan.open(
            "narrator.recency_guardrails_skipped",
            {
                "guardrails_skipped": GUARDRAIL_NAMES,
                "bytes_saved": TOTAL_PROSE_BYTES,
            },
        ):
            pass

        # Recent-narrative window (Recency zone, Story 49-1).
        # ADR-098 dropped --resume; the narrator lost its conversational
        # history because narrative_log lived in the Valley-zone game_state
        # JSON dump. This section restores the last K (default 4) entries
        # as readable prose in high-attention Recency — alongside
        # player_action — so turn-N prose stays consistent with
        # turn-(N-1) (2026-05-11 Glenross gender flip, secateurs-set-
        # down-twice).
        #
        # K is a CAP, not a floor: any non-empty window registers the
        # section with all available entries. Turns 1-3 of a fresh save
        # are exactly the scenario this story exists to fix — gating on
        # >=K would re-create the regression on the early turns.
        #
        # Per-entry byte cap (RECENT_NARRATIVE_PER_ENTRY_CAP) bounds a
        # single verbose narrator turn so it cannot eat the prompt
        # budget on its own (Reviewer reproduced 40kB / 72kB shapes from
        # 4×10kB entries — ADR-009 attention-zone collision with Late-
        # zone Format guardrails). Truncated entries carry
        # RECENT_NARRATIVE_TRUNCATION_MARKER so the cut is visible on
        # the GM panel and in saved prompts.
        #
        # The span fires on EVERY narrator turn (including empty-log
        # case with turn_count=0/total_tokens=0) so the GM panel can
        # distinguish "injector engaged with nothing to inject" from
        # "injector not wired" — matches the room.state_injected no-op-
        # fire discipline. Truth invariant: section registered IFF
        # (turn_count > 0 AND total_tokens > 0).
        _recent_window = list(context.recent_narrative_log)[-RECENT_NARRATIVE_WINDOW_K:]
        if _recent_window:
            _recent_body = _render_recent_narrative_window(_recent_window)
            _recent_turn_count = len(_recent_window)
            _recent_total_tokens = max(1, len(_recent_body) // 4)
        else:
            _recent_body = ""
            _recent_turn_count = 0
            _recent_total_tokens = 0
        with recent_narrative_context_injected_span(
            turn_count=_recent_turn_count,
            total_tokens=_recent_total_tokens,
        ):
            logger.info(
                "orchestrator.recent_narrative_context_injected turn_count=%d total_tokens=%d",
                _recent_turn_count,
                _recent_total_tokens,
            )
        if _recent_body:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "recent_narrative_context",
                    _recent_body,
                    AttentionZone.Recency,
                    SectionCategory.State,
                ),
            )

        # Narrator vocabulary (Late zone) — ADR-098: fires every turn
        registry.register_section(
            agent_name,
            PromptSection.new(
                "narrator_vocabulary",
                _build_vocabulary_section(context.narrator_vocabulary),
                AttentionZone.Late,
                SectionCategory.Format,
            ),
        )

        # PacingHint (Late zone, every tier — combat pacing can change
        # mid-session, so per-turn dynamic state must reach Delta tier too).
        # Rust parity: sidequest-agents/src/prompt_framework/mod.rs:89
        # ``register_pacing_section`` filters to PACING_AGENTS = ["narrator"]
        # internally; safe to call unconditionally when a hint is set.
        if context.pacing_hint is not None:
            hint = context.pacing_hint
            registry.register_pacing_section(
                agent_name,
                hint.narrator_directive(),
                hint.escalation_beat,
            )

        # Group B — Local DM decomposer narrator_directives (Recency zone).
        # When the decomposer ran, run its dispatch bank here and inject the
        # aggregated directives as a high-attention section so they land just
        # before the player action (load-bearing, not ambient context).
        # Group G Task 5: ``visible_dispatch_package`` is the redacted view
        # computed at the top of this method — entries flagged
        # ``redact_from_narrator_canonical`` are already gone.
        if visible_dispatch_package is not None:
            # The dispatch bank already ran ONCE in the pre-narrator pass
            # (``intent_router_pass``), engaging every engine on the snapshot
            # with a complete context. Re-running it here would engage each
            # engine a SECOND time (double-dispatch — a PC moves twice, a clue
            # is consumed twice), so consume the stashed ``BankResult`` instead.
            bank_result = context.bank_result
            if bank_result is None:
                # Invariant: a present dispatch_package means the pre-narrator
                # pass ran and stashed its BankResult. None here is a wiring
                # break, not an expected state — fail loud (No Silent Fallbacks).
                raise RuntimeError(
                    "build_narrator_prompt: dispatch_package present but "
                    "context.bank_result is None — pre-narrator pass wiring missing"
                )

            # Group C — lethality arbitration runs after the bank and before
            # the narrator_directives section is registered, so the arbiter's
            # paired must_narrate / must_not_narrate directives join the bank
            # directives in the same high-attention block. Determinism is
            # the point: the arbiter decides what verdict fires, the narrator
            # only decides how to describe it (spec §4.1).
            arbiter_directives: list[NarratorDirective] = []
            if context.lethality_policy is not None:
                with context.phase_timings.phase("lethality_arbiter"):
                    from sidequest.agents.lethality_arbiter import LethalityArbiter

                    arbiter = LethalityArbiter(policy=context.lethality_policy)
                    l_result = arbiter.arbitrate(
                        package=visible_dispatch_package,
                        bank_result=bank_result,
                        pc_cores_by_player=context.pc_cores_by_player,
                        npc_cores_by_name=context.npc_cores_by_name,
                    )
                    arbiter_directives = l_result.directives

            with context.phase_timings.phase("prompt_build"):
                # The bank ran on the FULL package; strip directives whose
                # visibility is redacted-from-narrator-canonical so the
                # narrator prompt never sees a secret dispatch's directive (MP
                # perception firewall, ADR-105). Mirrors redact_dispatch_package
                # by the same visibility flag, at directive granularity — and
                # uniformly covers subsystem-output directives, confidence-gate
                # degraded hints, and decomposer narrator_instructions.
                visible_bank_directives = [
                    d
                    for d in bank_result.directives
                    if not d.visibility.redact_from_narrator_canonical
                ]
                combined_directives = visible_bank_directives + arbiter_directives
                # Render via the single player-safe boundary: in-fiction
                # imperatives + framing, NEVER the raw NarratorDirectiveKind token
                # (the 2026-06-19 must_not_narrate leak — see narrator_directives).
                block = render_narrator_directives(combined_directives)
                if block:
                    registry.register_section(
                        agent_name,
                        PromptSection.new(
                            "narrator_directives",
                            block,
                            AttentionZone.Recency,
                            SectionCategory.State,
                        ),
                    )
                for key, err in bank_result.errors:
                    logger.warning(
                        "orchestrator.subsystem_error key=%s error=%s",
                        key,
                        err,
                    )

        with context.phase_timings.phase("prompt_build"):
            # Player action (Recency zone — highest attention, every tier)
            if context.merged_player_actions:
                # Multiplayer merged turn (ADR-036 sealed-letter dispatch).
                # Render every PC's declaration on its own line and reiterate
                # the agency rule inline so the LLM sees it adjacent to the
                # action block. Without this, the prior "Laverne says: ..."
                # framing wrapped the whole merged blob in one PC's name and
                # cued the model to generate dialogue for every PC named in
                # the block (2026-04-29 playtest: "Your call, Engineer,"
                # Laverne says — Laverne's player only typed "I look at
                # Shirley", a glance).
                lines = "\n".join(
                    f"- {name} declares: {act}" for name, act in context.merged_player_actions
                )
                player_action_text = (
                    "This turn, the seated players each declared an action "
                    "simultaneously. Resolve them as a single narrative beat:\n"
                    f"{lines}\n\n"
                    "STRICT: Narrate the resolution of these declared actions "
                    "ONLY. Do NOT generate dialogue, internal thoughts, "
                    "decisions, or new physical actions for any PC listed "
                    "above — only what their player declared. NPCs may speak "
                    "and react. PCs may not be made to speak."
                )
            elif context.opening_seed_shown:
                # Pingpong 2026-06-05 [BAR-1]: the action on a seeded opening
                # turn is the authored first_turn_invitation, ALREADY emitted
                # to the player verbatim by the cold-open path. Framing it as
                # `"<PC> says: <invitation>"` told the narrator the player
                # spoke that prose, and the action-rewrite contract dutifully
                # novelized it back — every seeded opening doubled its prose
                # (barsoom MP turn 1: seed + near-verbatim restatement). Keep
                # the invitation in recency for continuity, but mark it as
                # already-displayed authored prose, not player input.
                player_action_text = (
                    "OPENING TURN. The authored invitation below has ALREADY "
                    "been shown to the player verbatim — do NOT repeat, "
                    "restate, or paraphrase any sentence of it. Begin your "
                    "narration at the moment it ends and move the scene "
                    "forward. The player has not yet acted; do not invent "
                    "actions or dialogue for them.\n"
                    "<already-shown-invitation>\n"
                    f"{action}\n"
                    "</already-shown-invitation>"
                )
            else:
                player_action_text = f"{context.character_name} says: {action}"
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "player_action",
                    player_action_text,
                    AttentionZone.Recency,
                    SectionCategory.Action,
                ),
            )

            prompt_text = registry.compose(agent_name)
            section_count = len(registry.registry(agent_name))
            logger.info(
                "turn.agent_llm.prompt_build section_count=%d",
                section_count,
            )
            # Dashboard Prompt tab consumes `prompt_assembled`. The build-time
            # emission carries the Zone Breakdown + cache attribution that is
            # knowable WITHOUT an API call; `cache_usage` is None here (no SDK
            # response yet — shown as "n/a" loudly per No-Silent-Fallbacks).
            # On the SDK path (ToolingLlmClient → _run_narration_turn_sdk) this
            # build-time emit is suppressed: that path re-emits ONE enriched
            # event post-call with real usage + block digests, so the GM panel
            # gets a single coherent entry per turn rather than two.
            if not isinstance(self._client, ToolingLlmClient):
                from sidequest.telemetry.watcher_hub import publish_event as _pub

                payload = self._build_prompt_event_payload(
                    agent_name=agent_name,
                    context=context,
                    prompt_text=prompt_text,
                    section_count=section_count,
                    sections=registry.registry(agent_name),
                )
                payload["cache_usage"] = None
                _pub("prompt_assembled", payload, component="prompt_builder")
        return prompt_text, registry

    def _build_prompt_event_payload(
        self,
        *,
        agent_name: str,
        context: TurnContext,
        prompt_text: str,
        section_count: int,
        sections: list[PromptSection],
    ) -> dict[str, Any]:
        """Shared base payload for the ``prompt_assembled`` event.

        Used by both the build-time emission (above) and the SDK post-call
        emission (``_run_narration_turn_sdk``) so the GM panel sees one
        consistent shape. The returned dict contains: agent_name, agent,
        turn_number, section_count, prompt_len, system_len, user_len, bounded,
        total_tokens, zones. ``cache_usage`` (always) and ``cache_blocks`` (SDK
        path only) are NOT included — callers must set them before publishing.
        The PascalCase zone names match the dashboard's ZONE_COLORS map;
        ``agent`` aliases ``agent_name`` for pre-fix consumers (playtest
        2026-04-30 #1A).
        """
        from sidequest.agents.prompt_framework.bucket import (
            SectionBucket,
            default_bucket_for_section,
        )

        system_chars = sum(
            len(s.content)
            for s in sections
            if not s.is_empty() and default_bucket_for_section(s.name) == SectionBucket.System
        )
        user_chars = sum(
            len(s.content)
            for s in sections
            if not s.is_empty() and default_bucket_for_section(s.name) == SectionBucket.User
        )
        return {
            "agent_name": agent_name,
            "agent": agent_name,
            "turn_number": context.turn_number,
            "section_count": section_count,
            "prompt_len": len(prompt_text),
            "system_len": system_chars,
            "user_len": user_chars,
            "bounded": True,
            "total_tokens": max(1, len(prompt_text) // 4),
            "zones": _compute_zones_payload(sections),
        }

    # ------------------------------------------------------------------
    # Main turn entrypoint
    # ------------------------------------------------------------------

    async def run_narration_turn(
        self,
        action: str,
        context: TurnContext,
        *,
        extra_directive: str | None = None,
    ) -> NarrationTurnResult:
        """Process a player action through the Phase 1 narration pipeline.

        Routes to the SDK tool-loop path when the client is tooling-capable,
        otherwise delegates to the synchronous path. Narration is delivered
        complete-only — narrator-text streaming was removed (2026-06-07).

        Args:
            action: Raw player input text.
            context: Turn context (world state, genre prompts, etc.).
            extra_directive: Per-call reprompt directive injected by the
                  reprompt loop (spec 2026-05-20 step 7). When set, the
                  directive is stored on context so build_narrator_prompt
                  registers it as a Recency-zone guardrail. None on the
                  normal first-call path.
        """
        # Spec 2026-05-20 step 7: thread extra_directive into context so
        # build_narrator_prompt can register it as a Recency-zone guardrail.
        # TurnContext is per-turn-scoped (not reused), so mutation is safe.
        if extra_directive is not None:
            context.extra_directive = extra_directive

        # Phase D Task 1: when the configured client is a tooling-capable
        # LLM (AnthropicSdkClient — the ADR-101 default), route through
        # complete_with_tools so the 26-tool registry is exposed to the
        # model. Non-tooling clients (claude -p ClaudeClient, Ollama) take
        # the synchronous path. Narrator-text streaming was removed entirely
        # (playtest 2026-06-07, operator-directed): narration is delivered
        # complete-only — no partial-narration chunks ride the WebSocket.
        if isinstance(self._client, ToolingLlmClient):
            result = await self._run_narration_turn_sdk(action, context)
        else:
            result = await self._run_narration_turn_synchronous(action, context)
        # sq-playtest 2026-06-19 BLOCKER — empty player-facing prose must never
        # present as a clean success. An empty narration was persisted with
        # content='' + is_degraded=False (a turn marked complete + clean with
        # nothing to render), hanging the client on "narrator is thinking" with
        # no recovery. Trip the degraded stall + emit the GM-panel lie detector.
        # Runs BEFORE the fabricated-roll repair: the substituted stall carries
        # no roll numbers, so that pass is a no-op on a guarded turn.
        result = self._guard_empty_narration(action=action, result=result)
        # sq-playtest 2026-06-13 — fabricated-roll lie detector + prose-only
        # repair. The narrator never sees server-rolled dice (reprisals,
        # opposed NPC checks), so any roll/AC NUMBER it printed when no
        # dice tool fired this turn is an invented mechanic. Emit the
        # narrator.fabricated_roll span (GM panel) and launder the prose via
        # a toolless rewrite so the player never reads the fabrication.
        return await self._maybe_repair_fabricated_roll(action=action, result=result)

    def _guard_empty_narration(
        self,
        *,
        action: str,
        result: NarrationTurnResult,
    ) -> NarrationTurnResult:
        """Empty player-facing prose trips the degraded stall, never a blank success.

        sq-playtest 2026-06-19 (BLOCKER-CRITICAL, Oz turns 14/15): a narrator
        turn whose prose slot came back empty was persisted with ``content=''``
        and ``is_degraded=False`` — marked ``session.narration_complete``,
        ``degraded=False``. The client hung on "narrator is thinking" with no
        recovery; resubmitting the same phrasing reproduced the empty result.

        An empty narration is an unrecoverable-narrator outcome for this turn and
        is handled like every other one (cf. :meth:`_degraded_result`): flag it
        degraded and substitute the in-fiction stall so the surface stays honest
        AND renderable (the durable narrative log, the NARRATION message, and the
        TurnRecord all read ``result.narration`` downstream of here — fixing it at
        this single point covers every consumer). The turn's mechanical side
        effects are preserved: the WRITE tools already applied + saved state during
        dispatch, so only ``narration``/``is_degraded`` are touched (mirroring how
        :meth:`_maybe_repair_fabricated_roll` mutates ``narration`` in place).

        Emits the ``narrator.empty_narration`` OTEL span (GM-panel lie detector,
        CLAUDE.md OTEL principle) so an empty turn is auditable and distinct from a
        deliberate quiet beat — which carries real prose and never reaches here.

        Diagnosing WHY the prose came back empty (tool-only response / prose in the
        wrong field) is the separately-tracked defect #2; this guard owns the
        player-facing harm (the hang), not the root cause.
        """
        from sidequest.telemetry.spans.span import Span

        if (result.narration or "").strip():
            return result  # real prose — clean turn, no-op

        with Span.open(
            "narrator.empty_narration",
            {
                "action": action[:120],
                "raw_len": len(result.raw_response_text or ""),
                "tool_calls": len(result.tool_calls or []),
                "agent": result.agent_name or self._narrator.name(),
            },
        ):
            pass
        logger.warning(
            "narrator.empty_narration action=%r raw_len=%d tool_calls=%d — narrator "
            "returned empty player-facing prose; tripping the degraded stall (was "
            "persisting content='' as a clean success and hanging the client)",
            action,
            len(result.raw_response_text or ""),
            len(result.tool_calls or []),
        )

        result.narration = _EMPTY_NARRATION_STALL
        result.is_degraded = True
        return result

    async def _maybe_repair_fabricated_roll(
        self,
        *,
        action: str,
        result: NarrationTurnResult,
    ) -> NarrationTurnResult:
        """Detect + repair fabricated dice/AC numbers in finished narration.

        The narrator never sees server-rolled dice; a roll/AC number printed
        when no ``roll_dice`` tool fired this turn is a fabricated mechanic
        ("the worst lie the narrator can tell"). On a hit, emit the
        ``narrator.fabricated_roll`` OTEL span — the GM-panel lie detector —
        and, on the tooling path, reprompt a TOOLLESS prose-only rewrite
        (``tools=[]`` so it cannot re-run WRITE tools / double-apply state)
        that launders the invented mechanic out while preserving the fiction.
        """
        from sidequest.agents.fabricated_roll_guard import (
            ROLL_TOOL_NAMES,
            detect_fabricated_roll,
        )
        from sidequest.telemetry.spans.span import Span

        narration = result.narration or ""
        roll_tool_fired = any(
            (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) in ROLL_TOOL_NAMES
            for tc in (result.tool_calls or [])
        )
        matched = detect_fabricated_roll(narration, roll_tool_fired=roll_tool_fired)
        if matched is None:
            return result

        repaired_text: str | None = None
        if isinstance(self._client, ToolingLlmClient):
            try:
                repaired_text = await self._rewrite_prose_without_fabricated_roll(narration)
            except Exception:  # noqa: BLE001 — repair is best-effort; never fail the turn
                logger.warning(
                    "narrator.fabricated_roll repair_failed matched=%r", matched, exc_info=True
                )
                repaired_text = None
            # Only accept a rewrite that is actually clean.
            if repaired_text and detect_fabricated_roll(repaired_text, roll_tool_fired=False):
                logger.warning("narrator.fabricated_roll repair_still_dirty matched=%r", matched)
                repaired_text = None

        repaired = repaired_text is not None
        with Span.open(
            "narrator.fabricated_roll",
            {
                "matched": matched[:120],
                "repaired": repaired,
                "roll_tool_fired": roll_tool_fired,
                "action": action[:120],
            },
        ):
            pass
        logger.warning(
            "narrator.fabricated_roll matched=%r repaired=%s — narrator printed a "
            "die/AC number with no dice tool this turn",
            matched,
            repaired,
        )
        if repaired and repaired_text is not None:
            result.narration = repaired_text
        return result

    async def _rewrite_prose_without_fabricated_roll(self, narration: str) -> str | None:
        """Toolless rewrite pass that strips invented roll/AC numbers.

        Runs through the production tooling client with ``tools=[]`` so it makes
        a plain completion — no WRITE tools, no state mutation, no double-apply.
        Returns the cleaned prose, or None if the model returned nothing.
        """
        from sidequest.agents.fabricated_roll_guard import FABRICATED_ROLL_REWRITE_SYSTEM
        from sidequest.agents.model_routing import CallType, resolve_model
        from sidequest.agents.tooling_protocol import CacheableBlock, Message

        if not isinstance(self._client, ToolingLlmClient):
            return None
        rewrite = await self._client.complete_with_tools(
            system_blocks=[CacheableBlock(text=FABRICATED_ROLL_REWRITE_SYSTEM, cache=False)],
            messages=[Message(role="user", content=f"Narration to clean:\n\n{narration}")],
            tools=[],
            model=resolve_model(CallType.SCRATCH),
            caller="fabricated_roll_repair",
        )
        cleaned = (rewrite.text or "").strip()
        return cleaned or None

    async def _invoke_with_retry_once(
        self,
        *,
        system_prompt: str,
        user_message: str,
        phase_timings,
    ) -> tuple[ClaudeResponse | None, int]:
        """Send via send_stateless; retry once on transient failure (ADR-098 §Error handling).

        Returns (response, elapsed_ms). On unrecoverable failure returns
        (None, elapsed_ms) — caller renders the degraded in-fiction stall.
        """
        # _invoke_with_retry_once is only reached on the synchronous
        # LlmClient path, never the SDK path. The assert pins that
        # invariant for pyright and fails loudly if it's ever violated.
        # We assert "not Tooling" instead of "is LlmClient" because test
        # doubles (AsyncMock) don't satisfy an isinstance LlmClient check.
        assert not isinstance(self._client, ToolingLlmClient), (
            f"synchronous path must not see a ToolingLlmClient, got {type(self._client).__name__}"
        )
        client: LlmClient = self._client  # type: ignore[assignment]
        with turn_agent_llm_inference_span(
            model=NARRATOR_MODEL,
            prompt_len=len(system_prompt) + len(user_message),
        ):
            for attempt in (1, 2):
                call_start = time.monotonic()
                try:
                    with phase_timings.phase("narrator_subprocess"):
                        response = await client.send_stateless(
                            system_prompt=system_prompt,
                            user_message=user_message,
                            model=NARRATOR_MODEL,
                            allowed_tools=[],
                            env_vars={},
                        )
                    elapsed_ms = int((time.monotonic() - call_start) * 1000)
                    return response, elapsed_ms
                except _ClaudeTimeoutError as e:
                    elapsed_ms = int((time.monotonic() - call_start) * 1000)
                    if attempt == 1:
                        logger.warning(
                            "narrator.transient_retry attempt=%d duration_ms=%d error=%s",
                            attempt,
                            elapsed_ms,
                            e,
                        )
                        continue  # retry
                    logger.error("narrator.unrecoverable error=%s after retry", e)
                    return None, elapsed_ms
                except Exception as e:  # noqa: BLE001 - degraded fallback path
                    elapsed_ms = int((time.monotonic() - call_start) * 1000)
                    logger.error("narrator.unrecoverable error=%s", e)
                    return None, elapsed_ms
            raise AssertionError(
                "_invoke_with_retry_once: loop exhausted without return — should be unreachable"
            )

    def _check_oversized_prompt(
        self,
        system_prompt: str,
        user_message: str,
        registry: PromptRegistry,
        agent_name: str,
    ) -> bool:
        """Hard-cap canary (Story 61-3 — promotes ADR-098 §Bound canary).

        When ``len(system_prompt) + len(user_message) > PROMPT_BUDGET_BYTES_HARD``
        (~2 MB ≈ 500K tokens, half of Opus 4.8's 1M window), refuses the
        narrator turn. The 2026-05-23 incident burned $313 in 48h while a
        SOFT warning scrolled past unread overnight; the hard refuse stops
        the SDK call from billing and the LOUD emit pages the operator.

        Loudness contract (locked design B):

        * ``logger.error`` (was ``logger.warning``) — red-band log filters
          surface this during long sessions.
        * Watcher event ``prompt_oversized_hard`` with ``severity="error"``
          — GM panel can red-band filter on a stable event name.
        * Exactly one emit per refused turn (callers must not loop).

        :returns: ``True`` when the prompt exceeds the budget and the caller
            MUST refuse the SDK call. ``False`` when the prompt is within
            budget and the caller should proceed normally. Control flow stays
            in the caller — this method does not raise.
        """
        total = len(system_prompt) + len(user_message)
        if total <= PROMPT_BUDGET_BYTES_HARD:
            return False
        from sidequest.telemetry.watcher_hub import publish_event as _pub

        breakdown = [
            {"name": s.name, "chars": len(s.content)} for s in registry.registry(agent_name)
        ]
        logger.error(
            "narrator.prompt_oversized total_bytes=%d budget=%d sections=%d action=refuse",
            total,
            PROMPT_BUDGET_BYTES_HARD,
            len(breakdown),
        )
        _pub(
            "prompt_oversized_hard",
            {
                "total_bytes": total,
                "budget": PROMPT_BUDGET_BYTES_HARD,
                "sections": breakdown,
                "action": "refuse",
            },
            component="orchestrator",
            severity="error",
        )
        return True

    def _degraded_result(
        self,
        *,
        action: str,
        context: TurnContext,
        narration: str = "The world holds its breath.",
    ) -> NarrationTurnResult:
        """Render the in-fiction stall on unrecoverable narrator failure.

        :param narration: Override the default in-fiction stall text. Story
            61-3 uses a distinct line (``"[narrator-overload — operator paged]"``)
            on budget-refuse so session-recording grep can distinguish
            budget-refuse from SDK-error-refuse.
        """
        return NarrationTurnResult(
            narration=narration,
            is_degraded=True,
            agent_name=self._narrator.name(),
        )

    def _assemble_turn_result(
        self,
        *,
        response: ClaudeResponse,
        prompt_text: str,
        context: TurnContext,
        elapsed_ms: int,
        action: str,
    ) -> NarrationTurnResult:
        """Parse the narrator response into a NarrationTurnResult.

        Mechanical lift from the pre-refactor _run_narration_turn_synchronous body.
        The session-id storage block is intentionally NOT lifted — sessions are gone (ADR-098).
        """
        raw_response = response.text
        logger.info(
            "Claude CLI returned narration len=%d duration_ms=%d",
            len(raw_response),
            elapsed_ms,
        )

        with context.phase_timings.phase("narrator_extraction"):
            extraction = extract_structured_from_response(raw_response)

        shared = self._presentation_and_untooled_fields(
            extraction=extraction,
            raw_response=raw_response,
            context=context,
            elapsed_ms=elapsed_ms,
            prompt_text=prompt_text,
            token_count_in=response.input_tokens,
            token_count_out=response.output_tokens,
        )

        # Non-SDK-only observability — these log lines belong to the
        # sync sidecar path (the SDK path's mechanics are
        # tool-driven, so the tools' own spans carry the equivalent).
        if extraction["confrontation"]:
            logger.info(
                "encounter.confrontation_initiated confrontation_type=%s",
                extraction["confrontation"],
            )

        for bs_dict in extraction["beat_selections"]:
            if isinstance(bs_dict, dict):
                logger.info(
                    "encounter.agent_beat_selection actor=%s beat_id=%s target=%r",
                    bs_dict.get("actor"),
                    bs_dict.get("beat_id"),
                    bs_dict.get("target"),
                )

        beat_selections = [
            BeatSelection.from_dict(d) for d in extraction["beat_selections"] if isinstance(d, dict)
        ]

        # The non-SDK path is the SINGLE applier (no tool ran during its
        # dispatch), so it carries the tool-owned categories from the
        # sidecar in addition to the shared presentation/untooled fields.
        return NarrationTurnResult(
            **shared,
            location=extraction["location"],
            confrontation=extraction["confrontation"],
            beat_selections=beat_selections,
            affinity_progress=extraction["affinity_progress"],
            status_changes=extraction["status_changes"]
            if isinstance(extraction["status_changes"], list)
            else [],
            magic_working=(
                extraction["magic_working"]
                if isinstance(extraction.get("magic_working"), dict)
                else None
            ),
            days_advanced=extraction.get("days_advanced", 0),
            game_patch_dict=_extract_game_patch_json(raw_response),
        )

    def _presentation_and_untooled_fields(
        self,
        *,
        extraction: dict[str, Any],
        raw_response: str,
        context: TurnContext,
        elapsed_ms: int,
        prompt_text: str,
        token_count_in: int | None,
        token_count_out: int | None,
    ) -> dict[str, Any]:
        """Build the NarrationTurnResult kwargs shared by BOTH assemblers.

        Covers the fields that are sidecar-sourced on every path:
        presentation/signal fields with no successor tool (scene_mood,
        visual_scene, npcs_present, footnotes, sfx_triggers),
        the no-successor-tool state lanes (items_*,
        gold_change, lore_established, companions_*), and the
        agent/token/prompt/raw/secret telemetry tail. Performs the
        canonical-prose leak audit when a dispatch package is present.

        Story 151-3 (ADR-150 step 3): ``action_rewrite`` is the one field here
        that is NOT sidecar-sourced — it is read from the pre-narrator
        ``context.dispatch_package`` (the IntentRouter's rewrite), retiring the
        narrator game_patch field and its absent-warning.

        It deliberately does NOT include any key in
        :data:`_SDK_TOOL_OWNED_FIELDS` — that omission is structural (a
        shared helper provably cannot emit a tool-owned key), which is what
        makes the SDK-path fail-loud invariant a backstop rather than the
        only guard. ``_assemble_turn_result`` adds the tool-owned keys back
        (it is the single applier on the sync path);
        ``_assemble_turn_result_sdk`` adds only ``tool_calls``.
        """
        prose = extraction["prose"]

        if context.dispatch_package is not None:
            audit_canonical_prose(
                prose=prose,
                package=context.dispatch_package,
                entity_tokens_by_id=self._entity_tokens_for_registry(context),
            )

        npc_mentions = [NpcMention.from_value(v) for v in extraction["npcs_present"]]

        visual_scene: VisualScene | None = None
        if extraction["visual_scene"] and isinstance(extraction["visual_scene"], dict):
            visual_scene = VisualScene.from_dict(extraction["visual_scene"])

        # Story 151-3 (ADR-150 step 3): action_rewrite is sourced from the
        # pre-narrator IntentRouter (``context.dispatch_package.action_rewrite``),
        # NOT the retired narrator game_patch. This provenance flip closes the
        # ordering hazard for EVERY post-narrator consumer at once
        # (visibility_classifier, confrontation_intent_validator, narration_apply
        # all read ``result.action_rewrite``). None when the pre-pass produced no
        # rewrite — the omitted→default loud net is the pre-pass
        # ``intent_router.action_rewrite`` span (emitted=False), not a sidecar warn.
        action_rewrite: ActionRewrite | None = None
        pre_pass = context.dispatch_package.action_rewrite if context.dispatch_package else None
        if pre_pass is not None:
            # Story 153-1: the protocol ActionRewrite dropped ``you``; the
            # orchestrator-local ActionRewrite keeps its own ``you`` default ("").
            action_rewrite = ActionRewrite(named=pre_pass.named, intent=pre_pass.intent)

        return {
            "narration": prose,
            "is_degraded": False,
            # ---- presentation / signal (no successor tool) ----
            "scene_mood": extraction["scene_mood"],
            "visual_scene": visual_scene,
            "npcs_present": npc_mentions,
            "footnotes": extraction["footnotes"]
            if isinstance(extraction["footnotes"], list)
            else [],
            "sfx_triggers": extraction["sfx_triggers"]
            if isinstance(extraction["sfx_triggers"], list)
            else [],
            "action_rewrite": action_rewrite,
            # ADR-105 B3 — private per-PC prose, partitioned by the
            # narrator at generation time. Presentation field, NO
            # successor tool → sidecar-sourced on BOTH backends (the
            # firewall must hold on claude -p AND the SDK path).
            "private_prose_segments": extraction["private_segments"]
            if isinstance(extraction["private_segments"], list)
            else [],
            # ---- state with NO successor tool: narration_apply stays the
            #      single applier on BOTH paths ----
            "items_gained": extraction["items_gained"]
            if isinstance(extraction["items_gained"], list)
            else [],
            "items_lost": extraction.get("items_lost", []),
            "items_discarded": extraction.get("items_discarded", []),
            "items_consumed": extraction.get("items_consumed", []),
            "gold_change": extraction["gold_change"],
            "lore_established": extraction["lore_established"],
            "companions_added": extraction.get("companions_added", []),
            "companions_dismissed": extraction.get("companions_dismissed", []),
            # ---- OTEL / telemetry tail ----
            "agent_name": self._narrator.name(),
            "agent_duration_ms": elapsed_ms,
            "token_count_in": token_count_in,
            "token_count_out": token_count_out,
            "prompt_tier": "",  # vestigial field; tier system removed per ADR-098
            "prompt_text": prompt_text,
            "raw_response_text": raw_response,
            "secret_routes": list(self._last_secret_routes),
        }

    @staticmethod
    def _build_tool_calls_ledger(result: ToolingResult) -> list[dict[str, Any]]:
        """ADR-103 GM-panel lie-detector ledger from the SDK tool loop.

        One ``{"id", "name", "arguments"}`` entry per accumulated
        ``ToolUseBlock``. Built in ``_run_narration_turn_sdk`` (where the
        ``narration.turn`` span is still open) so the ledger can be BOTH
        emitted onto the span as a JSON-string attribute AND carried on the
        result — the panel correlates the per-turn tool detail the
        ``tool_call_count`` attribute alone cannot express.
        """
        return [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in result.tool_calls
        ]

    def _assemble_turn_result_sdk(
        self,
        *,
        result: ToolingResult,
        prompt_text: str,
        context: TurnContext,
        elapsed_ms: int,
        tool_calls_ledger: list[dict[str, Any]],
    ) -> NarrationTurnResult:
        """SDK-path NarrationTurnResult assembly — the hybrid split (Task E1.5-B).

        Distinct from :meth:`_assemble_turn_result` (the ClaudeClient
        sync assembler, which stays byte-for-byte unchanged for
        its callers). On the SDK path the 26 WRITE tools already mutated AND
        persisted (``ctx.repository.save``) game state during the tool-dispatch
        loop, so re-applying the narrator's sidecar would double-apply.

        The split:

        * **Presentation / no-successor-tool fields** — built by the shared
          :meth:`_presentation_and_untooled_fields` helper (scene_mood,
          visual_scene, npcs_present, footnotes, sfx_triggers,
          action_rewrite, items_*, gold_change,
          lore_established, companions_*, telemetry tail). That helper
          STRUCTURALLY cannot emit a tool-owned key, so the SDK result
          carries only sidecar-sourced presentation/untooled state.
        * **Tool-owned state** — every field in
          :data:`_SDK_TOOL_OWNED_FIELDS` — is left at its dataclass default
          (zeroed) by simply not being added to the shared kwargs. The
          tool's dispatch-time write is the single authority;
          ``narration_apply`` (and the session-handler trope/affinity/clue
          seams) become no-ops for those categories.
        * ``tool_calls`` — the ADR-103 ledger (built once by
          :meth:`_build_tool_calls_ledger`, also emitted on the
          ``narration.turn`` span by the caller).

        The post-construction fail-loud invariant is a backstop: the
        structural guarantee (shared helper omits tool-owned keys) is the
        primary guard; the assertion catches a future edit that adds a
        tool-owned key here directly.
        """
        raw_response = result.text
        logger.info(
            "SDK narrator returned narration len=%d duration_ms=%d tool_calls=%d",
            len(raw_response),
            elapsed_ms,
            len(tool_calls_ledger),
        )

        with context.phase_timings.phase("narrator_extraction"):
            extraction = extract_structured_from_response(raw_response)

        shared = self._presentation_and_untooled_fields(
            extraction=extraction,
            raw_response=raw_response,
            context=context,
            elapsed_ms=elapsed_ms,
            prompt_text=prompt_text,
            token_count_in=result.input_tokens,
            token_count_out=result.output_tokens,
        )

        # Story 59-4 / ADR-113: the Story 59-1 ``begin_confrontation``
        # narrator-sidecar engagement signal is retired. Confrontation
        # engagement is now router-driven by the Intent Router pre-narrator
        # pass (``sidequest.server.intent_router_pass.execute_intent_router_pre_narrator_pass``),
        # which calls ``sidequest.agents.subsystems.confrontation.run_confrontation_dispatch``
        # against the canonical snapshot BEFORE this assembler runs. The
        # narrator therefore narrates already-real encounter state and has
        # no signaling channel for engagement; ``result.confrontation`` is
        # not set on the SDK path. The retired ``begin_confrontation`` tool
        # stub lives at ``sidequest/agents/tools/_retired/begin_confrontation.py``
        # with a breadcrumb docstring.
        #
        # RW-2 opposed_check carve-out (playtest 2026-06-05, the_circuit
        # chase): ``narration_apply._resolve_opposed_check_branch`` consumes
        # ``result.beat_selections`` — the narrator's OPPONENT-side beat pick
        # paired with the player's stashed DICE_THROW d20. Zeroing the field
        # unconditionally made the entire opposed_check engine structurally
        # unreachable on the SDK path: the player's roll was silently
        # discarded every turn and all dial movement came from the narrator
        # free-handing ``advance_confrontation``. When the ACTIVE
        # confrontation declares ``resolution_mode: opposed_check`` we lift
        # the sidecar selections through. This is NOT a double-apply risk for
        # that mode — no WRITE tool applies opposed_check beats during
        # dispatch (``advance_confrontation`` refuses the mode), and the SOUL
        # gate in narration_apply still drops PC-side selections. All other
        # modes stay zeroed (anti-double-apply guard unchanged).
        _opposed_check_active = (
            getattr(context.confrontation_def, "resolution_mode", None)
            == ResolutionMode.opposed_check
        )
        beat_selections: list[BeatSelection] = []
        if _opposed_check_active:
            beat_selections = [
                BeatSelection.from_dict(d)
                for d in extraction["beat_selections"]
                if isinstance(d, dict)
            ]

        assembled = NarrationTurnResult(
            **shared, tool_calls=tool_calls_ledger, beat_selections=beat_selections
        )

        # Fail-loud backstop (CLAUDE.md no silent fallbacks): the tool-owned
        # partition must remain at dataclass defaults so narration_apply
        # does not double-apply what the WRITE tools already persisted.
        # ``beat_selections`` is exempt exactly when the opposed_check
        # carve-out above is active — that is the single sanctioned carrier.
        _violations = [
            name
            for name in _SDK_TOOL_OWNED_FIELDS
            if not (name == "beat_selections" and _opposed_check_active)
            and getattr(assembled, name) != getattr(_NTR_DEFAULTS, name)
        ]
        if _violations:
            raise AssertionError(
                "SDK-path NarrationTurnResult must zero tool-owned fields "
                f"(tools applied+saved them during dispatch); non-default: {_violations!r}"
            )

        return assembled

    async def _run_narration_turn_synchronous(
        self,
        action: str,
        context: TurnContext,
    ) -> NarrationTurnResult:
        """Stateless narrator pipeline (ADR-098).

        Build the prompt, partition into (system_prompt, user_message),
        send via :meth:`LlmClient.send_stateless`, parse, return.

        No session id is read or written. No first-turn-vs-subsequent
        branching. If the first attempt fails transiently, retry once;
        otherwise return a degraded :class:`NarrationTurnResult`.
        """
        with orchestrator_process_action_span(action_len=len(action)) as _action_span:
            agent_name = self._narrator.name()

            prompt_text, registry = await self.build_narrator_prompt(action, context)
            system_prompt, user_message = registry.compose_split(agent_name)

            if self._check_oversized_prompt(system_prompt, user_message, registry, agent_name):
                # Story 61-3: hard refuse short-circuits the SDK call. The
                # loud emit inside _check_oversized_prompt pages the operator;
                # the degraded result keeps the player surface alive.
                # Story 61-8 §C2: stamp the action span so the GM panel can
                # red-band-color the per-turn ribbon (companion to the
                # watcher event — the event flags the moment, the span
                # attribute lets a per-turn view color the whole turn).
                _action_span.set_attribute("refused_oversized", True)
                return self._degraded_result(
                    action=action,
                    context=context,
                    narration="[narrator-overload — operator paged]",
                )

            logger.info(
                "narrator.stateless_turn action=%r system_len=%d user_len=%d",
                action,
                len(system_prompt),
                len(user_message),
            )

            response, elapsed_ms = await self._invoke_with_retry_once(
                system_prompt=system_prompt,
                user_message=user_message,
                phase_timings=context.phase_timings,
            )

            if response is None:
                return self._degraded_result(action=action, context=context)

            return self._assemble_turn_result(
                response=response,
                prompt_text=prompt_text,
                context=context,
                elapsed_ms=elapsed_ms,
                action=action,
            )

    async def _run_narration_turn_sdk(
        self,
        action: str,
        context: TurnContext,
    ) -> NarrationTurnResult:
        """SDK-backed narration path (Phase D Task 1).

        When ``self._client`` is a ``ToolingLlmClient`` (in production,
        ``AnthropicSdkClient``), the narrator turn runs through
        ``complete_with_tools`` with the full 26-tool registry. The call is
        wrapped in a ``narration.turn`` cost-rollup span so the GM panel sees
        token totals, tool-call count, and model choice for the turn.

        Sidecar parsing (ADR-039) still runs against the resulting prose via
        ``_assemble_turn_result_sdk`` — but the hybrid split (Task E1.5-B)
        means only presentation / no-successor-tool fields are sourced from
        it; tool-owned state was already applied + persisted by the WRITE
        tools during dispatch. Phase D Task 4 retires the sidecar entirely.
        """
        # Function-local imports are organizational only — these modules
        # do not back-import orchestrator. They live here so the SDK path's
        # dependencies stay co-located with the method that uses them,
        # which makes Phase D Tasks 4 (sidecar retirement) and 6 (three-zone
        # cache split) easier to refactor without disturbing module-level
        # imports used by the sync path.
        from sidequest.agents.model_routing import CallType, resolve_model
        from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
        from sidequest.agents.tool_registry import ToolContext, default_registry
        from sidequest.telemetry.spans.cost import narration_turn_cost_span

        # Refuse to enter the SDK path if the wired client doesn't satisfy
        # the tooling protocol — no silent fallbacks (CLAUDE.md).
        if not isinstance(self._client, ToolingLlmClient):
            raise TypeError(
                f"_run_narration_turn_sdk called with non-tooling client "
                f"{type(self._client).__name__!r}"
            )

        with orchestrator_process_action_span(action_len=len(action)) as _action_span:
            agent_name = self._narrator.name()

            # build_narrator_prompt skips its build-time prompt_assembled on the
            # SDK path (ToolingLlmClient); the enriched event with real cache
            # usage + block digests is emitted post-call below (Story 60-2).
            prompt_text, registry = await self.build_narrator_prompt(action, context)
            zone_text, user_message = registry.compose_split_by_zone(agent_name)

            # Three-zone cacheable layout (ADR-101 Phase D Task 6).
            # Block 0 is the only cache=True block — it carries the stable
            # identity/voice/guardrails/SOUL (Primacy + Early). Valley and
            # Late+Recency ride uncached follow-on blocks so per-turn drift
            # (e.g. narrator_vocabulary, genre_transition_hints) does not
            # invalidate the cache prefix. The byte-stability gate in
            # tests/agents/test_cache_ttl_prefix_and_otel.py protects
            # system_blocks[0] across turns; the uncached blocks are free
            # to mutate.
            #
            # NOTE (Story 60-3): block-0 byte-stability is necessary but NOT
            # sufficient for the cache rebate. The stable prefix is confirmed
            # byte-identical across turns, yet the narrator's tool-use loop
            # still re-mints it at 5m on every continuation call (the appended
            # tool_use/tool_result messages carry no cache breakpoint). The
            # rebate is realized only once 60-4 adds a moving 1h breakpoint on
            # the continuation. See sprint/archive/60-3-session.md.
            stable_text = "\n\n".join(
                t
                for t in (
                    zone_text.get(AttentionZone.Primacy, ""),
                    zone_text.get(AttentionZone.Early, ""),
                )
                if t
            )
            valley_text = zone_text.get(AttentionZone.Valley, "")
            recency_text = "\n\n".join(
                t
                for t in (
                    zone_text.get(AttentionZone.Late, ""),
                    zone_text.get(AttentionZone.Recency, ""),
                )
                if t
            )
            system_blocks: list[CacheableBlock] = [CacheableBlock(text=stable_text, cache=True)]
            if valley_text:
                system_blocks.append(CacheableBlock(text=valley_text, cache=False))
            if recency_text:
                system_blocks.append(CacheableBlock(text=recency_text, cache=False))

            # Story 61-3: hard-cap oversized-prompt canary on the SDK path.
            # The 2026-05-23 incident burned $313 in 48h while a SOFT warning
            # on the SYNCHRONOUS path (the wrong path — ADR-101 default is
            # the SDK path) scrolled past unread. Refuse before the SDK call
            # bills, page the operator via a LOUD watcher event with
            # severity="error", and return a distinct degraded narration
            # ("[narrator-overload — operator paged]") so the player surface
            # doesn't hang and session-recording grep can distinguish
            # budget-refuse from SDK-error-refuse.
            system_prompt_total = "\n\n".join(
                t for t in (stable_text, valley_text, recency_text) if t
            )
            if self._check_oversized_prompt(
                system_prompt_total, user_message, registry, agent_name
            ):
                # Story 61-8 §C2 — see synchronous-path twin above. Stamp
                # the per-turn action span so a future GM-panel per-turn
                # view can red-band-color the entire refused turn.
                _action_span.set_attribute("refused_oversized", True)
                return self._degraded_result(
                    action=action,
                    context=context,
                    narration="[narrator-overload — operator paged]",
                )

            # Story 73-15 (ADR-117 tightening): advertise only the tools the
            # bound ruleset can actually use. The WWN/CWN-only tools declare a
            # ``ruleset`` and are filtered out on any other pack so the narrator
            # neither wastes tool-budget on, nor mis-attempts, tools it can only
            # fail to use. ``context.pack`` is None on legacy/fixture paths —
            # there we pass ruleset=None (the full catalog) so behavior is
            # unchanged (No Silent Fallbacks: the unfiltered list is the honest
            # answer when no ruleset is bound, and each tool keeps its fail-loud
            # self-guard backstop). Computed once here and reused for the real
            # ``tools=`` array below.
            _pack = context.pack
            bound_ruleset: str | None = None
            if _pack is not None and getattr(_pack, "rules", None) is not None:
                bound_ruleset = _pack.rules.ruleset
            advertised_tool_defs = default_registry.tool_definitions(bound_ruleset)
            _total_tool_count = len(default_registry.list_names())
            _advertised_tool_count = len(advertised_tool_defs)
            # GM-panel lie detector (CLAUDE.md OTEL Observability Principle):
            # surface the filter decision so the panel can verify the tightening
            # engaged rather than the narrator improvising.
            from sidequest.telemetry.spans.span import Span as _ToolFilterSpan

            with _ToolFilterSpan.open(
                "narrator.tools.ruleset_filter",
                {
                    "tools.bound_ruleset": bound_ruleset or "none",
                    "tools.advertised_count": _advertised_tool_count,
                    "tools.excluded_count": _total_tool_count - _advertised_tool_count,
                },
            ):
                pass

            # Stability-audit diagnostic — per-block token estimate using the
            # project's standard char/4 approximation (see orchestrator.py
            # token-estimate pattern). Tools size is computed from the
            # registry's serialized JSON (now the ruleset-filtered set, so the
            # estimate reflects what the narrator is actually handed). Drift in
            # the 'stable' region surfaces as a growing value across turns.
            tools_payload = json.dumps(
                [
                    {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                    for t in advertised_tool_defs
                ]
            )
            system_block_sizes = {
                "stable": len(stable_text) // 4,
                "valley": len(valley_text) // 4,
                "recency": len(recency_text) // 4,
                "tools": len(tools_payload) // 4,
            }
            messages = [Message(role="user", content=user_message)]

            model = resolve_model(CallType.NARRATION)

            # Aside-rides-the-cache (playtest 2026-06-07, ADR-107 re-scope):
            # stash the EXACT system blocks + tools + model this turn ships,
            # so an out-of-band aside can re-present the identical prefix to
            # the API and read it from cache (caches are per-model and
            # byte-exact — any drift is a miss, which the aside's cache_hit
            # span attribute surfaces as the lie-detector). The stash is a
            # reference copy, not a rebuild: rebuilding would risk byte
            # drift. Refreshed every SDK turn; None until the first turn
            # (the handler's legacy thin read-view covers that window).
            # DRIVER verification failure 2026-06-07: the per-turn game state
            # rides user_message (ADR-110 user-bucket placement), not the
            # system blocks — and the calendar reaches the narrator only via
            # the get_world_grounding TOOL. Stash both so the aside's user
            # turn can re-present them (user-turn bytes are outside the cache
            # prefix; this cannot bust the cache).
            _stash_calendar = ""
            if context.world_calendar:
                _stash_calendar = json.dumps(
                    context.world_calendar, ensure_ascii=False, default=str
                )
            self._aside_prompt_stash = AsidePromptStash(
                system_blocks=list(system_blocks),
                tools=list(advertised_tool_defs),
                model=model,
                user_state_text=user_message,
                calendar_summary=_stash_calendar,
            )

            # Phase E now plumbs world_id/session_id/store/lore_store/
            # monster_manual onto TurnContext via _build_turn_context (off
            # _SessionData). Read them directly. The "unknown"/"adhoc"
            # coalescing stays ONLY as a fail-loud guard (No Silent
            # Fallbacks): if a TurnContext reaches this path genuinely
            # unwired (a regression in _build_turn_context, or a legacy
            # caller), we still emit a real anomaly warning so the GM panel
            # sees it — post-wiring this should essentially never fire.
            world_id = context.world_id or "unknown"
            session_id = context.session_id or "adhoc"
            # Story 61-8 §A (review-fix round 2): both partial-wiring
            # guards now publish watcher events alongside logger.warning
            # so the GM panel sees the regressions distinctly (CLAUDE.md
            # OTEL Observability Principle — log-only is invisible to the
            # panel). The pre-existing umbrella ``context_missing_ids``
            # had the same gap; closing both here is the reviewer audit
            # ask filed as a small additional fix on the §A commit.
            from sidequest.telemetry.watcher_hub import publish_event as _pub_watcher

            if world_id == "unknown" or session_id == "adhoc":
                logger.warning(
                    "narrator.sdk_path.context_missing_ids — world_id=%s "
                    "session_id=%s unexpectedly missing post-wiring; check "
                    "_build_turn_context (should never fire in production).",
                    world_id,
                    session_id,
                )
                _pub_watcher(
                    "narrator_context_missing_ids",
                    {"world_id": world_id, "session_id": session_id},
                    component="orchestrator",
                    severity="warn",
                )
            # Story 61-8 §A: defense-in-depth on the Phase-E lore_store
            # seam. The umbrella ``context_missing_ids`` guard above only
            # catches the unwired-TurnContext case (world_id/session_id
            # missing). A REGRESSION in ``_build_turn_context`` that drops
            # ``lore_store=sd.lore_store`` would slip past — ids would be
            # present, ``query_lore`` would silently return
            # ``lore_store_wired=False``, and the narrator would
            # confabulate canon (the original 61-1 failure mode). Fire a
            # separate warning + watcher event so the GM panel sees the
            # partial-wiring regression distinctly from the umbrella case.
            if context.lore_store is None and world_id != "unknown" and session_id != "adhoc":
                logger.warning(
                    "narrator.sdk_path.context_missing_lore_store — "
                    "world_id=%s session_id=%s has present ids but "
                    "lore_store=None; query_lore will return "
                    "lore_store_wired=False. Check _build_turn_context "
                    "thread-through of sd.lore_store.",
                    world_id,
                    session_id,
                )
                _pub_watcher(
                    "narrator_context_missing_lore_store",
                    {"world_id": world_id, "session_id": session_id},
                    component="orchestrator",
                    severity="warn",
                )

            perception_filter = NarratorPerceptionFilter()
            call_start = time.monotonic()
            with narration_turn_cost_span(
                world_id=world_id,
                session_id=session_id,
                turn_number=context.turn_number,
                acting_pc=context.character_name,
            ) as span:
                tool_ctx = ToolContext(
                    world_id=world_id,
                    session_id=session_id,
                    perspective_pc=context.character_name,
                    turn_number=context.turn_number,
                    repository=context.repository,
                    otel_span=span,
                    perception_filter=perception_filter,
                    # Phase E wiring — THE fix for query_lore hit_count=0.
                    # Without this, ToolContext.lore_store defaulted None and
                    # the narrator got no world lore, then confabulated canon.
                    lore_store=context.lore_store,
                    # Same Phase-E seam: lookup_monster reads this.
                    monster_manual=context.monster_manual,
                    # Story 24-10: world-grounding pass-through — the fix for
                    # get_world_grounding returning null sections. Same seam:
                    # loaded once at session bootstrap, carried on TurnContext,
                    # read by the get_world_grounding tool. None when the
                    # pack/world authored no grounding (no silent fallback).
                    weather_state=context.weather_state,
                    world_demographics=context.world_demographics,
                    world_calendar=context.world_calendar,
                    # Story 59-1: begin_confrontation validates the requested
                    # confrontation type against this pack and SIGNALS via
                    # result.confrontation; narration_apply creates the encounter
                    # on the canonical snapshot (the tool does NOT instantiate it
                    # during dispatch). None until _build_turn_context stamps
                    # context.pack — begin_confrontation fails loudly rather than
                    # silently no-opping if it is missing.
                    genre_pack=context.pack,
                    # Story 73-3: the canonical in-turn snapshot the rest of the
                    # turn mutates and the end-of-turn save persists. WRITE tools
                    # (advance_confrontation) must mutate THIS object — not a
                    # fresh repository.load() copy — or the end-of-turn save
                    # clobbers their write (the lost-update bug). Same seam as
                    # genre_pack/lore_store above. None until _build_turn_context
                    # stamps context.snapshot; the tool fails loud rather than
                    # silently loading a fresh copy.
                    snapshot=context.snapshot,
                )

                # Positive wiring confirmation (CLAUDE.md OTEL principle —
                # replaces the silent context gap so the GM panel can verify
                # the fix engaged). The narration.turn span already carries
                # world_id/session_id/turn_number and tool.read.query_lore
                # already emits tool.lore.hit_count — this line is the
                # one-shot proof the lore_store reference reached the tool
                # context at all (lore_fragments>0 means query_lore CAN hit).
                lore_fragments = len(context.lore_store) if context.lore_store else 0
                span.set_attribute("narration.turn.lore_fragments", lore_fragments)
                logger.info(
                    "narrator.sdk_path.context_wired — world_id=%s "
                    "session_id=%s turn=%s lore_fragments=%d "
                    "monster_manual=%s",
                    world_id,
                    session_id,
                    context.turn_number,
                    lore_fragments,
                    context.monster_manual is not None,
                )

                async def dispatch(block: ToolUseBlock) -> ToolResultBlock:
                    return await default_registry.dispatch(block, tool_ctx)

                result = await self._client.complete_with_tools(
                    system_blocks=system_blocks,
                    messages=messages,
                    # Story 73-15: the ruleset-filtered set computed once above
                    # (bound_ruleset derived from context.pack). Pack-less paths
                    # get the full catalog unchanged.
                    tools=advertised_tool_defs,
                    tool_dispatch=dispatch,
                    model=model,
                    # Story 61-followup-D §C.1 — forward the session_id so
                    # the SDK client can key per-session cumulative cost
                    # against it. Pass context.session_id raw (None when
                    # absent) so tests / non-narrator paths bypass the
                    # tracker cleanly; do NOT substitute the "adhoc"
                    # sentinel here.
                    session_id=context.session_id,
                    # Story 82-9 — forward the operator's soft tool-loop cap
                    # (SIDEQUEST_NARRATOR_ITERATION_CAP; None = off) and tag the
                    # tool_loop summary span as a narrator solo-turn so the GM
                    # panel can filter curate calls out of solo-turn p95.
                    iteration_cap=resolve_narrator_iteration_cap(),
                    caller="narrator",
                )

                # Cost-rollup attributes — names per cost.py docstring.
                span.set_attribute("narration.turn.model_chosen", result.model)
                span.set_attribute("narration.turn.total_input_tokens", result.input_tokens)
                span.set_attribute("narration.turn.total_output_tokens", result.output_tokens)
                span.set_attribute(
                    "narration.turn.cache_read_tokens", result.cached_input_read_tokens
                )
                span.set_attribute(
                    "narration.turn.cache_write_tokens", result.cached_input_write_tokens
                )
                span.set_attribute(
                    "narration.turn.cache_write_5m_tokens",
                    result.cached_input_write_5m_tokens,
                )
                span.set_attribute(
                    "narration.turn.cache_write_1h_tokens",
                    result.cached_input_write_1h_tokens,
                )
                span.set_attribute(
                    "narration.turn.system_block_sizes_json",
                    json.dumps(system_block_sizes),
                )
                # Cache TTL the client is configured with, so the GM panel
                # can prove the 1h fix engaged and compute write
                # amortization against cache_write_tokens above.
                span.set_attribute(
                    "narration.turn.cache_ttl",
                    getattr(self._client, "cache_ttl", "n/a"),
                )
                # Total per-turn API spend — sum of compute_cost_usd
                # across every tool-loop iteration. Documented at
                # telemetry/spans/cost.py:15; the GM panel reads this to
                # show $/turn next to cache hit-rate (Task B1).
                span.set_attribute("narration.turn.total_cost_usd", result.cumulative_cost_usd)
                # Per-turn health band (all_systems_go / needs_work /
                # stop_everything) so the GM panel can color $/turn without
                # re-deriving thresholds. cost-per-turn, not daily total, is the
                # efficiency signal — see anthropic_cost.cost_band.
                span.set_attribute(
                    "narration.turn.cost_band", cost_band(result.cumulative_cost_usd)
                )
                span.set_attribute("narration.turn.tool_call_count", len(result.tool_calls))

                # ADR-103 / CLAUDE.md OTEL principle: emit the per-call
                # ledger, not just the count. The GM-panel lie-detector
                # correlates each tool's ``{id,name,arguments}`` against the
                # prose — ``tool_call_count`` alone cannot express that.
                # OTEL silently drops list/dict attribute values, so the
                # project convention (see telemetry/spans/magic.py
                # ``*_json`` attributes) is a JSON-string. Built here, while
                # the span is still open, and carried onto the result by the
                # assembler below so both consumers see the same ledger.
                tool_calls_ledger = self._build_tool_calls_ledger(result)
                span.set_attribute("narration.turn.tool_calls_json", json.dumps(tool_calls_ledger))

            elapsed_ms = int((time.monotonic() - call_start) * 1000)

            # Story 60-2 — emit the enriched prompt_assembled now that the real
            # API cache usage and the assembled blocks are both in hand. The
            # cache_blocks digests hash the SAME `stable_text`/`valley_text`/
            # `recency_text`/`tools_payload` that built `system_blocks` above, so
            # the panel's "stable / drifted" claim cannot diverge from what was
            # actually sent (single source of truth — see context-story-60-2).
            from sidequest.telemetry.watcher_hub import publish_event as _pub_prompt

            _prompt_sections = registry.registry(agent_name)
            _prompt_payload = self._build_prompt_event_payload(
                agent_name=agent_name,
                context=context,
                prompt_text=prompt_text,
                section_count=len(_prompt_sections),
                sections=_prompt_sections,
            )
            _prompt_payload["cache_blocks"] = _compute_cache_blocks(
                stable_text=stable_text,
                valley_text=valley_text,
                recency_text=recency_text,
                tools_payload=tools_payload,
            )
            _prompt_payload["cache_usage"] = {
                "cache_read": result.cached_input_read_tokens,
                "cache_write": result.cached_input_write_tokens,
                "cache_write_5m": result.cached_input_write_5m_tokens,
                "cache_write_1h": result.cached_input_write_1h_tokens,
                "cost_usd": result.cumulative_cost_usd,
                "cost_band": cost_band(result.cumulative_cost_usd),
                "cache_ttl": getattr(self._client, "cache_ttl", "n/a"),
            }
            _pub_prompt("prompt_assembled", _prompt_payload, component="prompt_builder")

            # Task E1.5-B — hybrid split. The WRITE tools already mutated +
            # persisted (``ctx.repository.save``) every tool-owned state category
            # during the dispatch loop above. ``_assemble_turn_result_sdk``
            # builds the NarrationTurnResult so the tool-owned fields are
            # ZEROED (narration_apply must not re-apply them) while
            # presentation / no-successor-tool fields stay sidecar-sourced.
            # The non-SDK ``_assemble_turn_result`` is intentionally NOT
            # called here — that path re-applies the full sidecar because no
            # tool runs during its dispatch, and double-applying on the SDK
            # path is exactly the bug this task fixes.
            return self._assemble_turn_result_sdk(
                result=result,
                prompt_text=prompt_text,
                context=context,
                elapsed_ms=elapsed_ms,
                tool_calls_ledger=tool_calls_ledger,
            )
