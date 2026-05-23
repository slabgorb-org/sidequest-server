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
    from sidequest.game.lore_store import LoreStore
    from sidequest.game.monster_manual import MonsterManual
    from sidequest.game.session import GameSnapshot

# Importing this package wires the 26 tool adapters onto default_registry at
# module import time. Required for the SDK path; the streaming/sync ClaudeClient
# paths do not depend on the registry.
import sidequest.agents.tools  # noqa: F401  (registration side effect)
from sidequest.agents.claude_client import (
    ClaudeClient,
    ClaudeResponse,
    LlmClient,
)
from sidequest.agents.claude_client import (
    TimeoutError as _ClaudeTimeoutError,
)
from sidequest.agents.narrator import NarratorAgent, is_streaming_enabled
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
from sidequest.protocol.dice import RollOutcome
from sidequest.protocol.dispatch import DispatchPackage, NarratorDirective
from sidequest.telemetry.leak_audit import audit_canonical_prose
from sidequest.telemetry.phase_timing import PhaseTimings
from sidequest.telemetry.spans import (
    orchestrator_process_action_span,
    recent_narrative_context_injected_span,
    turn_agent_llm_inference_span,
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
                            _section_rides_cache(s.name, zone_value)
                            and s.category.value == "state"
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
SOFT_PROMPT_BUDGET_BYTES = 2_000_000  # ~500K tokens, half of Opus 4.7's 1M window (ADR-098)

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
        return cls(
            actor=str(d.get("actor", "")),
            beat_id=str(d.get("beat_id", "")),
            outcome=outcome,
            target=d.get("target"),
            spell_id=str(spell_id_raw) if spell_id_raw else None,
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
    quest_updates: dict[str, str] = field(default_factory=dict)
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
    # (ClaudeClient sync/streaming) — no tool loop runs there, so there is
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
    # SqliteStore — kept ``Any`` to avoid a circular import (mirrors
    # ``ToolContext.store``'s "kept Any to avoid coupling" rationale).
    store: Any = None
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

    # Group B (Local DM decomposer) — session handler populates before calling
    # run_narration_turn. Consumed by build_narrator_prompt to register the
    # narrator_directives PromptSection. Default None = decomposer did not run.
    dispatch_package: DispatchPackage | None = None

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


def extract_structured_from_response(raw: str) -> dict[str, Any]:
    """Extract the narrator's prose and all structured fields from a raw response.

    The narrator emits a ```game_patch { ... }``` block every turn containing
    footnotes, items, NPCs, mood, etc. This function parses that block and
    maps it to a plain dict, then strips the fence from the returned prose.

    Returns a dict with keys:
      prose, footnotes, items_gained, items_lost, npcs_present, quest_updates,
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
        "npcs_present=%d quest_updates=%d sfx_triggers=%d "
        "has_visual_scene=%s has_scene_mood=%s has_action_rewrite=%s "
        "beat_selections=%d confrontation=%r "
        "has_location=%s gold_change=%r status_changes=%d "
        "companions_added=%d companions_dismissed=%d",
        len(patch.get("footnotes", [])),
        len(patch.get("items_gained", [])),
        len(patch.get("items_lost", [])),
        len(patch.get("items_discarded", [])),
        len(patch.get("items_consumed", [])),
        len(patch.get("npcs_present", patch.get("npcs_met", []))),
        len(patch.get("quest_updates", {})),
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

    return {
        "prose": prose,
        "footnotes": patch.get("footnotes", []),
        "items_gained": patch.get("items_gained", []),
        "items_lost": patch.get("items_lost", []),
        "items_discarded": patch.get("items_discarded", []),
        "items_consumed": patch.get("items_consumed", []),
        "npcs_present": patch.get("npcs_present", patch.get("npcs_met", [])),
        "quest_updates": patch.get("quest_updates", {}),
        "visual_scene": patch.get("visual_scene"),
        "scene_mood": patch.get("scene_mood", patch.get("mood")),
        "sfx_triggers": patch.get("sfx_triggers", []),
        "action_rewrite": patch.get("action_rewrite"),
        # ADR-105 B3: private per-PC prose the narrator partitioned out
        # of the public blob. Keep only well-formed entries (a non-empty
        # ``text`` string); a malformed entry is dropped here rather than
        # risking an un-routable segment downstream. anchor_pc is
        # optional (the per-segment POV swap no-ops without it).
        "private_segments": [
            {
                "text": str(seg["text"]).strip(),
                "anchor_pc": (str(seg["anchor_pc"]).strip() or None)
                if seg.get("anchor_pc")
                else None,
            }
            for seg in patch.get("private_segments", [])
            if isinstance(seg, dict) and isinstance(seg.get("text"), str) and seg["text"].strip()
        ],
        "beat_selections": patch.get("beat_selections", []),
        "confrontation": patch.get("confrontation"),
        "location": patch.get("location"),
        "affinity_progress": [
            (str(d["name"]), int(d.get("delta", 1)))
            for d in patch.get("affinity_progress", [])
            if isinstance(d, dict) and "name" in d
        ],
        "gold_change": patch.get("gold_change"),
        "lore_established": patch.get("lore_established"),
        "status_changes": patch.get("status_changes", []),
        # Magic system (Coyote Star iter 3 — Task 3.3). Forwarded as a
        # raw dict; pydantic validation happens in
        # ``narration_apply.apply_magic_working`` so the parse error is
        # raised at the apply seam (where ``MagicWorkingParseError`` is
        # defined) rather than during extraction.
        "magic_working": patch.get("magic_working"),
        "companions_added": [d for d in patch.get("companions_added", []) if isinstance(d, dict)],
        "companions_dismissed": [str(n) for n in patch.get("companions_dismissed", []) if n],
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
# (``ctx.store.save``) game state during the tool-dispatch loop. The
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
# items_* / gold_change / quest_updates / lore_established / companions_*
# are deliberately NOT in this partition: NO registered tool in
# sidequest/agents/tools/ mutates inventory, gold, the quest log, lore, or
# the companion roster (verified — query_character/query_encounter only
# READ inventory), and none has a COVERAGE_MAP row. They stay
# sidecar-sourced so narration_apply remains their single applier on BOTH
# paths.
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
    # here. Story 59-1: a tool CANNOT create the encounter on the SDK path —
    # ``ctx.store.load()`` returns a fresh deserialized snapshot, and the
    # tool's ``ctx.store.save`` is clobbered at turn end by ``room.save()``,
    # which persists the room's CANONICAL in-memory snapshot (the tool never
    # touched it). So engagement is routed through ``result.confrontation``
    # (set in ``_assemble_turn_result_sdk`` from the begin_confrontation tool
    # call) and applied by ``narration_apply``'s consumer, which mutates the
    # canonical snapshot IN PLACE — the single creation mechanism on BOTH
    # backends. ``begin_confrontation`` is the narrator-facing signal +
    # validator, not the state writer.
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


def _build_verbosity_section(verbosity: str) -> str:
    """Build the narrator verbosity constraint text for the given setting.

    Port of the verbosity match block in build_narrator_prompt_tiered().
    """
    if verbosity == "concise":
        return (
            "<critical>\n"
            "<length-limit>\n"
            "HARD LIMIT: Maximum 4 sentences of prose. DO NOT EXCEED 400 characters of narrative text.\n"
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
            "HARD LIMIT: Maximum 10 sentences of prose. DO NOT EXCEED 1000 characters of narrative text.\n"
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
        "HARD LIMIT, per acting PC this turn: maximum 8 sentences and 800 characters of prose.\n"
        "If no PCs are acting (scene anchor, transition, or pure narrator beat), the same\n"
        "limit applies to the whole response: 8 sentences / 800 characters total.\n"
        "This overrides all other length guidance. If a trope beat, genre voice instruction, "
        "or MUST-weave directive would push you past this limit, cut description — never cut the limit.\n"
        "Each acting PC gets one short paragraph for simple actions, "
        "or two short paragraphs for arrivals or reveals. Give every PC their own beat — "
        "do not collapse two PCs' actions into a single sentence to save room.\n"
        "The game_patch JSON block does not count toward this limit.\n"
        "Count sentences per PC before responding. If any PC has more than 8, cut that PC's beat.\n"
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
                    is a ToolingLlmClient (AnthropicSdkClient) and streaming
                    is disabled, ``run_narration_turn`` routes through
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

        ADR-111 (story 57-4): on the SDK tool-use path
        (``isinstance(self._client, ToolingLlmClient)``) the four guardrail
        prose blocks live at their migration targets — the
        ``tools=`` array's ``description`` field (for tool-owned artifacts)
        or the slimmed-sidecar Primacy/Stable cached prose (for sidecar-
        owned artifacts). On the legacy ``claude -p`` / Ollama paths the
        Recency-zone registration stays byte-identical to pre-111. This
        helper centralises the gate so the four registration sites in
        ``build_narrator_prompt`` collapse from ~10 lines each to one call.
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
        # Backend-gated (Task E1.5-A). The claude -p path gets the legacy
        # full-sidecar prose; the SDK tool-use path gets the slimmed-sidecar
        # + tool-routing prose so the model is not double-instructed to emit
        # the 8 tool-owned categories E1.5-B deliberately zeros. ToolingLlmClient
        # is imported unconditionally at module top — no silent fallback.
        self._narrator.build_output_format(
            registry,
            tool_backend=isinstance(self._client, ToolingLlmClient),
        )

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

            # ADR-112 / Story 57-3 re-zoned Valley → Early — see comment above.
            if gp.chargen:
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
        if context.npc_pool or context.npcs:
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

        # World context (Valley zone) — persistent across turns.
        # Currently carries the AVAILABLE CULTURES block with
        # ``Culture.chargen=False`` entries filtered out (Story 41-11,
        # closing the Phase 2.2 IOU). Strip the leading newline the
        # helper emits for Rust-style concat — the registry handles
        # section separation.
        if context.world_context:
            registry.register_section(
                agent_name,
                PromptSection.new(
                    "world_context",
                    context.world_context.lstrip("\n"),
                    AttentionZone.Valley,
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

        # Magic context (Valley zone) — injected when a world has magic.yaml loaded.
        # Tells the narrator which plugins are active, what the hard_limits are,
        # and the per-actor ledger bars so it can emit magic_working correctly.
        if context.magic_state is not None:
            from sidequest.magic.context_builder import build_magic_context_block

            reliquaries = None
            if context.world_items is not None:
                reliquaries = list(context.world_items.reliquaries)
            magic_block = build_magic_context_block(
                magic_state=context.magic_state,
                actor_id=context.character_name or None,
                reliquaries=reliquaries,
            )
            if magic_block:
                registry.register_section(
                    agent_name,
                    PromptSection.new(
                        "magic_context",
                        f"<magic-context>\n{magic_block}\n</magic-context>",
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

        # Narrator verbosity (Recency zone — every turn)
        registry.register_section(
            agent_name,
            PromptSection.new(
                "narrator_verbosity",
                _build_verbosity_section(context.narrator_verbosity),
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
                        f"GM-NOTE: One or more inferred intent suggestions from "
                        f"the previous turn:\n{_intent_directive_block}",
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
        # GM panel can verify on every turn whether the Recency-zone
        # registrations actually skipped (SDK path) or fired (legacy).
        # Constant-emit shape: the span fires on every prompt-build so
        # absence-of-span is unambiguous (= the migration call site is
        # missing entirely), not "the legacy path". ``GUARDRAIL_NAMES``
        # and ``TOTAL_PROSE_BYTES`` are precomputed at module load time
        # — derived from the static ``ALL_GUARDRAILS`` tuple, they never
        # change after import and don't need per-turn recomputation.
        _tool_backend = isinstance(self._client, ToolingLlmClient)
        from sidequest.telemetry.spans.span import Span as _GuardrailSpan

        with _GuardrailSpan.open(
            "narrator.recency_guardrails_skipped",
            {
                "tool_backend": _tool_backend,
                "guardrails_skipped": GUARDRAIL_NAMES if _tool_backend else (),
                "bytes_saved": TOTAL_PROSE_BYTES if _tool_backend else 0,
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
            from sidequest.agents.subsystems import run_dispatch_bank

            # ``npc_pool`` is required by ``run_npc_agency`` (kw-only,
            # no default; rewired from ``npc_registry`` in story 45-52).
            # Always include it — even when empty — so the subsystem can
            # invoke without TypeError. The bank filters context keys per
            # subsystem signature so we don't accidentally blast
            # ``npc_pool`` into subsystems that don't accept it.
            bank_context: dict[str, object] = {
                "npc_pool": list(context.npc_pool or []),
            }

            with context.phase_timings.phase("dispatch_bank"):
                bank_result = await run_dispatch_bank(
                    visible_dispatch_package,
                    context=bank_context,
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
                combined_directives = list(bank_result.directives) + arbiter_directives
                if combined_directives:
                    block = "\n".join(f"- [{d.kind}] {d.payload}" for d in combined_directives)
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
        room: object | None = None,
        extra_directive: str | None = None,
    ) -> NarrationTurnResult:
        """Process a player action through the Phase 1 narration pipeline.

        Routes to the streaming path when SIDEQUEST_NARRATOR_STREAMING=1,
        otherwise delegates to the synchronous path (default, flag-off behavior
        is byte-identical to prior implementation).

        Args:
            action: Raw player input text.
            context: Turn context (world state, genre prompts, etc.).
            room: Optional SessionRoom for streaming delta fan-out. Only
                  consumed by the streaming path; the sync path ignores it.
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
        # model. This check MUST take precedence over the streaming flag:
        # SDK streaming is Phase D Task 7 and is NOT yet implemented, and
        # _run_narration_turn_streaming asserts the client is not a
        # ToolingLlmClient. Letting SIDEQUEST_NARRATOR_STREAMING=1 win here
        # routed the default backend into the streaming path and crashed
        # every turn with AssertionError. The tooling client always takes
        # the SDK path until Task 7 lands; streaming remains for non-tooling
        # clients (claude -p ClaudeClient) only.
        if isinstance(self._client, ToolingLlmClient):
            return await self._run_narration_turn_sdk(action, context)
        if is_streaming_enabled():
            return await self._run_narration_turn_streaming(action, context, room=room)
        return await self._run_narration_turn_synchronous(action, context)

    async def _run_narration_turn_streaming(
        self,
        action: str,
        context: TurnContext,
        *,
        room: object | None = None,
    ) -> NarrationTurnResult:
        """Streaming variant — broadcasts prose deltas live, emits canonical
        NarrationTurnResult at end-of-stream using the same extraction path
        as the synchronous variant.

        Pipeline:
          action → build_narrator_prompt → send_stream (ClaudeClient)
               → StreamFenceParser (prose deltas → broadcast_delta)
               → extract_structured_from_response on full_text
               → NarrationTurnResult (same shape as sync path)

        Falls back to the synchronous path if the client does not support
        streaming (e.g. Ollama or a test double that only has send_with_session).
        """
        import asyncio
        import uuid

        from sidequest.agents.claude_client import (
            StreamComplete,
            StreamError,
            TextDelta,
        )
        from sidequest.agents.stream_fence import StreamFenceParser
        from sidequest.server.emitters import broadcast_delta
        from sidequest.telemetry.spans import (
            narrator_stream_complete_span,
            narrator_stream_error_span,
            narrator_stream_fence_detected,
            narrator_stream_first_token,
            narrator_stream_start_span,
        )

        # Narrow self._client back to LlmClient for the streaming path —
        # run_narration_turn gates the SDK path on isinstance(...,
        # ToolingLlmClient), so by the time we reach the streaming path the
        # client is guaranteed NOT to be a ToolingLlmClient. The assert
        # pins that invariant for pyright (the union widening landed in
        # Phase D Task 1) and fails loudly if a future caller bypasses
        # run_narration_turn. We deliberately do NOT assert isinstance
        # against LlmClient — Protocol runtime-checks reject AsyncMock and
        # other structural test doubles that nevertheless work at runtime.
        assert not isinstance(self._client, ToolingLlmClient), (
            f"streaming path must not see a ToolingLlmClient, got {type(self._client).__name__}"
        )
        client: LlmClient = self._client  # type: ignore[assignment]

        # Degrade to synchronous if the client doesn't support send_stream
        # (e.g. Ollama or legacy test doubles). No silent fallback — we log
        # loudly so the discrepancy is visible in the GM panel.
        if not hasattr(client, "send_stream"):
            logger.warning(
                "orchestrator.streaming_unsupported — client=%r lacks send_stream; "
                "falling back to synchronous path",
                type(self._client).__name__,
            )
            return await self._run_narration_turn_synchronous(action, context)

        with orchestrator_process_action_span(action_len=len(action)):
            agent_name = self._narrator.name()

            prompt_text, _registry = await self.build_narrator_prompt(action, context)

            # ADR-098: stateless — no persistent session; every turn is a fresh call.
            current_session_id: str | None = None
            system_prompt_for_establish = prompt_text
            send_prompt = action

            # Mint a turn_id for delta sequencing.  Use the interaction counter
            # when available so deltas are correlated with the canonical event.
            turn_id: str = str(context.turn_number) if context.turn_number else str(uuid.uuid4())

            seq = 0
            delta_count = 0
            prose_chunks: list[str] = []
            first_token_time: float | None = None

            async def on_prose_delta(chunk: str) -> None:
                nonlocal seq
                prose_chunks.append(chunk)
                if room is not None:
                    await broadcast_delta(
                        turn_id=turn_id,
                        chunk=chunk,
                        seq=seq,
                        room=room,
                    )
                seq += 1

            call_start = time.monotonic()

            async def on_fence(prose_bytes: int) -> None:
                narrator_stream_fence_detected(
                    turn_id=turn_id,
                    prose_bytes_at_fence=prose_bytes,
                    seconds_to_fence=time.monotonic() - call_start,
                )

            parser = StreamFenceParser(on_prose_delta=on_prose_delta, on_fence_detected=on_fence)
            terminal: StreamComplete | StreamError | None = None

            with narrator_stream_start_span(
                turn_id=turn_id,
                prompt_tokens=len(send_prompt) // 4,
                model=NARRATOR_MODEL,
                session_id=current_session_id,
            ):
                try:
                    with (
                        context.phase_timings.phase("narrator_subprocess"),
                        turn_agent_llm_inference_span(
                            model=NARRATOR_MODEL,
                            prompt_len=len(send_prompt),
                        ),
                    ):
                        async for event in client.send_stream(
                            prompt=send_prompt,
                            model=NARRATOR_MODEL,
                            session_id=current_session_id,
                            system_prompt=system_prompt_for_establish,
                            allowed_tools=[],
                            env_vars={},
                        ):
                            if isinstance(event, TextDelta):
                                if first_token_time is None:
                                    first_token_time = time.monotonic() - call_start
                                    narrator_stream_first_token(
                                        turn_id=turn_id, ttft_seconds=first_token_time
                                    )
                                delta_count += 1
                                await parser.feed(event.text)
                            elif isinstance(event, (StreamComplete, StreamError)):
                                terminal = event
                except asyncio.CancelledError:
                    elapsed_s = time.monotonic() - call_start
                    from sidequest.telemetry.spans import narrator_stream_cancelled_span

                    narrator_stream_cancelled_span(
                        turn_id=turn_id,
                        reason="task_cancelled",
                        partial_prose_bytes=len("".join(prose_chunks)),
                    )
                    logger.warning(
                        "CLAUDE CLI STREAMING CANCELLED turn_id=%s elapsed_s=%.2f",
                        turn_id,
                        elapsed_s,
                    )
                    raise
                except Exception as e:
                    elapsed_ms = int((time.monotonic() - call_start) * 1000)
                    narrator_stream_error_span(
                        turn_id=turn_id,
                        error_kind=type(e).__name__,
                        partial_prose_bytes=len("".join(prose_chunks)),
                        total_seconds=elapsed_ms / 1000.0,
                        detail=str(e),
                    )
                    logger.error(
                        "CLAUDE CLI STREAMING FAILED — returning degraded response (ADR-005) "
                        "agent=%s duration_ms=%d error=%s",
                        agent_name,
                        elapsed_ms,
                        e,
                    )
                    return NarrationTurnResult(
                        narration=(
                            f"**{context.current_location}**\n\n"
                            "The world holds its breath for a moment... "
                            "something shifts in the distance, but the moment passes."
                        ),
                        is_degraded=True,
                        agent_name=agent_name,
                        agent_duration_ms=elapsed_ms,
                        prompt_tier="",  # ADR-098: tier system removed
                        prompt_text=prompt_text,
                        secret_routes=list(self._last_secret_routes),
                    )

                elapsed_ms = int((time.monotonic() - call_start) * 1000)
                result = await parser.finalize()

                # On StreamError, return degraded response with whatever partial
                # prose we collected before the failure.
                if isinstance(terminal, StreamError):
                    narrator_stream_error_span(
                        turn_id=turn_id,
                        error_kind=terminal.kind,
                        partial_prose_bytes=len(result.prose),
                        total_seconds=elapsed_ms / 1000.0,
                        detail=terminal.detail,
                    )
                    logger.error(
                        "CLAUDE CLI STREAM ERROR — returning degraded response "
                        "agent=%s kind=%s duration_ms=%d detail=%s",
                        agent_name,
                        terminal.kind,
                        elapsed_ms,
                        terminal.detail,
                    )
                    partial_prose = (
                        result.prose
                        or terminal.partial_text
                        or (
                            f"**{context.current_location}**\n\n"
                            "The world holds its breath for a moment... "
                            "something shifts in the distance, but the moment passes."
                        )
                    )
                    return NarrationTurnResult(
                        narration=partial_prose,
                        is_degraded=True,
                        agent_name=agent_name,
                        agent_duration_ms=elapsed_ms,
                        prompt_tier="",  # ADR-098: tier system removed
                        prompt_text=prompt_text,
                        secret_routes=list(self._last_secret_routes),
                    )

            # Emit complete span for successful streaming turn.
            input_tokens = terminal.input_tokens if isinstance(terminal, StreamComplete) else None
            output_tokens = terminal.output_tokens if isinstance(terminal, StreamComplete) else None
            narrator_stream_complete_span(
                turn_id=turn_id,
                total_seconds=elapsed_ms / 1000.0,
                ttft_seconds=first_token_time,
                prose_bytes=len(result.prose),
                delta_count=delta_count,
                json_parse_status=result.status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            # Use the full_text from StreamComplete for extraction (authoritative
            # source — avoids double-reconstruction from chunk list).
            raw_response = (
                terminal.full_text
                if isinstance(terminal, StreamComplete)
                else result.prose
                + (
                    f"\n```game_patch\n{result.game_patch_json}\n```"
                    if result.game_patch_json
                    else ""
                )
            )

            logger.info(
                "Claude CLI returned streaming narration len=%d duration_ms=%d "
                "delta_count=%d fence_status=%s",
                len(raw_response),
                elapsed_ms,
                seq,
                result.status,
            )

            # Parse narrator response using the same helper as the sync path.
            with context.phase_timings.phase("narrator_extraction"):
                extraction = extract_structured_from_response(raw_response)

            prose = extraction["prose"]

            # Group G Task 7 — canonical-leak audit (safety net).
            if context.dispatch_package is not None:
                audit_canonical_prose(
                    prose=prose,
                    package=context.dispatch_package,
                    entity_tokens_by_id=self._entity_tokens_for_registry(context),
                )

            if extraction["action_rewrite"] is None:
                logger.warning("action_rewrite absent from extraction (streaming) — using default")

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

            npc_mentions = [NpcMention.from_value(v) for v in extraction["npcs_present"]]
            beat_selections = [
                BeatSelection.from_dict(d)
                for d in extraction["beat_selections"]
                if isinstance(d, dict)
            ]
            visual_scene: VisualScene | None = None
            if extraction["visual_scene"] and isinstance(extraction["visual_scene"], dict):
                visual_scene = VisualScene.from_dict(extraction["visual_scene"])
            action_rewrite: ActionRewrite | None = None
            if isinstance(extraction["action_rewrite"], dict):
                action_rewrite = ActionRewrite.from_dict(extraction["action_rewrite"])

            return NarrationTurnResult(
                narration=prose,
                is_degraded=False,
                location=extraction["location"],
                scene_mood=extraction["scene_mood"],
                visual_scene=visual_scene,
                confrontation=extraction["confrontation"],
                beat_selections=beat_selections,
                npcs_present=npc_mentions,
                items_gained=extraction["items_gained"]
                if isinstance(extraction["items_gained"], list)
                else [],
                items_lost=extraction.get("items_lost", []),
                items_discarded=extraction.get("items_discarded", []),
                items_consumed=extraction.get("items_consumed", []),
                footnotes=extraction["footnotes"]
                if isinstance(extraction["footnotes"], list)
                else [],
                quest_updates=extraction["quest_updates"]
                if isinstance(extraction["quest_updates"], dict)
                else {},
                sfx_triggers=extraction["sfx_triggers"]
                if isinstance(extraction["sfx_triggers"], list)
                else [],
                action_rewrite=action_rewrite,
                # ADR-105 B3 — firewall must hold on the streaming
                # backend too (this assembler builds the result by hand;
                # the shared helper is not used here).
                private_prose_segments=extraction["private_segments"]
                if isinstance(extraction["private_segments"], list)
                else [],
                affinity_progress=extraction["affinity_progress"],
                gold_change=extraction["gold_change"],
                lore_established=extraction["lore_established"],
                status_changes=extraction["status_changes"]
                if isinstance(extraction["status_changes"], list)
                else [],
                magic_working=(
                    extraction["magic_working"]
                    if isinstance(extraction.get("magic_working"), dict)
                    else None
                ),
                companions_added=extraction.get("companions_added", []),
                companions_dismissed=extraction.get("companions_dismissed", []),
                days_advanced=extraction.get("days_advanced", 0),
                game_patch_dict=_extract_game_patch_json(raw_response),
                agent_name=agent_name,
                agent_duration_ms=elapsed_ms,
                token_count_in=input_tokens,
                token_count_out=output_tokens,
                prompt_tier="",  # ADR-098: tier system removed
                prompt_text=prompt_text,
                raw_response_text=raw_response,
                secret_routes=list(self._last_secret_routes),
            )

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
        # Same narrowing rationale as _run_narration_turn_streaming —
        # _invoke_with_retry_once is only reached on the synchronous
        # LlmClient path, never the SDK path. The assert pins that
        # invariant for pyright and fails loudly if it's ever violated.
        # See the streaming-path comment for why we assert "not Tooling"
        # instead of "is LlmClient" (AsyncMock / test doubles).
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

    def _maybe_emit_oversized_canary(
        self,
        system_prompt: str,
        user_message: str,
        registry: PromptRegistry,
        agent_name: str,
    ) -> None:
        """Soft canary for unbounded growth regressions (ADR-098 §Bound canary)."""
        total = len(system_prompt) + len(user_message)
        if total <= SOFT_PROMPT_BUDGET_BYTES:
            return
        from sidequest.telemetry.watcher_hub import publish_event as _pub

        breakdown = [
            {"name": s.name, "chars": len(s.content)} for s in registry.registry(agent_name)
        ]
        logger.warning(
            "narrator.prompt_oversized total_bytes=%d budget=%d sections=%d",
            total,
            SOFT_PROMPT_BUDGET_BYTES,
            len(breakdown),
        )
        _pub(
            "prompt_oversized",
            {
                "total_bytes": total,
                "budget": SOFT_PROMPT_BUDGET_BYTES,
                "sections": breakdown,
            },
            component="orchestrator",
        )

    def _degraded_result(self, *, action: str, context: TurnContext) -> NarrationTurnResult:
        """Render the in-fiction stall on unrecoverable narrator failure."""
        return NarrationTurnResult(
            narration="The world holds its breath.",
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
        # sync/streaming sidecar path (the SDK path's mechanics are
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
        visual_scene, npcs_present, footnotes, sfx_triggers, action_rewrite),
        the no-successor-tool state lanes (items_*, quest_updates,
        gold_change, lore_established, companions_*), and the
        agent/token/prompt/raw/secret telemetry tail. Also performs the two
        shared side effects: the canonical-prose leak audit and the
        action_rewrite-absent warning.

        It deliberately does NOT include any key in
        :data:`_SDK_TOOL_OWNED_FIELDS` — that omission is structural (a
        shared helper provably cannot emit a tool-owned key), which is what
        makes the SDK-path fail-loud invariant a backstop rather than the
        only guard. ``_assemble_turn_result`` adds the tool-owned keys back
        (it is the single applier on the sync/streaming path);
        ``_assemble_turn_result_sdk`` adds only ``tool_calls``.
        """
        prose = extraction["prose"]

        if context.dispatch_package is not None:
            audit_canonical_prose(
                prose=prose,
                package=context.dispatch_package,
                entity_tokens_by_id=self._entity_tokens_for_registry(context),
            )

        if extraction["action_rewrite"] is None:
            logger.warning("action_rewrite absent from extraction — using default (empty rewrite)")

        npc_mentions = [NpcMention.from_value(v) for v in extraction["npcs_present"]]

        visual_scene: VisualScene | None = None
        if extraction["visual_scene"] and isinstance(extraction["visual_scene"], dict):
            visual_scene = VisualScene.from_dict(extraction["visual_scene"])

        action_rewrite: ActionRewrite | None = None
        if isinstance(extraction["action_rewrite"], dict):
            action_rewrite = ActionRewrite.from_dict(extraction["action_rewrite"])

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
            "quest_updates": extraction["quest_updates"]
            if isinstance(extraction["quest_updates"], dict)
            else {},
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
        sync/streaming assembler, which stays byte-for-byte unchanged for
        its callers). On the SDK path the 26 WRITE tools already mutated AND
        persisted (``ctx.store.save``) game state during the tool-dispatch
        loop, so re-applying the narrator's sidecar would double-apply.

        The split:

        * **Presentation / no-successor-tool fields** — built by the shared
          :meth:`_presentation_and_untooled_fields` helper (scene_mood,
          visual_scene, npcs_present, footnotes, sfx_triggers,
          action_rewrite, items_*, quest_updates, gold_change,
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

        # Story 59-1: confrontation ENGAGEMENT signal. begin_confrontation
        # cannot create the encounter itself (its ctx.store write is clobbered
        # by room.save of the canonical snapshot — see _SDK_TOOL_OWNED_FIELDS
        # note). Route the requested type onto result.confrontation here so
        # narration_apply's consumer creates the encounter on the CANONICAL
        # snapshot, in place, the same single mechanism the legacy backend
        # uses. Two gates mirror begin_confrontation's own checks so the
        # assembler honors a call the tool rejected:
        #   (a) skip if an encounter is already active — the tool returns a
        #       recoverable error in that case; routing the type would set a
        #       confrontation that narration_apply silently drops, masking the
        #       misfire from the watcher/telemetry;
        #   (b) validate against the genre's offered types so a narrator typo
        #       cannot reach narration_apply (which raises on an unknown type).
        _encounter_active = context.encounter is not None and not getattr(
            context.encounter, "resolved", False
        )
        if not _encounter_active:
            _valid_confrontations = {t for (t, _label, _cat) in context.available_confrontations}
            for _tc in result.tool_calls:
                if _tc.name != "begin_confrontation":
                    continue
                _ctype = _tc.arguments.get("confrontation_type")
                if isinstance(_ctype, str) and _ctype in _valid_confrontations:
                    shared["confrontation"] = _ctype
                    break

        # No key in _SDK_TOOL_OWNED_FIELDS is added — the shared helper
        # cannot emit one (structural guarantee). The tools own + persisted
        # those categories during dispatch; the ledger is SDK-specific, and
        # ``confrontation`` (no longer owned) is set above when the narrator
        # called begin_confrontation.
        assembled = NarrationTurnResult(**shared, tool_calls=tool_calls_ledger)

        # Fail-loud backstop (CLAUDE.md no silent fallbacks): the tool-owned
        # partition must remain at dataclass defaults so narration_apply
        # does not double-apply what the WRITE tools already persisted.
        _violations = [
            name
            for name in _SDK_TOOL_OWNED_FIELDS
            if getattr(assembled, name) != getattr(_NTR_DEFAULTS, name)
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
        with orchestrator_process_action_span(action_len=len(action)):
            agent_name = self._narrator.name()

            prompt_text, registry = await self.build_narrator_prompt(action, context)
            system_prompt, user_message = registry.compose_split(agent_name)

            self._maybe_emit_oversized_canary(system_prompt, user_message, registry, agent_name)

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
        # imports used by the streaming/sync paths.
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

        with orchestrator_process_action_span(action_len=len(action)):
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

            # Stability-audit diagnostic — per-block token estimate using the
            # project's standard char/4 approximation (see orchestrator.py
            # token-estimate pattern). Tools size is computed from the
            # registry's serialized JSON. Drift in the 'stable' region
            # surfaces as a growing value across turns of one session.
            tools_payload = json.dumps(
                [
                    {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                    for t in default_registry.tool_definitions()
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
            if world_id == "unknown" or session_id == "adhoc":
                logger.warning(
                    "narrator.sdk_path.context_missing_ids — world_id=%s "
                    "session_id=%s unexpectedly missing post-wiring; check "
                    "_build_turn_context (should never fire in production).",
                    world_id,
                    session_id,
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
                    store=context.store,
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
                    tools=default_registry.tool_definitions(),
                    tool_dispatch=dispatch,
                    model=model,
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
                "cache_ttl": getattr(self._client, "cache_ttl", "n/a"),
            }
            _pub_prompt("prompt_assembled", _prompt_payload, component="prompt_builder")

            # Task E1.5-B — hybrid split. The WRITE tools already mutated +
            # persisted (``ctx.store.save``) every tool-owned state category
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
