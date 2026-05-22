"""Apply NarrationTurnResult mutations to GameSnapshot.

Extracted from session_handler.py — pure functions over snapshot + result.
Re-exported by session_handler for back-compat.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from random import Random
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import ValidationError

if TYPE_CHECKING:
    from sidequest.agents.orchestrator import BeatSelection
    from sidequest.game.character import Character
    from sidequest.game.encounter import EncounterActor
    from sidequest.magic.confrontations import ConfrontationDefinition
    from sidequest.server.session_room import SessionRoom

from sidequest.game.morale import (
    MoraleOutcome,
    OpponentSideState,
    OpponentState,
    maybe_check_morale,
)
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.region_validation import (
    canonicalize_region_name,
    validate_region_name,
)
from sidequest.game.session import (
    ContainerState,
    GameSnapshot,
    Npc,
    RoomState,
)
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import FleeConsequence, MoraleTrigger, ResolutionMode
from sidequest.magic.confrontations import (
    BranchName,
    evaluate_auto_fire_triggers,
)
from sidequest.magic.models import Flag, MagicWorking
from sidequest.magic.state import ApplyWorkingResult, ThresholdCrossingEvent
from sidequest.magic.validator import validate as magic_validate
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.confrontation import resolve_magic_confrontation
from sidequest.server.dispatch.sealed_letter import (
    SealedLetterOutcome,
    resolve_sealed_letter_lookup,
)
from sidequest.server.session_helpers import (
    _apply_npc_observation_gate,
    _auto_mint_prose_only_npcs,
    _detect_missed_recurring_npcs,
    _detect_npc_identity_drift,
)
from sidequest.telemetry.spans import (
    container_retrieval_blocked_span,
    container_retrieval_recorded_span,
    inventory_narrator_extracted_span,
    location_drift_repaired_span,
    lore_established_span,
    magic_working_span,
    npc_auto_registered_span,
    npc_pc_name_skipped_span,
    npc_referenced_span,
    quest_update_span,
    region_entry_canonicalized_dedup_span,
    region_entry_rejected_span,
    trope_resolution_handshake_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)


# Story 49-3 — leading bold-title regex for the location-drift repair.
# Matches the first non-whitespace block at the start of the narration
# if it is a Markdown bold span, optionally heading-prefixed (``#`` /
# ``##``). The captured group is the title text. Inline ``**emphasis**``
# mid-paragraph cannot match because ``\A\s*`` anchors at the very
# beginning of the string after only whitespace. ``[^*\n]+`` prevents
# nested asterisks and stops at a newline so a runaway bold span
# doesn't swallow paragraphs.
_LEADING_BOLD_TITLE_RE = re.compile(r"\A\s*(?:#{1,2}\s+)?\*\*([^*\n]+)\*\*")


def _extract_leading_bold_title(narration: str) -> str | None:
    """Return the bold-title text at the start of ``narration``, stripped.

    Returns ``None`` when no leading bold span is present so the caller
    can short-circuit. The narrator's bold-title convention is the
    Markdown ``**Title**`` / ``## **Title**`` shape; anything else is
    treated as continuation prose and is not a scene header.
    """
    match = _LEADING_BOLD_TITLE_RE.match(narration)
    if match is None:
        return None
    title = match.group(1).strip()
    return title or None


def _resolve_innate_cast_for_beat(
    *,
    sel: BeatSelection,
    actor: EncounterActor,
    snapshot: GameSnapshot,
) -> None:
    """Story 47-10 — drive resolve_innate_v1_cast for a cast_spell beat.

    Each guard publishes a watcher event on miss so the GM panel can surface
    "cast_spell fired but innate_v1.cast didn't" — the lie-detector pattern
    for wiring gaps (CLAUDE.md OTEL principle). Loud failures, never silent.
    """
    spell_id = getattr(sel, "spell_id", None)
    if not spell_id:
        _watcher_publish(
            "magic.cast_spell_no_spell_id",
            {"actor": actor.name, "beat_id": "cast_spell"},
            component="magic",
            severity="warning",
        )
        return
    magic_state = snapshot.magic_state
    if magic_state is None:
        _watcher_publish(
            "magic.cast_spell_no_magic_state",
            {"actor": actor.name, "spell_id": spell_id},
            component="magic",
            severity="warning",
        )
        return
    catalogs = getattr(magic_state.config, "spell_catalogs", None) or {}
    spell = None
    for cat in catalogs.values():
        try:
            spell = cat.get(spell_id)
            break
        except KeyError:
            continue
    if spell is None:
        _watcher_publish(
            "magic.cast_spell_unknown",
            {
                "actor": actor.name,
                "spell_id": spell_id,
                "available_catalogs": sorted(catalogs.keys()),
            },
            component="magic",
            severity="warning",
        )
        return
    # Prepared-list gate — at apply-time, refuse to resolve a cast for a
    # spell the actor hasn't memorized. Defense-in-depth: the
    # beats_available_for filter should already have caught this in the
    # prompt build.
    prepared_at_level = magic_state.prepared_spells.get(actor.name, {}).get(spell.level, [])
    if spell_id not in prepared_at_level:
        _watcher_publish(
            "magic.cast_spell_not_prepared",
            {
                "actor": actor.name,
                "spell_id": spell_id,
                "level": spell.level,
                "prepared_at_level": prepared_at_level,
            },
            component="magic",
            severity="warning",
        )
        return

    # Save resolver: v1 stub. Opposed-check pipeline integration is a
    # follow-up tracked at:
    #   docs/superpowers/specs/2026-05-06-magic-system-caverns-and-claudes-implementation-design.md §10 (open question 5)
    #   sprint/epic-47.yaml story 47-10 Architect spec-check Mismatch 4
    # For now, default to "fail" — the defender does not save, the full
    # effect_template applies. This is the worst-case-for-defender path,
    # which is narratively safe (the narrator already chose to depict the
    # spell hitting; we don't soften without an opposed roll). The
    # auto-apply branch (null-stat spells like Magic Missile) does fire
    # the full innate_v1.cast span correctly; only the opposed-check
    # branch uses this stub.
    def _stub_save_resolver(stat: str, target_id: str) -> str:
        return "fail"

    from sidequest.magic.innate_v1_cast import resolve_innate_v1_cast

    target_id = sel.target if getattr(sel, "target", None) else ""
    resolve_innate_v1_cast(
        spell=spell,
        actor_id=actor.name,
        target_id=target_id,
        slot_consumed=True,  # resource_deltas drain ran above
        save_resolver=_stub_save_resolver if spell.save.stat is not None else None,
    )

    # Story 47-10: append to spent_spells so the UI MagicBlock can render
    # the cast spell struck-through-but-visible until rest. Idempotent on
    # repeated cast of the same spell at the same level (set semantics).
    spent_at_level = magic_state.spent_spells.setdefault(actor.name, {}).setdefault(spell.level, [])
    if spell_id not in spent_at_level:
        spent_at_level.append(spell_id)


def _all_opponents_mindless(opp_actors, pack: GenrePack | None) -> bool:
    """Return True iff every opponent actor in ``opp_actors`` maps to an
    NpcArchetype with ``mindless: True``.

    V1 deviation (Task 9 architect feedback, 2026-05-08): the dial-based
    morale wire path has no per-actor archetype linkage available without
    threading new state through ``EncounterActor``. The current encounter
    model carries actor name + side + role but no archetype reference, so
    the lookup ``actor → archetype.mindless`` is structurally unavailable
    at this seam.

    For V1 we default to ``False`` (= morale roll proceeds normally for
    every side). A future story that adds archetype linkage to
    ``EncounterActor`` (or a side-level ``mindless`` flag emitted by the
    encounter instantiator) can light this up; the helper exists as the
    single seam to update. Per CLAUDE.md "no silent fallbacks": this
    is a documented V1 deviation, not a missing-data fallback.
    """
    # Fail-loud guard: empty side cannot be all-mindless. Caller already
    # filters opponent actors, but defensive anyway.
    if not opp_actors:
        return False
    if pack is None:
        return False
    # V1: no archetype lookup wired. See deviation note above.
    return False


def _emit_morale_triggers(
    encounter,
    cdef,
    opponent_side_label: str,
    pre_kill_state: list[OpponentState],
    post_kill_state: list[OpponentState],
    killed_was_leader: bool,
    rng: Random,
) -> list[tuple[MoraleTrigger, MoraleOutcome]]:
    """Detect and fire first_blood, half_killed, leader_killed in one pass.

    Called after every beat that advances ``player_metric`` (the dial
    that tracks "opponents being defeated"). The function checks which
    triggers apply (per B/X morale rules), deduplicates via
    ``encounter.morale_events``, calls ``maybe_check_morale`` for each
    eligible trigger, and records the result.

    ─── Dial-based pseudo-HP approximation (V1 deviation) ───────────────
    The encounter engine uses dial abstractions (``player_metric``,
    ``opponent_metric``) rather than per-opponent HP. The morale spec
    (§4.4) describes triggers in terms of "opponent count drops by 1",
    which has no direct analog in this system. V1 approximates:

        pseudo_initial    = player_metric.threshold
        pseudo_pre_alive  = max(0, threshold - pre_dial_value)
        pseudo_post_alive = max(0, threshold - post_dial_value)

    Higher dial value = "more opponents down". This preserves the
    invariants the spec needs (first_blood on first dial advance,
    half_killed when post crosses ⌊threshold/2⌋), but is lossy — no
    per-opponent KO event, no per-opponent leader tag.

    ─── V1 limitations ──────────────────────────────────────────────────
    - ``leader_killed`` is False at the per-beat path. The dial-based
      wire cannot detect *which* actor was KO'd; that requires
      per-opponent HP tracking (future story). The narrator-emitted
      ``intimidated`` sidecar (Task 10) is the working path for explicit
      leader-takedown signaling until then.
    - ``mindless`` is global per side at this resolution (see
      ``_all_opponents_mindless`` — V1 returns False).

    ─── Use direct-call path for tests ──────────────────────────────────
    Tests that want to exercise per-opponent semantics (specifically the
    "two goblins, kill one then kill the other" scenario, or the
    ``leader_killed`` trigger) call this helper directly with
    constructed ``OpponentState`` lists. The helper itself is correct
    per spec; only the production wire is approximate.

    Returns a list of ``(trigger, outcome)`` tuples for the caller to
    act on (chase escalation / surrender / rout). Deferred to Task 12
    for full flee-consequence dispatch; this task wires detection +
    recording.

    OTEL: morale-check spans are deferred to Task 12 (per plan). This
    function emits a watcher event per trigger for GM-panel visibility
    in the interim.
    """
    fired: list[tuple[MoraleTrigger, MoraleOutcome]] = []

    pre_alive = sum(1 for o in pre_kill_state if o.alive)
    post_alive = sum(1 for o in post_kill_state if o.alive)
    initial = len(pre_kill_state)
    side = OpponentSideState(label=opponent_side_label, opponents=post_kill_state)

    triggers_to_check: list[MoraleTrigger] = []

    # first_blood: first opponent downed from full side (fires once per side).
    event_key_fb = f"first_blood:{opponent_side_label}"
    if (
        pre_alive == initial
        and post_alive < initial
        and event_key_fb not in encounter.morale_events
    ):
        triggers_to_check.append(MoraleTrigger.first_blood)

    # half_killed: opponent count crosses ≤ ⌊initial/2⌋.
    event_key_hk = f"half_killed:{opponent_side_label}"
    if post_alive <= initial // 2 < pre_alive and event_key_hk not in encounter.morale_events:
        triggers_to_check.append(MoraleTrigger.half_killed)

    # leader_killed: the downed actor was tagged is_leader.
    if killed_was_leader:
        triggers_to_check.append(MoraleTrigger.leader_killed)

    for trig in triggers_to_check:
        event_key = f"{trig.value}:{opponent_side_label}"
        outcome = maybe_check_morale(cdef, side, trig, rng)
        fired.append((trig, outcome))
        encounter.morale_events.append(event_key)
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "morale_trigger",
                "trigger": trig.value,
                "opponent_side": opponent_side_label,
                "outcome": outcome.value,
                "post_alive": post_alive,
                "initial": initial,
            },
            component="confrontation",
        )
        logger.info(
            "confrontation.morale_trigger trigger=%s side=%s outcome=%s post_alive=%d initial=%d",
            trig.value,
            opponent_side_label,
            outcome.value,
            post_alive,
            initial,
        )
    return fired


def _apply_flee_consequences(
    encounter,
    cdef,
    fired: list[tuple[MoraleTrigger, MoraleOutcome]],
) -> None:
    """Apply chase/surrender/rout based on morale.flee_consequence.

    No-op if no fired trigger has outcome=flee. Idempotent: relies on
    encounter state not being mutated twice for the same outcome
    (the caller passes a fresh ``fired`` list per beat).

    For surrender/rout: also sets ``encounter.resolved = True`` and
    ``encounter.outcome`` so the encounter ends. For chase: sets
    ``flee_consequence_pending="chase"`` only — full chase escalation
    is a follow-up story (V1 limitation, documented).

    Loud-fail (ValueError) on unknown FleeConsequence values per
    CLAUDE.md "no silent fallbacks" — unknown values surface drift
    immediately.
    """
    if not any(outcome is MoraleOutcome.flee for _, outcome in fired):
        return
    if cdef.morale is None:
        # Defensive: should not happen — fired list is non-empty only when
        # morale is configured. Loud-fail surfaces upstream bugs.
        raise ValueError(
            f"_apply_flee_consequences called with fired outcomes but no morale "
            f"block on '{cdef.label}'"
        )
    consequence = cdef.morale.flee_consequence
    if consequence is FleeConsequence.chase:
        # V1: set pending flag; actual chase launch is a follow-up story.
        # The orchestrator can inspect flee_consequence_pending="chase" to
        # transition to a chase confrontation. No further mutations here.
        encounter.flee_consequence_pending = "chase"
    elif consequence is FleeConsequence.surrender:
        encounter.flee_consequence_pending = "surrender"
        encounter.opponents_disposition = "surrendered"
        if not encounter.resolved:
            encounter.resolved = True
            encounter.outcome = "surrender"
    elif consequence is FleeConsequence.rout:
        encounter.flee_consequence_pending = "rout"
        encounter.opponents_disposition = "routed"
        if not encounter.resolved:
            encounter.resolved = True
            encounter.outcome = "rout"
    else:
        raise ValueError(f"unknown flee_consequence: {consequence!r}")
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "flee_consequence",
            "consequence": consequence.value,
            "side": encounter.encounter_type,
        },
        component="confrontation",
    )
    logger.info(
        "confrontation.flee_consequence consequence=%s side=%s",
        consequence.value,
        encounter.encounter_type,
    )


def _gate_applies_to_encounter(encounter, pack) -> bool:
    """The SOUL gate fires for legacy apply_beat encounters only.

    Sealed-letter dispatch (dogfight) is itself an explicit secret-commit
    UI — both pilots' commits arrive via that flow, not via prose
    extraction. Excluding sealed-letter from the gate avoids breaking the
    dogfight production path while still locking the legacy beat-loop
    against the [S2-BUG] failure mode.
    """
    if encounter is None or pack is None:
        return False
    from sidequest.server.dispatch.confrontation import find_confrontation_def

    cdef = find_confrontation_def(
        pack.rules.confrontations if pack.rules else [],
        encounter.encounter_type,
    )
    if cdef is None:
        # Pack-data inconsistency — let the downstream code raise its own
        # ValueError so the caller sees the real bug. The gate stays off.
        return False
    return cdef.resolution_mode != ResolutionMode.sealed_letter_lookup


def _filter_inferred_pc_beats(
    selections: list,
    encounter,
    *,
    narrating_player: str,
    seated_pc_names: set[str] | None = None,
) -> list:
    """SOUL "The Test" gate (Playtest 2026-04-26 [S2-BUG]).

    Drop every beat selection whose actor is a *seated PC*. Those
    selections are extracted from the narrator's prose — they did NOT
    originate from a ``DICE_THROW`` frame on a player's socket, so they
    fail the explicit-consent contract. NPC beats (opponents, neutrals,
    AND companion NPCs on the player side) are passed through unchanged:
    only seated PCs need the consent contract.

    ``seated_pc_names`` is the set of actor names that map to a seat in
    ``snapshot.player_seats.values()``. When passed, only those names
    trigger the rejection — narrator-driven NPC ally beats (Donut's
    ``defend target='Carl'``, etc.) flow through the gate so downstream
    resolvers can apply them with the same OTEL discipline as opponent
    beats. When omitted (legacy callers / pre-MP saves), the gate falls
    back to the original side-only check (every player-side actor is
    treated as seated).

    Playtest 2026-05-06 (sumpdrake fight, caverns_sunden Grimvault):
    Donut's ``defend target='Carl'`` was being rejected with
    ``inferred_pc_beat_rejected`` even though Donut is a recruited
    NPC — the gate had no way to distinguish him from Carl. After the
    seat-aware fix, companion-NPCs reach the resolver and emit
    ``apply_beat`` + ``encounter.beat_applied`` watcher events; Carl's
    explicit-consent contract still applies for Carl himself.

    Each rejected PC beat emits a span + watcher event so the GM panel
    can see the gate firing. Without OTEL the gate is invisible — and
    "is this fix actually working?" is unanswerable.

    ``narrating_player`` is the player whose narration produced these
    selections (used to label ``source`` as ``narrator_self`` when the
    rejected actor IS the narrating PC, ``peer_narration`` otherwise).
    """
    from sidequest.telemetry.spans import encounter_beat_skipped_span

    kept: list = []
    for sel in selections:
        actor = encounter.find_actor(sel.actor) if encounter is not None else None
        side = actor.side if actor is not None else "unknown"
        if side != "player":
            kept.append(sel)
            continue
        # Seat-aware: a player-side actor that is NOT in the seated-PC
        # manifest is a companion NPC — narrator-driven, no consent
        # contract. Let it through. When the seat manifest isn't
        # supplied, fall back to the original "drop every player-side
        # beat" behavior so legacy callers don't accidentally accept
        # narrator-inferred PC beats.
        if seated_pc_names is not None and sel.actor not in seated_pc_names:
            kept.append(sel)
            continue
        # PC-side beat from narrator extraction — REJECT.
        source = "narrator_self" if sel.actor == narrating_player else "peer_narration"
        reason = "inferred_pc_beat_no_explicit_action"
        with encounter_beat_skipped_span(
            reason=reason,
            actor=sel.actor,
            actor_side=side,
            beat_id=sel.beat_id,
            source=source,
            narrating_player=narrating_player,
        ):
            pass
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "inferred_pc_beat_rejected",
                "actor": sel.actor,
                "actor_side": side,
                "beat_id": sel.beat_id,
                "source": source,
                "narrating_player": narrating_player,
                "reason": reason,
            },
            component="confrontation",
            severity="warning",
        )
        logger.warning(
            "confrontation.inferred_pc_beat_rejected actor=%s source=%s "
            "narrating_player=%s beat_id=%s reason=%s",
            sel.actor,
            source,
            narrating_player,
            sel.beat_id,
            reason,
        )
    return kept


class MagicWorkingParseError(RuntimeError):
    """Raised when ``game_patch.magic_working`` has invalid shape.

    Surfaces three distinct failure modes:
    - snapshot has ``magic_state=None`` but the narrator emitted a working
      (no silent fallback per CLAUDE.md);
    - the dict fails ``MagicWorking`` pydantic validation;
    - the working names an actor that has no instantiated ledger bars
      (call ``magic_state.add_character`` first).
    """


@dataclass
class StatusChangePromotion:
    """A magic threshold crossing promoted to a ``status_changes`` ADD.

    The pipeline reuses the existing ``Status`` renderer downstream — this
    dataclass is only the intermediate shape between
    ``promote_crossings_to_status_changes`` and the snapshot mutation that
    appends a ``Status`` to the actor's ``core.statuses``. Severity is
    carried as a string keyed against ``StatusSeverity[...]`` (the enum's
    member names: ``Scratch`` / ``Wound`` / ``Scar`` / ``Boon``).
    """

    actor: str
    status_text: str
    severity: Literal["Scratch", "Wound", "Scar", "Boon"]


@dataclass
class MagicApplyResult:
    """Aggregate result of applying a ``magic_working`` patch field.

    Wraps the underlying ``ApplyWorkingResult`` (ledger mutations + threshold
    crossings) with the validator's flag list. Returned by
    ``apply_magic_working`` and attached to ``NarrationApplyOutcome.magic``
    so downstream tasks (3.4 status_changes auto-promotion, 3.5 OTEL span)
    can read the threshold crossings + flag severity without re-running
    validation.

    ``auto_fired`` (Phase 5 / Story 47-3) carries any magic confrontations
    whose ``auto_fire_trigger`` matched the actor's post-working bar
    values. The session pipeline iterates this list to dispatch
    ``CONFRONTATION_OUTCOME`` payloads through the existing confrontation
    overlay route. Empty when no triggers fire.
    """

    apply: ApplyWorkingResult
    flags: list[Flag]
    auto_fired: list[tuple[ConfrontationDefinition, str]] = field(  # noqa: F821 — forward ref resolved at runtime
        default_factory=list,
    )

    @property
    def crossings(self) -> list[ThresholdCrossingEvent]:
        return self.apply.crossings


@dataclass(frozen=True)
class RepromptRequest:
    """Returned by the apply step when validator severity is 'reprompt'.

    Carries the directive string the orchestrator should inject into the
    second narrator call's recency zone. Spec 2026-05-20.
    """

    matched_type: str
    declared: str | None
    directive: str


@dataclass
class NarrationApplyOutcome:
    """Aggregate result of applying a NarrationTurnResult to a snapshot.

    Carries the per-dispatch-branch outcome objects so callers can read
    them without re-implementing the dispatch logic. Currently only the
    sealed-letter (dogfight) branch surfaces an outcome — extend with
    additional fields as other branches grow structured returns.

    All fields are ``None`` when the corresponding branch did not fire
    this turn (no encounter, wrong resolution_mode, no beat_selections,
    early-return on non-NarrationTurnResult input, etc.). Callers that
    don't care can ignore the return value entirely — it is purely
    additive over the prior ``None`` return.

    ``magic`` carries the magic-working apply result when the narrator
    emitted a ``magic_working`` field on this turn's ``game_patch``;
    ``None`` otherwise. Tasks 3.4/3.5 read ``magic.crossings`` and
    ``magic.flags`` to drive auto-promotion + OTEL. Direct callers that
    don't care can ignore it.
    """

    sealed_letter: SealedLetterOutcome | None = None
    magic: MagicApplyResult | None = None
    reprompt_request: RepromptRequest | None = None
    classified_intent: str = "unspecified"


def apply_magic_working(*, snapshot: GameSnapshot, patch_field: dict) -> MagicApplyResult:
    """Parse a ``game_patch.magic_working`` dict, validate, and apply.

    Returns ``MagicApplyResult`` aggregating the mutated ledger /
    threshold crossings (via ``ApplyWorkingResult``) with the validator's
    flag list. Raises ``MagicWorkingParseError`` on:

    - ``snapshot.magic_state is None`` (world has no magic config loaded
      but narrator emitted a working — fail loud per CLAUDE.md
      no-silent-fallback);
    - ``patch_field`` failing ``MagicWorking`` pydantic validation;
    - ``apply_working`` raising ``KeyError`` (actor has no instantiated
      character bars — caller must run ``add_character`` first).

    The caller (the narration_apply pipeline branch below) is responsible
    for promoting threshold crossings to ``status_changes`` (Task 3.4)
    and emitting the ``magic.working_applied`` OTEL span (Task 3.5).
    This function intentionally does NEITHER — it is the parse + validate
    + apply seam only.
    """
    if snapshot.magic_state is None:
        raise MagicWorkingParseError("magic_working emitted but world has no magic_state loaded")
    try:
        working = MagicWorking.model_validate(patch_field)
    except ValidationError as e:
        raise MagicWorkingParseError(f"magic_working schema invalid: {e}") from e

    flags = magic_validate(working, snapshot.magic_state.config)

    try:
        apply_result = snapshot.magic_state.apply_working(working)
    except KeyError as e:
        raise MagicWorkingParseError(f"unknown actor: {e}") from e

    # Task 3.5: emit ``magic.working`` OTEL span + watcher publish so the
    # GM panel sees every working land. Build the post-apply ledger
    # snapshot from the bars touched by this working — world-scope bars
    # (no character-scope bar for the cost type, e.g. ``vitality`` on a
    # world that doesn't track it on the character) are tolerated via
    # ``KeyError`` skip per architect plan §3.5: not every cost_type is
    # surfaced as a character-scope bar; that's a config truth, not a
    # silent fallback for a missing-data bug.
    from sidequest.magic.state import BarKey

    ledger_after: dict[str, float] = {}
    for cost_type in working.costs:
        try:
            bar = snapshot.magic_state.get_bar(
                BarKey(
                    scope="character",
                    owner_id=working.actor,
                    bar_id=cost_type,
                )
            )
        except KeyError:
            continue
        ledger_after[cost_type] = bar.value

    with magic_working_span(
        plugin=working.plugin,
        mechanism=working.mechanism,
        actor=working.actor,
        domain=working.domain,
        narrator_basis=working.narrator_basis,
        costs_debited=dict(working.costs),
        flags=flags,
        ledger_after=ledger_after,
        flavor=working.flavor,
        consent_state=working.consent_state,
        item_id=working.item_id,
        alignment_with_item_nature=working.alignment_with_item_nature,
    ):
        # Direct watcher publish so OTEL-less paths (unit tests, headless
        # playtest drivers without a TracerProvider) still see the
        # working on the dashboard event feed. Mirrors the ``encounter``
        # / ``inventory`` dual-path pattern elsewhere in this module.
        _watcher_publish(
            "state_transition",
            {
                "field": "magic_state",
                "op": "working",
                "plugin": working.plugin,
                "actor": working.actor,
                "mechanism_engaged": working.mechanism,
                "domain": working.domain,
                "narrator_basis": working.narrator_basis,
                "costs_debited": dict(working.costs),
                "flags": [f.model_dump() for f in flags],
                "ledger_after": ledger_after,
                "flavor": working.flavor or "",
                "consent_state": working.consent_state or "",
                "item_id": working.item_id or "",
                "alignment_with_item_nature": (
                    float(working.alignment_with_item_nature)
                    if working.alignment_with_item_nature is not None
                    else 0.0
                ),
            },
            component="magic",
        )

    # Phase 5 (Story 47-3): evaluate auto-fire triggers against the
    # actor's post-working bar values. Each firing emits its own watcher
    # event so the GM panel sees the trigger engage; the caller iterates
    # ``result.auto_fired`` to dispatch CONFRONTATION_OUTCOME payloads
    # through the confrontation overlay route.
    actor_prefix = f"character|{working.actor}|"
    actor_bar_values: dict[str, float] = {}
    for k, bar in snapshot.magic_state.ledger.items():
        if k.startswith(actor_prefix):
            _, _, bar_id = k.split("|", 2)
            actor_bar_values[bar_id] = bar.value

    auto_fired = evaluate_auto_fire_triggers(
        confs=snapshot.magic_state.confrontations,
        character_id=working.actor,
        bar_values=actor_bar_values,
    )

    # Lie-detector for "did the engine evaluate auto-fire at all"
    # (sprint 3 cold-subsystem audit). Per-firing events below cover
    # the matched case; silent passes where 0 confrontations fired
    # were indistinguishable from "engine never ran" without this
    # summary event. ``candidates`` counts only confrontations whose
    # bar appears in ``bar_values`` — those are the only ones whose
    # trigger expression can match for this actor this turn.
    candidates = [
        c
        for c in snapshot.magic_state.confrontations
        if c.auto_fire and c.auto_fire_trigger is not None
    ]
    actor_bar_ids = set(actor_bar_values.keys())
    actor_candidates = [
        c
        for c in candidates
        if c.auto_fire_trigger
        and (m := re.match(r"^\s*(\w+)\s*", c.auto_fire_trigger))
        and m.group(1) in actor_bar_ids
    ]
    _watcher_publish(
        "state_transition",
        {
            "field": "magic_state",
            "op": "confrontation_evaluation",
            "actor": working.actor,
            "candidates_total": len(candidates),
            "candidates_for_actor": len(actor_candidates),
            "fired_count": len(auto_fired),
        },
        component="magic",
    )

    for conf, character_id in auto_fired:
        _watcher_publish(
            "state_transition",
            {
                "field": "magic_state",
                "op": "confrontation_fire",
                "confrontation_id": conf.id,
                "actor": character_id,
                "trigger": conf.auto_fire_trigger or "",
            },
            component="magic",
        )
        # Phase 5 wire-first: synthesize a CONFRONTATION payload for
        # each auto-fire so the UI overlay mounts. Magic confrontations
        # do not flow through the StructuredEncounter beat-loop in v1
        # (rounds=1 for the_bleeding_through; the narrator carries the
        # round prose), so we hand-roll the minimum payload the
        # ConfrontationOverlay needs. The payload is drained by the
        # session handler after this turn's apply pipeline returns.
        snapshot.pending_magic_auto_fires.append(
            _build_magic_confrontation_payload(
                conf=conf,
                actor=character_id,
                magic_state=snapshot.magic_state,
            )
        )

    return MagicApplyResult(apply=apply_result, flags=flags, auto_fired=auto_fired)


def _build_magic_confrontation_payload(
    *,
    conf,  # ConfrontationDefinition
    actor: str,
    magic_state,
) -> dict:
    """Synthesize a CONFRONTATION payload for a magic-system auto-fire.

    Magic confrontations don't have StructuredEncounter actors / beats;
    they auto-fire and resolve in narrator prose, and the overlay's
    role is to surface the *fact* of the confrontation + the four
    outcome branches the narrator might select. Player and opponent
    metrics map to the resource_pool primary/secondary; metric values
    snapshot the actor's current bars (clamped to the 0-1 range as
    integers in the UI's 0-10 scale).
    """
    primary = conf.resource_pool.get("primary", "primary")
    secondary = conf.resource_pool.get("secondary", "tension")

    def _bar_value(bar_id: str) -> float:
        from sidequest.magic.state import BarKey

        try:
            return magic_state.get_bar(
                BarKey(scope="character", owner_id=actor, bar_id=bar_id)
            ).value
        except KeyError:
            # Confrontation references a bar the actor's ledger does
            # not have — content drift between confrontations.yaml's
            # resource_pool and the world's ledger_bars schema (typo,
            # cross-world reuse, post-migration save). The payload must
            # still construct (refusing here would orphan the auto-fire
            # and the apply pipeline doesn't tolerate a raise), but the
            # gap MUST be visible to the GM panel — silent fallback to
            # 0.0 was a CLAUDE.md ADD-1 violation Westley flagged in
            # round 2. Surface via watcher event so Sebastien sees the
            # ledger hole at debug time.
            logger.warning(
                "magic.bar_missing_for_payload actor=%s bar_id=%s confrontation_id=%s",
                actor,
                bar_id,
                conf.id,
            )
            _watcher_publish(
                "state_transition",
                {
                    "field": "magic_state",
                    "op": "bar_missing_for_payload",
                    "actor": actor,
                    "bar_id": bar_id,
                    "confrontation_id": conf.id,
                },
                component="magic",
                severity="warning",
            )
            return 0.0

    primary_value = _bar_value(primary)
    return {
        "type": conf.id,
        "label": conf.label,
        "category": "magic_confrontation",
        "actors": [{"name": actor, "role": "channeler"}],
        "player_metric": {
            "name": primary,
            "current": int(primary_value * 10),
            "starting": int(primary_value * 10),
            "threshold": 10,
        },
        "opponent_metric": {
            "name": secondary,
            "current": 5,
            "starting": 0,
            "threshold": 10,
        },
        "beats": [],
        "secondary_stats": None,
        "genre_slug": magic_state.config.genre_slug,
        "mood": "haunted",
        "active": True,
    }


def _promote_pool_member_to_npc(member: NpcPoolMember) -> Npc:
    """Build an ``Npc`` from an ``NpcPoolMember``, preserving identity
    (name, pronouns, appearance, role) and recording ``pool_origin`` so
    Sebastien's mechanical-visibility lens can trace the NPC back to the
    pool entry it was promoted from. Stat block is the same placeholder
    shape ``Session._npc_from_patch`` uses — fresh edge pool, empty
    inventory, level 1.
    """
    from sidequest.game.creature_core import (
        CreatureCore,
        Inventory,
        placeholder_edge_pool,
    )

    core = CreatureCore(
        name=member.name,
        description=member.appearance or "No description",
        personality=member.role or "Unknown",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        edge=placeholder_edge_pool(),
    )
    return Npc(
        core=core,
        pronouns=member.pronouns,
        appearance=member.appearance,
        pool_origin=member.name,
    )


def resolve_status_target(
    snapshot: GameSnapshot,
    *,
    actor_name: str,
    turn_num: int,
    trigger: str,
):
    """Resolve a status-mutation actor name to a creature whose
    ``core.statuses`` can be appended to or popped from.

    Search order:
    1. ``snapshot.characters`` — PCs.
    2. ``snapshot.npcs`` — mechanically-active NPCs.
    3. ``snapshot.npc_pool`` — auto-registered or world-authored pool
       members. A pool hit is *promoted* to ``Npc`` (per Wave 2A docs:
       "when the same name engages mechanically … an Npc is created with
       pool_origin = self.name") so the status can land on a real
       ``CreatureCore``. The pool entry is left in place — it remains a
       re-citable cast member, shadowed by the ``Npc`` lookup.

    Returns ``None`` when the name doesn't match any of the three; the
    caller emits its own unknown-actor warning so the warning label can
    distinguish add (``status_change.unknown_actor``) from clear
    (``status_clear.unknown_actor``).

    Playtest 2026-05-09 fix: previously this lookup was hand-rolled at
    each call site against ``snapshot.characters`` only, so injuries
    minted on auto-registered NPCs (e.g. the dying delver in Sünden)
    silently fell on the floor with ``status_change.unknown_actor``.
    """
    for ch in snapshot.characters:
        if ch.core.name == actor_name:
            return ch
    for npc in snapshot.npcs:
        if npc.core.name == actor_name:
            return npc
    pool_match = next(
        (m for m in snapshot.npc_pool if m.name == actor_name),
        None,
    )
    if pool_match is None:
        return None
    promoted = _promote_pool_member_to_npc(pool_match)
    snapshot.npcs.append(promoted)
    _watcher_publish(
        "state_transition",
        {
            "field": "npcs",
            "op": "promoted_from_pool",
            "name": pool_match.name,
            "pool_origin": pool_match.name,
            "drawn_from": pool_match.drawn_from,
            "trigger": trigger,
            "turn": turn_num,
        },
        component="npc_pool",
    )
    logger.info(
        "npc.promoted_from_pool name=%r trigger=%s turn=%d",
        pool_match.name,
        trigger,
        turn_num,
    )
    return promoted


def _append_status_to_actor(
    *,
    target,
    actor: str,
    text: str,
    severity,
    source: str,
    turn_num: int,
    encounter_type: str | None,
) -> None:
    """Append a ``Status`` to ``target.core.statuses`` and emit OTEL.

    Centralizes the side-effect shape shared by the narrator-extracted
    ``status_changes`` path and the magic-threshold-promotion path: both
    build the same ``Status`` record, open ``encounter_status_added_span``,
    and publish the same ``state_transition`` watcher event. The only
    caller-visible difference is ``source`` (``narrator_extraction`` vs.
    ``magic_threshold_promotion``), which keeps Sebastien's mechanical-
    visibility lens able to distinguish "narrator said so" from "bar
    fired auto-promotion".

    Each caller is responsible for resolving ``target`` (the
    ``Character``) and emitting its own unknown-actor warning before
    calling — the warning labels differ between the two paths and a
    server-side test asserts the narrator-path warning text exactly.
    """
    from sidequest.game.status import Status
    from sidequest.telemetry.spans import encounter_status_added_span

    target.core.statuses.append(
        Status(
            text=text,
            severity=severity,
            absorbed_shifts=0,
            created_turn=turn_num,
            created_in_encounter=encounter_type,
        )
    )
    with encounter_status_added_span(
        actor=actor,
        text=text,
        severity=severity.value,
        source=source,
    ):
        pass
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "status_added",
            "actor": actor,
            "text": text,
            "severity": severity.value,
            "source": source,
            "turn": turn_num,
            "encounter_type": encounter_type,
        },
        component="encounter",
    )


def _apply_magic_status_promotions(
    *,
    snapshot: GameSnapshot,
    magic_result: MagicApplyResult,
    player_name: str,
) -> None:
    """Apply Task 3.4 status promotions to ``snapshot.characters``.

    Mirrors the side-effect shape of the manual ``result.status_changes``
    branch below (``Status`` append + watcher publish) so the GM panel
    sees auto-promoted statuses on the same lane as narrator-extracted
    ones. ``source="magic_threshold_promotion"`` distinguishes them in
    the watcher feed — Sebastien's mechanical-visibility lens demands
    that auto-fired statuses are traceable back to the bar that fired
    them, not blurred into "narrator said so".

    Does not raise: a missing actor character (BarKey owner_id with no
    matching ``core.name``) logs and skips. The MagicState ledger and
    the Character roster are populated from different paths (chargen vs.
    add_character) and a soft mismatch shouldn't crash the apply
    pipeline mid-turn.
    """
    from sidequest.game.status import StatusSeverity

    promotions = promote_crossings_to_status_changes(result=magic_result, snapshot=snapshot)
    if not promotions:
        return

    turn_num = snapshot.turn_manager.interaction
    encounter_type = snapshot.encounter.encounter_type if snapshot.encounter else None
    for promo in promotions:
        target = next(
            (c for c in snapshot.characters if c.core.name == promo.actor),
            None,
        )
        if target is None:
            logger.warning(
                "magic.status_promotion_unknown_actor actor=%s text=%s "
                "player=%s — bar fired but no matching character.core.name",
                promo.actor,
                promo.status_text,
                player_name,
            )
            continue
        _append_status_to_actor(
            target=target,
            actor=promo.actor,
            text=promo.status_text,
            severity=StatusSeverity[promo.severity],
            source="magic_threshold_promotion",
            turn_num=turn_num,
            encounter_type=encounter_type,
        )


def promote_crossings_to_status_changes(
    *, result: MagicApplyResult, snapshot: GameSnapshot
) -> list[StatusChangePromotion]:
    """Convert ``MagicApplyResult.crossings`` into status-change promotions.

    Reads the per-bar ``promote_to_status`` config from the world's
    ``LedgerBarSpec`` — NOT a hardcoded module-level dict (architect §5.3,
    2026-04-29). This keeps status text/severity world-tunable: a different
    innate-using world (e.g. tea_and_murder-touched) can map ``sanity`` →
    ``"Slipping"``, ``Scar`` without code change. A bar without
    ``promote_to_status`` is silently skipped — the architect explicitly
    calls this out as the right behavior, not a fallback (not every bar
    surfaces as a Status; world-scope bars never do).
    """
    if snapshot.magic_state is None:
        return []

    promotions: list[StatusChangePromotion] = []
    bars_by_id = {b.id: b for b in snapshot.magic_state.config.ledger_bars}

    for crossing in result.crossings:
        spec = bars_by_id.get(crossing.bar_key.bar_id)
        if spec is None or spec.promote_to_status is None:
            # Architect §5.3: silent skip is correct — not every bar
            # promotes. ``spec is None`` would be a config inconsistency
            # (crossing references a bar id not in the config), but the
            # crossing itself was emitted by ``apply_working`` reading
            # the same config, so this branch is structurally
            # unreachable for in-config bars. Keep it defensive without
            # raising — Task 3.5 will add an OTEL span if it ever fires.
            continue
        promotions.append(
            StatusChangePromotion(
                actor=crossing.bar_key.owner_id,
                status_text=spec.promote_to_status.text,
                severity=spec.promote_to_status.severity,
            )
        )
    return promotions


def _apply_npc_mentions(
    *,
    snapshot: GameSnapshot,
    mentions: list[Any],
    turn_num: int,
    acting_character_name: str | None = None,
) -> None:
    """Apply narrator NPC mentions via 3-step lookup (Wave 2A, story 45-47).

    Order:
      0. PC-name pre-filter (existing). PC names skip the loop entirely.
      1. ``snapshot.npcs`` (case-folded name match). On hit: update
         ``Npc.last_seen_*``; run drift detection. Do NOT overwrite Npc
         identity fields — those have authoritative state of their own.
      2. ``snapshot.npc_pool`` (case-folded name match). On hit: additive
         upsert role/pronouns/appearance onto the pool member; existing
         values win on conflict. Pool members are not consumed — re-citable.
      3. Novel name. Append a new ``NpcPoolMember(drawn_from=
         "narrator_invented")`` to ``snapshot.npc_pool``.

    Every cite (after PC skip) emits ``SPAN_NPC_REFERENCED`` with
    ``match_strategy ∈ {npcs_hit, pool_hit, invented}`` and ``pool_origin``.
    The novel branch ALSO emits the existing ``SPAN_NPC_AUTO_REGISTERED``
    span (preserved from pre-Wave-2A telemetry — registry_len now reports
    pool length).
    """
    pc_name_lookup = {
        c.core.name.lower(): c.core.name
        for c in snapshot.characters
        if getattr(getattr(c, "core", None), "name", None)
    }

    for mention in mentions:
        matched_pc = pc_name_lookup.get(mention.name.lower())
        if matched_pc is not None:
            with npc_pc_name_skipped_span(
                npc_name=mention.name,
                matched_pc=matched_pc,
                turn_number=turn_num,
            ):
                logger.info(
                    "npc.pc_name_skipped name=%r matched_pc=%r turn=%d",
                    mention.name,
                    matched_pc,
                    turn_num,
                )
            continue

        name_key = mention.name.casefold()

        # Step 1: existing Npc shadows everything else.
        npc_hit: Npc | None = None
        for npc in snapshot.npcs:
            if npc.core.name.casefold() == name_key:
                npc_hit = npc
                break
        if npc_hit is not None:
            # ``Npc`` has no string ``role`` field (only the archetype-id
            # ``npc_role_id``, which is not narrator-cited prose). Pass
            # ``None`` so the drift detector skips the role check; pronouns
            # drift is still meaningful.
            _detect_npc_identity_drift(
                existing_name=npc_hit.core.name,
                existing_role=None,
                existing_pronouns=npc_hit.pronouns,
                mention=mention,
                turn_num=turn_num,
            )
            actor_loc = snapshot.party_location(perspective=acting_character_name)
            if actor_loc:
                npc_hit.last_seen_location = actor_loc
            npc_hit.last_seen_turn = turn_num
            with npc_referenced_span(
                npc_name=mention.name,
                match_strategy="npcs_hit",
                pool_origin=npc_hit.pool_origin,
                turn_number=turn_num,
            ):
                logger.info(
                    "npc.referenced name=%r match=npcs_hit pool_origin=%r turn=%d",
                    mention.name,
                    npc_hit.pool_origin,
                    turn_num,
                )
            continue

        # Step 2: pool member match.
        pool_hit: NpcPoolMember | None = None
        for member in snapshot.npc_pool:
            if member.name.casefold() == name_key:
                pool_hit = member
                break
        if pool_hit is not None:
            _detect_npc_identity_drift(
                existing_name=pool_hit.name,
                existing_role=pool_hit.role,
                existing_pronouns=pool_hit.pronouns,
                mention=mention,
                turn_num=turn_num,
            )
            # Additive upsert — fill empty identity fields from the mention,
            # never overwrite a value already set.
            if mention.role and not pool_hit.role:
                pool_hit.role = mention.role
            if mention.pronouns and not pool_hit.pronouns:
                pool_hit.pronouns = mention.pronouns
            if mention.appearance and not pool_hit.appearance:
                pool_hit.appearance = mention.appearance
            with npc_referenced_span(
                npc_name=mention.name,
                match_strategy="pool_hit",
                pool_origin=pool_hit.name,
                turn_number=turn_num,
            ):
                logger.info(
                    "npc.referenced name=%r match=pool_hit turn=%d",
                    mention.name,
                    turn_num,
                )
            continue

        # Step 3: novel — narrator invented a name not in any store.
        new_member = NpcPoolMember(
            name=mention.name,
            role=mention.role or None,
            pronouns=mention.pronouns or None,
            appearance=mention.appearance or None,
            archetype_id=None,
            drawn_from="narrator_invented",
        )
        snapshot.npc_pool.append(new_member)
        with npc_referenced_span(
            npc_name=mention.name,
            match_strategy="invented",
            pool_origin=None,
            turn_number=turn_num,
        ):
            logger.info(
                "npc.referenced name=%r match=invented turn=%d",
                mention.name,
                turn_num,
            )
        # Preserve pre-Wave-2A auto-registered telemetry — the
        # ``WatcherSpanProcessor`` re-emits the state_transition event
        # via ``SPAN_ROUTES[SPAN_NPC_AUTO_REGISTERED]``. ``registry_len``
        # now reflects pool length.
        with npc_auto_registered_span(
            npc_name=mention.name,
            pronouns=mention.pronouns or "",
            role=mention.role or "",
            turn_number=turn_num,
            registry_len=len(snapshot.npc_pool),
        ):
            logger.info(
                "npc.auto_registered name=%r pronouns=%r role=%r turn=%d",
                mention.name,
                mention.pronouns or "",
                mention.role or "",
                turn_num,
            )


def _apply_course_sidecar(
    *,
    snapshot: GameSnapshot,
    result: object,
    room: SessionRoom,
) -> None:
    """Parse and apply a plot_course / cancel_course sidecar from game_patch_dict.

    Called from _apply_narration_result_to_snapshot before the encounter
    lifecycle block. Skips silently when:
    - result has no game_patch_dict (non-NarrationTurnResult or empty patch)
    - game_patch_dict carries no course intent (parse_course_sidecar returns None)
    - room.session has no orbital_content (non-orbital world)
    """
    from sidequest.agents.orchestrator import NarrationTurnResult

    if not isinstance(result, NarrationTurnResult):
        return
    patch_dict = result.game_patch_dict
    if not patch_dict:
        return

    from sidequest.handlers.course_intent import handle_course_sidecar
    from sidequest.orbital.course import _bodies_in_scope, compute_courses
    from sidequest.protocol.course_intent import (
        CancelCourseSidecar,
        PlotCourseSidecar,
        parse_course_sidecar,
    )
    from sidequest.telemetry.spans.course import (
        emit_course_cancel,
        emit_course_plot_accepted,
        emit_course_plot_rejected,
    )

    course_sidecar = parse_course_sidecar(patch_dict)
    if course_sidecar is None:
        return

    session = room.session
    if session.orbital_content is None:
        logger.debug(
            "course_sidecar.skipped intent=%s reason=no_orbital_content",
            course_sidecar.intent,
        )
        return

    in_scope = _bodies_in_scope(
        session.orbital_content.orbits,
        session.orbital_scope,
    )
    available = compute_courses(
        orbits=session.orbital_content.orbits,
        party_at=snapshot.party_body_id,
        in_scope_body_ids=in_scope,
        recent_body_mentions=list(session.recent_body_mentions),
        quest_anchors=list(snapshot.quest_anchors),
    )
    handler_result = handle_course_sidecar(
        sidecar=course_sidecar,
        snapshot=snapshot,
        available_courses=available,
    )

    if isinstance(course_sidecar, PlotCourseSidecar):
        if handler_result.accepted:
            emit_course_plot_accepted(
                from_body=snapshot.party_body_id,
                course=snapshot.plotted_course,
            )
            logger.info(
                "course.plot.accepted course_id=%r from_body=%r eta_hours=%s dv=%s",
                course_sidecar.course_id,
                snapshot.party_body_id,
                snapshot.plotted_course.eta_hours if snapshot.plotted_course else None,
                snapshot.plotted_course.delta_v if snapshot.plotted_course else None,
            )
        else:
            emit_course_plot_rejected(
                course_id=course_sidecar.course_id,
                reason=handler_result.reason,
                available_ids=sorted(available.keys()),
            )
            logger.warning(
                "course.plot.rejected course_id=%r reason=%s available=%s",
                course_sidecar.course_id,
                handler_result.reason,
                sorted(available.keys()),
            )
            # NOTE: reactions-hint injection skipped — add_reaction_for_next_turn
            # does not exist on Session. See task instructions: escalated to
            # Bundle 6 / dedicated reactions mechanism bundle.
    elif isinstance(course_sidecar, CancelCourseSidecar):
        emit_course_cancel(
            was_already_clear=handler_result.was_already_clear,
        )
        logger.info(
            "course.cancel was_already_clear=%s",
            handler_result.was_already_clear,
        )


def _apply_morale_sidecar(
    *,
    snapshot: GameSnapshot,
    result: object,
    pack: GenrePack | None,
) -> None:
    """Apply a ``morale_event`` sidecar from game_patch_dict to the active encounter.

    Called from ``_apply_narration_result_to_snapshot`` after the course
    sidecar handler and before the encounter beat loop.

    Skips silently when:
    - result has no game_patch_dict (non-NarrationTurnResult or empty patch)
    - game_patch_dict carries no ``morale_event`` key
    - no active encounter on the snapshot
    - pack is None or encounter type not in pack rules
    - confrontation has no morale block (morale=None)

    Raises ValueError on unknown ``morale_event`` values (loud-fail per
    ADR-039 and CLAUDE.md "no silent fallbacks" — unknown values surface
    narrator drift immediately).
    """
    from sidequest.agents.orchestrator import NarrationTurnResult

    if not isinstance(result, NarrationTurnResult):
        return
    patch_dict = result.game_patch_dict
    if not patch_dict:
        return

    sidecar_morale_event = patch_dict.get("morale_event")
    if sidecar_morale_event is None:
        return

    _KNOWN_SIDECAR_MORALE_EVENTS = {"intimidated"}
    if sidecar_morale_event not in _KNOWN_SIDECAR_MORALE_EVENTS:
        raise ValueError(
            f"narrator sidecar morale_event={sidecar_morale_event!r} not recognized; "
            f"known values: {sorted(_KNOWN_SIDECAR_MORALE_EVENTS)}"
        )

    enc = snapshot.encounter
    if enc is None or enc.resolved:
        return

    if pack is None or pack.rules is None:
        return

    from sidequest.server.dispatch.confrontation import find_confrontation_def

    cdef = find_confrontation_def(pack.rules.confrontations, enc.encounter_type)
    if cdef is None or cdef.morale is None:
        # Confrontation has no morale block — no morale check possible.
        return

    if sidecar_morale_event == "intimidated":
        event_key = f"intimidated:{enc.encounter_type}"
        if event_key not in enc.morale_events:
            opp_actors = [a for a in enc.actors if a.side == "opponent"]
            side = OpponentSideState(
                label=enc.encounter_type,
                opponents=[OpponentState(id=a.name, alive=True) for a in opp_actors],
            )
            outcome = maybe_check_morale(cdef, side, MoraleTrigger.intimidated, Random())
            enc.morale_events.append(event_key)
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "morale_trigger",
                    "trigger": "intimidated",
                    "opponent_side": enc.encounter_type,
                    "outcome": outcome.value,
                    "source": "sidecar",
                },
                component="confrontation",
            )
            logger.info(
                "confrontation.morale_trigger trigger=intimidated side=%s outcome=%s source=sidecar",
                enc.encounter_type,
                outcome.value,
            )
            # Task 11: apply flee consequence if intimidated roll returned flee.
            sidecar_fired: list[tuple[MoraleTrigger, MoraleOutcome]] = [
                (MoraleTrigger.intimidated, outcome)
            ]
            _apply_flee_consequences(enc, cdef, sidecar_fired)


def resolve_item_recipient(
    snapshot: GameSnapshot,
    entry: dict[str, object],
    *,
    narrating_character_name: str,
    lane: str,
) -> Character:
    """ADR-108: resolve which seated PC a narrator item entry belongs to.

    Sealed MP rounds (ADR-036) have no single "acting player" and
    inventory is per-player (ADR-037), so the recipient is an explicit
    narrator-supplied signal (``entry["recipient"]`` — the same
    convention as ``BeatSelection.actor``) validated against the seated
    set. Resolution modes, mirrored onto every per-item ``inventory``
    watcher so the GM panel can answer "did Catalina actually get the
    medpatch, or did the narrator wing it?":

    - ``tagged`` — ``recipient`` names a seated PC (∈
      ``player_seats.values()``) → that Character. Also the
      single-player / empty-manifest lone PC: behaviour unchanged, NOT a
      contract violation (ADR-108 §2).
    - ``recipient_missing`` — ``recipient`` absent/empty in a seated
      round (narrator contract violation).
    - ``non_seated_recipient`` — ``recipient`` present but not a seated
      PC (narrator contract violation).

    On either violation the item degrades to the **narrating socket's
    PC** (``narrating_character_name``) — deterministic and observable —
    and a loud ``inventory`` / ``recipient_missing`` watcher
    (severity=warning) fires. It is NEVER attributed to
    ``snapshot.characters[0]`` as a positional default in a seated round
    (that positional default IS the ADR-108 bug); the narrated item is
    never dropped (*Yes, And*).

    Always emits one per-item ``item_recipient_resolved`` watcher
    (severity=info, ``component=inventory``) naming the resolved
    recipient + lane + mode — the CLAUDE.md OTEL lie-detector for
    inventory attribution.
    """
    chars = snapshot.characters

    def _by_name(name: str) -> Character | None:
        for c in chars:
            if c.core.name == name:
                return c
        return None

    seated = {n for n in snapshot.player_seats.values() if n}
    offered = str(entry.get("recipient", "") or "").strip()
    item_name = str(entry.get("name", "") or "").strip()

    if not seated:
        # Single-player / pre-MP save: the lone first character is the
        # only resolution. Behaviour is unchanged and this is NOT a
        # contract violation — the loud watcher stays silent so
        # single-player saves don't pollute the GM panel (ADR-108 §2).
        resolved = chars[0]
        mode = "tagged"
    elif offered and (_seated_match := _by_name(offered)) is not None and offered in seated:
        resolved = _seated_match
        mode = "tagged"
    else:
        mode = "non_seated_recipient" if offered else "recipient_missing"
        # Absent-recipient rule (ADR-108 §3): degrade to the narrating
        # socket's PC — never characters[0].
        fallback = _by_name(narrating_character_name)
        if fallback is None:
            # Deep name-skew: the narrating socket's PC is absent from
            # the snapshot's character list. Unreachable in a normal
            # sealed round (the narrating socket always seats a PC), but
            # we must neither drop the narrated item (*Yes, And*) nor
            # crash the apply pipeline. Fail LOUD and degrade to the
            # first character as the only remaining non-dropping option.
            logger.warning(
                "inventory.narrating_pc_unresolved narrating=%r seated=%s "
                "offered=%r lane=%s — degrading to first character",
                narrating_character_name,
                sorted(seated),
                offered,
                lane,
            )
            fallback = chars[0]
        resolved = fallback
        _watcher_publish(
            "state_transition",
            {
                "field": "inventory",
                "op": "recipient_missing",
                "resolution_mode": mode,
                "lane": lane,
                "item": item_name,
                "offered_recipient": offered,
                "fallback_recipient": resolved.core.name,
                "narrating_character": narrating_character_name,
                "seated": sorted(seated),
            },
            component="inventory",
            severity="warning",
        )

    _watcher_publish(
        "state_transition",
        {
            "field": "inventory",
            "op": "item_recipient_resolved",
            "resolution_mode": mode,
            "lane": lane,
            "item": item_name,
            "recipient": resolved.core.name,
        },
        component="inventory",
        severity="info",
    )
    return resolved


def _apply_narration_result_to_snapshot(
    snapshot: GameSnapshot,
    result: object,
    player_name: str,
    *,
    room: SessionRoom,
    pack: GenrePack | None = None,
    dice_failed: bool | None = None,
    dice_actor: str | None = None,
    from_explicit_action: bool = False,
    opposed_player_d20: int | None = None,
    opposed_player_beat_id: str | None = None,
    opposed_player_actor: str | None = None,
    acting_character_name: str | None = None,
    already_reprompted: bool = False,
) -> NarrationApplyOutcome:
    """Apply narrator-extracted fields to the snapshot.

    Phase 1: location, quest_updates, lore_established, npc_pool / npcs
    upsert, inventory items_gained / items_lost.
    Story 3.4: encounter instantiation and beat application (when pack provided).

    ``dice_failed=True`` / ``False`` signals a dice-replay turn — the dice
    is the mechanical event for the rolling player. ``None`` means no dice
    this turn (free-text turn; narrator's beat_selections stand on their
    declared tier).

    ``dice_actor`` is the rolling actor's name (paired with ``dice_failed``).
    On a dice-replay turn, only that actor's beat selection is filtered out —
    ``dispatch_dice_throw`` already applied it. Other actors' selections
    (typically opponent-side NPCs the narrator routes the round-trip through)
    still apply so the opponent dial can advance and combat is two-sided.
    Playtest 2026-04-25 [P0]: prior behavior dropped *all* selections,
    leaving the opponent dial inert and combat structurally unresolvable.

    ``from_explicit_action`` is False on the production session-handler
    path (the only real call site routes narrator-extracted prose). The
    SOUL-gate (Playtest 2026-04-26 [S2-BUG]) drops every PC-side beat
    selection in that mode and emits ``confrontation
    .inferred_pc_beat_rejected`` watcher events — PC mechanical actions
    MUST trace back to an explicit DICE_THROW frame, never to a peer or
    self narration. Test helpers that simulate the dispatch path may set
    ``from_explicit_action=True`` to bypass the gate.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult

    outcome = NarrationApplyOutcome()
    # Default classified_intent from raw action_rewrite.intent — the
    # validator dispatch below may overwrite with matched_type on mismatch.
    _initial_intent = ""
    if isinstance(result, NarrationTurnResult):
        ar = getattr(result, "action_rewrite", None)
        if ar is not None:
            _initial_intent = (getattr(ar, "intent", "") or "").strip()
    outcome.classified_intent = _initial_intent or "unspecified"

    if not isinstance(result, NarrationTurnResult):
        return outcome

    # Magic working (Coyote Star iter 3 — Task 3.3). Ordered ahead of
    # the location/quest/inventory/encounter branches so the
    # ``magic.working_applied`` OTEL span Task 3.5 will add timestamps
    # before any downstream snapshot mutation — the GM panel reads
    # magic-resolution as the first event of the turn, paired tightly
    # with the prose that produced it. The parse-error path is
    # *swallowed* below (logged + continue) by design: narration is
    # already in the user's hands, so we never crash the apply pipeline
    # on a malformed working — Task 3.5 will promote that log to a
    # ``magic.parse_error`` span. Threshold-crossing → status_changes
    # auto-promotion (Task 3.4) is wired below in the ``else`` branch;
    # the ``magic.working_applied`` OTEL span itself (Task 3.5) is still
    # pending — see that task for the wire-up.
    magic_working_field = getattr(result, "magic_working", None)
    if magic_working_field is not None:
        try:
            outcome.magic = apply_magic_working(snapshot=snapshot, patch_field=magic_working_field)
        except MagicWorkingParseError as e:
            # Log + continue — narration is already delivered; the parse
            # error must not crash the apply pipeline. Task 3.5 will
            # promote this to a ``magic.parse_error`` OTEL span so the
            # GM panel sees it. Until then, structured logging keeps the
            # failure auditable.
            logger.warning(
                "magic.parse_error player=%s reason=%s",
                player_name,
                e,
            )
        else:
            # Task 3.4: auto-promote threshold crossings into Status.
            # Reuses the existing Status renderer downstream — no new UI.
            # The world's per-bar ``promote_to_status`` block decides the
            # status text + severity (architect §5.3); bars without that
            # block produce no promotion (silent skip is intended).
            _apply_magic_status_promotions(
                snapshot=snapshot,
                magic_result=outcome.magic,
                player_name=player_name,
            )

    # Story 49-3: location-drift repair. Glenross playtest 2026-05-11 —
    # the narrator wrote new ``**Room Title**`` markdown headers across
    # five turns without filling the structured ``patch.location`` field.
    # State held ``the_manse`` for turns 1-5 while prose moved through
    # four different rooms; the GM panel couldn't tell where the party
    # was (SOUL.md "Illusionism" failure mode). The repair: when the
    # narrator left ``result.location`` empty AND the prose opens with a
    # bold room title that disagrees with current state, auto-promote the
    # title into ``result.location`` so the existing apply pipeline (the
    # ``if result.location:`` branch below) writes the canonical entry.
    #
    # Auto-fill, not fail-loud: blocking a turn is more expensive than
    # an audited repair. ``narrator.location_drift_repaired`` is the
    # load-bearing observability hook — Sebastien's GM panel surfaces
    # every repair so the prompt can be iterated to prevent the drift
    # at its source (Recency-zone guardrail in build_narrator_prompt).
    if not result.location and result.narration:
        _actor_for_drift = acting_character_name or player_name
        if _actor_for_drift:
            _candidate = _extract_leading_bold_title(result.narration)
            if _candidate is not None:
                _current = snapshot.character_locations.get(_actor_for_drift)
                if _candidate != _current:
                    logger.warning(
                        "narrator.location_drift_repaired character=%s "
                        "old_state=%r new_from_title=%r turn=%d player=%s",
                        _actor_for_drift,
                        _current,
                        _candidate,
                        snapshot.turn_manager.interaction,
                        player_name,
                    )
                    with location_drift_repaired_span(
                        old_state=_current or "",
                        new_from_title=_candidate,
                        character=_actor_for_drift,
                        player_name=player_name,
                        turn=snapshot.turn_manager.interaction,
                    ):
                        result.location = _candidate

    if result.location:
        # Wave 2B (story 45-48): per-character locations are the only
        # source of truth. The previous "snapshot the global before
        # clobbering" seed loop is gone — there is no global. Compute the
        # acting PC's prior location for scene-change detection below.
        # ``acting_character_name`` is the canonical actor identity;
        # legacy callers (older tests, dispatch paths that haven't been
        # threaded yet) pass the actor as ``player_name`` instead — fall
        # back to that so the apply path still records location updates
        # rather than silently dropping them.
        actor_for_location = acting_character_name or player_name
        old_loc = (
            snapshot.character_locations.get(actor_for_location) if actor_for_location else None
        )
        # Story 47-4: rig-coupled auto-fire hook. Any narrator-emitted
        # location change runs through process_room_entry, which resolves
        # bare world-name rooms ("Galley") against chassis.interior_rooms
        # and dispatches eligible auto-fire confrontations (e.g. the_tea_brew
        # on Galley entry with bond_tier >= familiar). Non-chassis rooms are
        # silent no-ops on this path — the legacy room-graph machinery
        # (init_room_graph_location, region graph) handles those.
        if acting_character_name and snapshot.chassis_registry:
            from sidequest.game.room_movement import process_room_entry

            process_room_entry(
                snapshot,
                character_id=acting_character_name,
                room_id=result.location,
                current_turn=snapshot.turn_manager.interaction,
            )
        # Bind this turn's location to the acting character. Legacy
        # callers that haven't been threaded with ``acting_character_name``
        # fall back to ``player_name`` (which has historically held the
        # character name in this seam). The existing observability log
        # line below records the narrator's emit either way.
        if actor_for_location:
            snapshot.character_locations[actor_for_location] = result.location
            _watcher_publish(
                "state_transition",
                {
                    "kind": "character_location_updated",
                    "character": actor_for_location,
                    "old_location": old_loc,
                    "new_location": result.location,
                    "player_name": player_name,
                },
                component="game",
            )
        # Story 45-16: filter narrator-emitted location before adding
        # to the region graph. Playtest 3 leaked
        # `(aside — narrator brief)` into discovered_regions because
        # this seam appended unconditionally. Reject + emit OTEL so
        # Sebastien's lie-detector sees the filter fire.
        is_valid_region, rejection_reason = validate_region_name(result.location)
        if not is_valid_region:
            with region_entry_rejected_span(
                entry=result.location,
                reason=rejection_reason or "unknown",
                caller_path="narration_apply.location_update",
                player_name=player_name,
            ):
                logger.warning(
                    "region.entry_rejected reason=%s entry=%r player=%s caller=narration_apply.location_update",
                    rejection_reason,
                    result.location,
                    player_name,
                )
        else:
            # Story 45-17: canonical-slug dedup. The narrator emits
            # surface variants for the same room across turns
            # (Felix's Playtest 3: "The Crew Quarters" vs "the crew
            # quarters"); compare slugs, not raw strings.
            new_slug = canonicalize_region_name(result.location)
            existing_match: str | None = None
            for existing in snapshot.discovered_regions:
                if canonicalize_region_name(existing) == new_slug:
                    existing_match = existing
                    break
            if existing_match is None:
                snapshot.discovered_regions.append(result.location)
            elif existing_match != result.location:
                # Surface variants — emit dedup span so the GM panel
                # sees the merge fire (CLAUDE.md OTEL principle).
                with region_entry_canonicalized_dedup_span(
                    entry=result.location,
                    canonical_slug=new_slug,
                    existing_surface_form=existing_match,
                    caller_path="narration_apply.location_update",
                    player_name=player_name,
                ):
                    logger.info(
                        "region.entry_canonicalized_dedup entry=%r existing=%r slug=%s caller=narration_apply.location_update",
                        result.location,
                        existing_match,
                        new_slug,
                    )
        logger.info(
            "state.location_update old=%r new=%r player=%s",
            old_loc,
            result.location,
            player_name,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "location",
                "before": old_loc,
                "after": result.location,
                "player_name": player_name,
                "turn_number": snapshot.turn_manager.interaction,
                "discovered_count": len(snapshot.discovered_regions),
            },
            component="state.location",
        )
        # Scratch sweep on scene change. A location change is a scene
        # boundary by every TTRPG convention — the cough you took in the
        # previous room shouldn't pile onto the cough you take in the
        # next one (Playtest 2026-04-26 Bug #1). Wound and Scar persist;
        # only Scratch clears. ``old_loc`` is None at session start —
        # don't sweep on the first location set (no scene to leave).
        if old_loc and old_loc != result.location:
            from sidequest.server.status_clear import clear_scratch_on_scene_end

            clear_scratch_on_scene_end(
                snapshot,
                reason="location_change",
                turn=snapshot.turn_manager.interaction,
            )

            # Pingpong 2026-04-30: confrontation panel sticks open after
            # the party physically leaves the encounter location.
            # Repro: party negotiates with Inspector Karenina in her
            # office, then walks out — location updates correctly to
            # the corridor, then the freight stair — but the
            # Confrontation tab still shows the Diplomatic Negotiation
            # active with the four beat buttons clickable. Karenina is
            # two floors up; clicking "Threaten" generates puppet
            # narration of an interaction that can't physically happen
            # (Sebastien-class GM-panel-vs-world-state divergence).
            #
            # Fix: a location change is a scene boundary by tabletop
            # convention. If an encounter is still active when the
            # party leaves the room, mark it resolved as
            # `abandoned_on_location_change`. The existing dispatch
            # branch in websocket_session_handler.py (`elif prior_live
            # and not now_live:`) detects the resolved=True flip and
            # builds a CONFRONTATION { active: false } clear payload —
            # post my pingpong-2026-04-30-confrontation-broadcast fix
            # this reaches every current socket including the
            # dispatcher's reconnected one. No new wire message
            # needed; we just trigger the existing clear path.
            #
            # Caveat — chase encounters legitimately move with the
            # party (location changes WITHIN the encounter). The
            # genre pack carries `category` metadata on each
            # confrontation def; rather than re-look it up here on
            # the apply path (which would mean threading the
            # GenrePack through narration_apply just for this), we
            # accept a simple over-resolution rule: location change
            # always resolves the encounter. The bug repro is a
            # negotiation, not a chase, and the user accepted this
            # rule explicitly in the pingpong note ("location-change
            # events should auto-resolve any active confrontations
            # whose participants are no longer co-located"). Chase
            # support can revisit this with a `category in {chase,
            # mobile}` skip if the gap surfaces in playtest.
            active_encounter = snapshot.encounter
            if active_encounter is not None and not active_encounter.resolved:
                abandoned_type = active_encounter.encounter_type
                active_encounter.resolved = True
                active_encounter.outcome = "abandoned_on_location_change"
                logger.info(
                    "encounter.deactivated_on_location_change "
                    "encounter_type=%s old_location=%r new_location=%r player=%s",
                    abandoned_type,
                    old_loc,
                    result.location,
                    player_name,
                )
                # OTEL lie-detector (CLAUDE.md OTEL principle): the GM
                # panel must see the deactivation fire so Sebastien can
                # verify the engine — not the narrator's prose — is the
                # reason the dial cleared. Without this span the
                # subsystem is silent and a regression where the
                # encounter stays active is invisible until the next
                # playtest.
                _watcher_publish(
                    "confrontation_deactivated_on_location_change",
                    {
                        "encounter_type": abandoned_type,
                        "old_location": old_loc,
                        "new_location": result.location,
                        "player_name": player_name,
                        "turn_number": snapshot.turn_manager.interaction,
                    },
                    component="confrontation",
                )

    if result.quest_updates:
        # Span emission replaces the prior direct ``_watcher_publish`` —
        # ``WatcherSpanProcessor`` re-emits the same ``state_transition``
        # event via ``SPAN_ROUTES[SPAN_QUEST_UPDATE]``.
        with quest_update_span(
            updates=result.quest_updates,
            player_name=player_name,
            turn_number=snapshot.turn_manager.interaction,
        ):
            for quest_id, status in result.quest_updates.items():
                snapshot.quest_log[quest_id] = status
            logger.info(
                "state.quest_update count=%d player=%s",
                len(result.quest_updates),
                player_name,
            )

    # Inventory — apply narrator items_gained/items_lost/items_discarded/
    # items_consumed on the rolling player's character. Playtest 2026-04-24
    # found a wiring gap: watcher emitted but inventory.items never
    # updated, leaving UI out of sync. Item shape mirrors
    # dispatch/chargen_loadout._item_dict_from_catalog. items_lost removes
    # the first matching name (case-insensitive) — narrator-granted items
    # currently arrive as quantity=1 singletons. items_discarded (Story
    # 45-14) flips the first matching item's state from "Carried" to
    # "Discarded" without removing it — narrator-recoverable abandon/drop
    # semantics. items_consumed (Story 45-15) also removes the first
    # matching item but is a distinct lane so the OTEL span can surface
    # "spent on use" vs. "given away" — Playtest 3 Felix found the
    # maintenance kit lingered at quantity=1 after patch-foam use because
    # the consume verb had no apply seam.
    items_discarded = getattr(result, "items_discarded", None) or []
    items_consumed = getattr(result, "items_consumed", None) or []
    if (
        result.items_gained or result.items_lost or items_discarded or items_consumed
    ) and snapshot.characters:
        # ADR-108: NO single ``snapshot.characters[0]`` recipient. In a
        # sealed MP round (ADR-036) there is no "acting player" and
        # inventory is per-player (ADR-037); each item entry resolves its
        # own seated-PC recipient via ``resolve_item_recipient``. The
        # narrating socket's PC (``acting_character_name`` falling back to
        # ``player_name`` — the same idiom as ``actor_for_location``
        # above) is the deterministic absent-recipient degradation.
        narrating_name = acting_character_name or player_name
        turn_num = snapshot.turn_manager.interaction

        def _narrator_item_dict(entry: dict[str, object]) -> dict[str, object]:
            name_val = str(entry.get("name", "") or "").strip() or "Unknown Item"
            desc_val = str(entry.get("description", "") or "").strip() or (
                "An item acquired during adventure."
            )
            category_raw = str(entry.get("category", "") or "").strip().lower()
            allowed = {
                "weapon",
                "armor",
                "tool",
                "consumable",
                "quest",
                "treasure",
                "misc",
            }
            category = category_raw if category_raw in allowed else "misc"
            slug = name_val.lower().replace(" ", "_").replace("-", "_")
            return {
                "id": f"narrator:{slug}",
                "name": name_val,
                "description": desc_val,
                "category": category,
                "value": 0,
                "weight": 0.0,
                "rarity": "common",
                "narrative_weight": 0.5,
                "tags": [],
                "equipped": False,
                "quantity": 1,
                "uses_remaining": None,
                "state": "Carried",
            }

        added_names: list[str] = []
        removed_names: list[str] = []
        discarded_names: list[str] = []
        unmatched_discards: list[str] = []
        consumed_names: list[str] = []
        unmatched_consumes: list[str] = []

        # Story 45-13: per-room container retrieved-state. Each
        # ``items_gained`` entry may carry an optional ``from_container``
        # annotation pointing at a narrator-emitted container id (e.g.
        # ``"tin_box"``). The room id is the acting PC's current location
        # (Wave 2B: per-character, no global snapshot.location). The
        # apply-time gate is the load-bearing block per AC #6: even when
        # the prompt-time hint is bypassed, a duplicate retrieval in the
        # same room is filtered here.
        # Fall back to player_name when callers haven't threaded
        # acting_character_name (parity with actor_for_location above).
        room_id = snapshot.party_location(perspective=acting_character_name or player_name) or ""
        round_number = snapshot.turn_manager.round
        for entry in result.items_gained or []:
            container_id = str(entry.get("from_container", "") or "").strip()
            if container_id and not room_id:
                # No silent fallback (CLAUDE.md): the narrator emitted a
                # container annotation but the snapshot has no canonical
                # room. The gate is unreachable; the item still lands so
                # play does not stall, but the GM panel must see the
                # configuration gap. A future story may want to harden
                # this into a hard refusal once narrator emission is
                # stable.
                logger.warning(
                    "state.container_gate_unreachable player=%s "
                    "container=%s reason=snapshot_location_empty round=%d",
                    player_name,
                    container_id,
                    round_number,
                )
            elif container_id and room_id:
                room_state = snapshot.room_states.get(room_id)
                prior = room_state.containers.get(container_id) if room_state is not None else None
                if prior is not None and prior.retrieved:
                    # Duplicate retrieval — apply-time gate fires. Item
                    # is NOT appended, prior_retrieved_at_round is
                    # preserved (read-only check, no clobber). The
                    # ContainerState model_validator guarantees that if
                    # ``prior.retrieved`` is True, ``retrieved_at_round``
                    # is a real int; the int(... or 0) cast below is
                    # therefore a defensive belt for None — the validator
                    # is the suspenders.
                    with container_retrieval_blocked_span(
                        room_id=room_id,
                        container_id=container_id,
                        prior_retrieved_at_round=int(
                            prior.retrieved_at_round or 0,
                        ),
                        current_round=round_number,
                        interaction=turn_num,
                        player_name=player_name,
                        genre=snapshot.genre_slug,
                        world=snapshot.world_slug,
                    ):
                        # warning, not info: narrator produced a known-bad
                        # duplicate that the gate had to suppress — this
                        # is a client-side error path per python.md #4.
                        logger.warning(
                            "state.container_retrieval_blocked player=%s "
                            "room=%s container=%s prior_round=%s "
                            "current_round=%s",
                            player_name,
                            room_id,
                            container_id,
                            prior.retrieved_at_round,
                            round_number,
                        )
                    continue  # skip the inventory append for this entry

                # First retrieval — record state and fire recorded span.
                if room_state is None:
                    room_state = RoomState(room_id=room_id)
                    snapshot.room_states[room_id] = room_state
                room_state.containers[container_id] = ContainerState(
                    container_id=container_id,
                    retrieved=True,
                    retrieved_at_round=round_number,
                )
                with container_retrieval_recorded_span(
                    room_id=room_id,
                    container_id=container_id,
                    round_number=round_number,
                    interaction=turn_num,
                    items_gained_count=1,
                    player_name=player_name,
                    genre=snapshot.genre_slug,
                    world=snapshot.world_slug,
                ):
                    logger.info(
                        "state.container_retrieval_recorded player=%s "
                        "room=%s container=%s round=%d",
                        player_name,
                        room_id,
                        container_id,
                        round_number,
                    )

            item_dict = _narrator_item_dict(entry)
            recipient_char = resolve_item_recipient(
                snapshot,
                entry,
                narrating_character_name=narrating_name,
                lane="gained",
            )
            recipient_char.core.inventory.items.append(item_dict)
            added_names.append(str(item_dict["name"]))

        for entry in result.items_lost or []:
            lost_name = str(entry.get("name", "") or "").strip().lower()
            if not lost_name:
                continue
            recipient_char = resolve_item_recipient(
                snapshot,
                entry,
                narrating_character_name=narrating_name,
                lane="lost",
            )
            for idx, existing in enumerate(recipient_char.core.inventory.items):
                existing_name = str(existing.get("name", "") or "").strip().lower()
                if existing_name == lost_name:
                    recipient_char.core.inventory.items.pop(idx)
                    removed_names.append(lost_name)
                    break

        # Story 45-14: items_discarded — transition first matching item's
        # state out of "Carried" instead of removing. Per CLAUDE.md
        # "no silent fallbacks": when the narrator declares a discard for
        # an item that isn't actually in inventory we log the miss and
        # surface it on the OTEL span so the GM panel sees the gap (the
        # narrator hallucinated, or extraction lost the prior pickup).
        for entry in items_discarded:
            discard_name = str(entry.get("name", "") or "").strip().lower()
            if not discard_name:
                continue
            recipient_char = resolve_item_recipient(
                snapshot,
                entry,
                narrating_character_name=narrating_name,
                lane="discarded",
            )
            matched = False
            for existing in recipient_char.core.inventory.items:
                existing_name = str(existing.get("name", "") or "").strip().lower()
                if existing_name == discard_name and (
                    str(existing.get("state", "Carried")) == "Carried"
                ):
                    existing["state"] = "Discarded"
                    existing["equipped"] = False
                    discarded_names.append(discard_name)
                    matched = True
                    break
            if not matched:
                unmatched_discards.append(discard_name)
                logger.warning(
                    "state.inventory_discard_miss player=%s turn=%d name=%r "
                    "reason=no_carried_match",
                    player_name,
                    turn_num,
                    discard_name,
                )

        # Story 45-15: items_consumed — used-up consumables drop from
        # inventory. AC1 demands no item remain at state=Consumed after
        # end-of-turn; the simplest fix is to never set Consumed in the
        # first place — the consume lane removes outright. Per CLAUDE.md
        # "no silent fallbacks": when the narrator declares a consume for
        # an item that isn't in inventory we surface ``unmatched_consumes``
        # on the OTEL span so the GM panel sees the gap (the narrator
        # hallucinated the use, or extraction lost the prior pickup).
        for entry in items_consumed:
            consume_name = str(entry.get("name", "") or "").strip().lower()
            if not consume_name:
                continue
            recipient_char = resolve_item_recipient(
                snapshot,
                entry,
                narrating_character_name=narrating_name,
                lane="consumed",
            )
            matched = False
            for idx, existing in enumerate(recipient_char.core.inventory.items):
                existing_name = str(existing.get("name", "") or "").strip().lower()
                if existing_name == consume_name:
                    recipient_char.core.inventory.items.pop(idx)
                    consumed_names.append(consume_name)
                    matched = True
                    break
            if not matched:
                unmatched_consumes.append(consume_name)
                logger.warning(
                    "state.inventory_consume_miss player=%s turn=%d "
                    "name=%r reason=no_inventory_match",
                    player_name,
                    turn_num,
                    consume_name,
                )

        # Span emission replaces the prior direct ``_watcher_publish`` —
        # ``WatcherSpanProcessor`` re-emits the same ``state_transition``
        # event via ``SPAN_ROUTES[SPAN_INVENTORY_NARRATOR_EXTRACTED]``.
        # ``added_names`` / ``removed_names`` / ``discarded_names`` /
        # ``consumed_names`` reflect the actual mutation outcome
        # (case-insensitive match, only successful transitions/removals
        # recorded), so the route-extracted payload
        # mirrors the post-mutation state.
        with inventory_narrator_extracted_span(
            gained=added_names,
            lost=removed_names,
            discarded=discarded_names,
            consumed=consumed_names,
            player_name=player_name,
            turn_number=turn_num,
            unmatched_discards_count=len(unmatched_discards),
            unmatched_consumes_count=len(unmatched_consumes),
        ):
            logger.info(
                "state.inventory_update player=%s turn=%d gained=%s lost=%s "
                "discarded=%s unmatched_discards=%s consumed=%s "
                "unmatched_consumes=%s",
                player_name,
                turn_num,
                added_names,
                removed_names,
                discarded_names,
                unmatched_discards,
                consumed_names,
                unmatched_consumes,
            )

    # Economy — apply narrator gold_change to the acting PC's purse.
    # Playtest 2026-05-07 wiring fix. The narrator already emits
    # ``gold_change`` on prose-described purchases / payments / windfalls
    # (e.g. "nineteen silver buys all three" → ``gold_change=-19``) and
    # the orchestrator surfaced the field on ``NarrationTurnResult``,
    # but no apply seam consumed it — so the patch reached
    # ``snapshot.companions`` and the inventory items, while the player's
    # purse stayed frozen at chargen. Sünden's economy is the play
    # loop's tension dial; a frozen purse mechanically detunes every
    # market interaction (Sebastien-axis players notice in one trade).
    #
    # Solo and MP behave the same as the items lane: mutate the first
    # character (``snapshot.characters[0]``) since that's the rolling
    # PC on the prose path. Clamp to >= 0 — a narrator that says "you
    # spend the last fifty silver" against a 30sp purse should not
    # underflow into negative debt without an explicit tracker.
    gold_change_field = getattr(result, "gold_change", None)
    if gold_change_field is not None and snapshot.characters:
        try:
            delta = int(gold_change_field)
        except (TypeError, ValueError):
            logger.warning(
                "economy.gold_change_invalid value=%r player=%s turn=%d",
                gold_change_field,
                player_name,
                snapshot.turn_manager.interaction,
            )
            delta = 0
        if delta != 0:
            character = snapshot.characters[0]
            before = int(character.core.inventory.gold)
            after = max(0, before + delta)
            applied_delta = after - before  # negative when clamped at zero
            character.core.inventory.gold = after
            turn_num = snapshot.turn_manager.interaction
            logger.info(
                "economy.gold_change player=%s actor=%s turn=%d "
                "requested_delta=%+d applied_delta=%+d before=%d after=%d "
                "clamped=%s",
                player_name,
                character.core.name,
                turn_num,
                delta,
                applied_delta,
                before,
                after,
                bool(applied_delta != delta),
            )
            _watcher_publish(
                "state_transition",
                {
                    "kind": "economy.gold_change",
                    "actor": character.core.name,
                    "requested_delta": delta,
                    "applied_delta": applied_delta,
                    "before": before,
                    "after": after,
                    "clamped": bool(applied_delta != delta),
                    "turn_number": turn_num,
                    "player_name": player_name,
                },
                component="economy",
            )

    if result.lore_established:
        added: list[str] = []
        for lore in result.lore_established:
            if lore not in snapshot.lore_established:
                snapshot.lore_established.append(lore)
                added.append(lore)
        # Span emission drives the ``lore_retrieval`` typed event with
        # ``component=lore`` via ``SPAN_ROUTES[SPAN_LORE_ESTABLISHED]``.
        # No prior ``_watcher_publish`` existed for this path — the GM
        # panel's Lore tab was previously dark for narrator-driven
        # additions.
        with lore_established_span(
            items=added,
            added_count=len(added),
            total=len(snapshot.lore_established),
            player_name=player_name,
            turn_number=snapshot.turn_manager.interaction,
        ):
            logger.info(
                "state.lore_established player=%s turn=%d added=%d total=%d",
                player_name,
                snapshot.turn_manager.interaction,
                len(added),
                len(snapshot.lore_established),
            )

    # NPC registry — auto-register + drift detection (Story 37-44).
    turn_num = snapshot.turn_manager.interaction
    # Playtest 2026-04-29: pre-compute the case-folded set of PC names so the
    # registry never admits a name that already belongs to a player character.
    # The MP joiner-orientation auto-narration was naming the host PC in the
    # narration block, and the auto-register loop was promoting that PC into
    # the NPC registry as ``role=ally`` (symptom of the symmetric 45-18 bug).
    # Once a PC is in the NPC registry, downstream beat-selection and party
    # state queries treat them as fungible with NPCs — the narrator and the
    # mechanical layer both stop knowing the player exists as a player.
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=list(result.npcs_present),
        turn_num=turn_num,
        acting_character_name=acting_character_name,
    )

    # Story 45-53: detect known recurring NPCs named in prose but missing
    # from npcs_present. Soft warning span (no exception) — the GM panel
    # surfaces the miss for human follow-up.
    _detect_missed_recurring_npcs(
        snapshot=snapshot,
        narration_text=result.narration or "",
        emitted_mentions=list(result.npcs_present),
        turn_num=turn_num,
    )

    # Story 49-6: ratification gate. Resolves observation_pending pool
    # members from the PRIOR turn against THIS turn's emitted_mentions —
    # promote on match (clear the flag, keep entry), purge on miss
    # (remove entry from npc_pool). Order is load-bearing: this MUST run
    # BEFORE _auto_mint_prose_only_npcs below, otherwise the gate would
    # evaluate this turn's own freshly-minted entries against this turn's
    # (omitting) mentions and self-purge them. Emits
    # SPAN_NPC_OBSERVATION_GATE_PROMOTED / SPAN_NPC_OBSERVATION_GATE_PURGED
    # so Sebastien's GM panel sees every gate decision.
    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=list(result.npcs_present),
        turn_num=turn_num,
    )

    # Story 49-2: auto-mint NPCs the narrator named in prose via role
    # (Father, mother, the doctor, ...) or honorific (Mrs. Gow, Dr.
    # Sallow, ...) but omitted from npcs_present. Runs AFTER the
    # recurring-presence detector so known names hit the 45-53 detector
    # first; this catches the FIRST-mention path. Side-effects only —
    # appends NpcPoolMember(drawn_from="dialogue_extraction",
    # observation_pending=True) and emits SPAN_NPC_AUTO_MINTED_FROM_PROSE
    # per mint. New mints face the 49-6 ratification gate on the NEXT
    # turn — not this one.
    _auto_mint_prose_only_npcs(
        snapshot=snapshot,
        narration_text=result.narration or "",
        emitted_mentions=list(result.npcs_present),
        turn_num=turn_num,
    )

    # Plot-a-course: parse course sidecar variants out of the
    # game_patch payload and apply them to the snapshot. Other
    # sidecar handlers (dice, encounter trigger) ignore course
    # intents — parse_course_sidecar returns None for those.
    # NOTE: reactions-hint injection on rejection (add_reaction_for_next_turn)
    # is not yet implemented — no reactions mechanism exists on Session.
    # Escalated: Bundle 6 or a dedicated reactions bundle should wire that path.
    _apply_course_sidecar(snapshot=snapshot, result=result, room=room)

    # B/X morale sidecar: narrator-emitted morale_event (Task 10, ADR-039).
    # Fires before the beat loop so an intimidated trigger can register
    # before any dial advance occurs this turn.
    _apply_morale_sidecar(snapshot=snapshot, result=result, pack=pack)

    # Encounter lifecycle (dual-track momentum, spec 2026-04-25)
    if pack is not None:
        # Spec 2026-05-20 — ActionRewrite.intent is the authoritative signal.
        # ADR-067's inference site, finally wired via confrontation_intent_validator.
        from sidequest.agents.confrontation_intent_validator import validate as _validate_intent
        from sidequest.game.beat_kinds import apply_beat
        from sidequest.server.dispatch.confrontation import find_confrontation_def
        from sidequest.server.dispatch.encounter_lifecycle import (
            NoOpponentAvailableError,
            SealedLetterArityError,
            instantiate_encounter_from_trigger,
        )
        from sidequest.telemetry.spans import (
            confrontation_unengaged_turn_span,
            encounter_beat_skipped_span,
            encounter_empty_actor_list_span,
            encounter_resolved_span,
        )

        _mismatch = _validate_intent(
            getattr(result, "action_rewrite", None),
            result.confrontation,
            pack,
            active_encounter=snapshot.encounter is not None
            and not snapshot.encounter.resolved,
        )

        _intent_text = (
            (getattr(getattr(result, "action_rewrite", None), "intent", "") or "").strip()
        )
        _classified_intent_value = _intent_text or "unspecified"

        # Story 59-1 — no-emission lie-detector. The intent-mismatch path below
        # only fires when the narrator emitted an ``action_rewrite.intent`` to
        # tokenize. The 2026-05-21 Glenross playtest hit the OTHER blind spot:
        # a textbook standoff in prose with NO confrontation field, NO active
        # encounter, AND NO intent — so ``_validate_intent`` returned None and
        # nothing engaged, silently. Emit a STRUCTURAL watcher (no engagement +
        # no intent) so the GM panel sees the miss. NOT prose keyword-scanning
        # (the deleted ``_CONFRONTATION_TRIGGER_PATTERNS`` regex stays dead).
        _no_active_encounter = snapshot.encounter is None or snapshot.encounter.resolved
        if not result.confrontation and _no_active_encounter and not _intent_text:
            with confrontation_unengaged_turn_span(
                player_name=player_name,
                genre_slug=snapshot.genre_slug or "",
            ):
                logger.warning(
                    "confrontation.unengaged_turn player=%s genre=%s — turn "
                    "engaged no confrontation and emitted no intent (validator "
                    "blind spot); GM panel should review for a winged standoff",
                    player_name,
                    snapshot.genre_slug or "",
                )

        if _mismatch is not None:
            _effective_severity = _mismatch.severity
            _outcome_label: str | None = None
            if already_reprompted and _effective_severity == "reprompt":
                _effective_severity = "warn"  # bounded retry, fall through
                _outcome_label = "fall_through"

            _classified_intent_value = _mismatch.matched_type

            from sidequest.telemetry.spans import confrontation_intent_mismatch_span

            with confrontation_intent_mismatch_span(
                matched_type=_mismatch.matched_type,
                declared_type=_mismatch.declared,
                severity=_effective_severity,
                matched_tokens=_mismatch.matched_tokens,
                reprompt_attempted=already_reprompted,
                outcome=_outcome_label,
            ):
                pass

            if _effective_severity == "soft_suggest":
                snapshot.next_turn_directives.append(
                    f"Last turn's intent suggested {_mismatch.matched_type}. "
                    f"If this scene is in fact a {_mismatch.matched_type}, open the "
                    f"encounter on this turn."
                )
            elif _effective_severity == "reprompt":
                # Spec 2026-05-20 — build the directive, attach to outcome,
                # return early so the orchestrator can re-invoke the narrator
                # with extra_directive=<directive> and re-apply with
                # already_reprompted=True.
                #
                # CAVEAT: by this point the apply has already mutated
                # location, lore, NPCs, inventory, magic_working, etc.
                # The early return only skips the ENCOUNTER/BEAT
                # application below. The second-attempt apply will re-run
                # those pre-validator mutations against the second
                # narration's fields, which may double-add NPCs or
                # overwrite location. The double-apply risk is rare in
                # practice (requires the second narration to mention the
                # same NPCs/items) and per-spec; revisit if playtest
                # surfaces concrete regressions.
                outcome.reprompt_request = RepromptRequest(
                    matched_type=_mismatch.matched_type,
                    declared=_mismatch.declared,
                    directive=(
                        f"Previous attempt described a {_mismatch.matched_type} "
                        f"(intent: '{_intent_text}') but did not open one. "
                        f"Either set confrontation={_mismatch.matched_type} "
                        f"or rewrite without {_mismatch.matched_type}-shaped language."
                    ),
                )
                outcome.classified_intent = _classified_intent_value
                return outcome

        outcome.classified_intent = _classified_intent_value

        # (a) Narrator-initiated encounter
        if result.confrontation and (snapshot.encounter is None or snapshot.encounter.resolved):
            if not result.npcs_present:
                with encounter_empty_actor_list_span(
                    encounter_type=result.confrontation,
                    genre_slug=snapshot.genre_slug or "",
                    player_name=player_name,
                ):
                    logger.warning(
                        "encounter.empty_actor_list confrontation=%s player=%s",
                        result.confrontation,
                        player_name,
                    )
            # Playtest 2026-05-03 [BUG] — confrontation widget showed only
            # the action-submitter PC even though both PCs played the bundled
            # MP turn. Bundled-action narration produces ONE narrator call
            # but every seated PC is in the round by construction, so seat
            # the other seated PCs as side="player" actors alongside the
            # submitter. ``player_seats`` maps player_id → character.core.name;
            # values() is the canonical PC roster for the session. Solo
            # sessions and pre-MP saves pass an empty list (single-PC actor
            # array, identical to prior behavior).
            additional_pc_names = [
                name for name in snapshot.player_seats.values() if name and name != player_name
            ]
            # Story 45-33 (CLAUDE.md "strict helper, lenient caller"): the
            # lifecycle raises ValueError with an OTEL span when a
            # category=combat encounter resolves to zero opponents
            # post-fallback (no narrator npcs AND no registry NPCs at the
            # player's location). The OTEL span is the lie-detector
            # signal — the wrapper catches the exception so the narration
            # turn stays resilient (no crashed turn for an LLM extraction
            # gap). The encounter is not created and the
            # ``encounter_empty_actor_list_span`` above already logged the
            # gap; the GM panel sees both spans and can correlate.
            try:
                instantiate_encounter_from_trigger(
                    snapshot=snapshot,
                    pack=pack,
                    encounter_type=result.confrontation,
                    player_name=player_name,
                    npcs_present=result.npcs_present,
                    genre_slug=snapshot.genre_slug,
                    additional_player_names=additional_pc_names,
                )
            except NoOpponentAvailableError as exc:
                # Narrowly scoped: only the Story 45-33 no-opponent guard is
                # caught here. The unknown-encounter-type / bad-side
                # ValueErrors all PROPAGATE — those are config/extraction errors
                # that the existing test suite asserts crash the turn.
                logger.warning(
                    "encounter.no_opponent_available confrontation=%s player=%s reason=%s",
                    result.confrontation,
                    player_name,
                    exc,
                )
            except SealedLetterArityError as exc:
                # Playtest 2026-05-08: narrator triggered a sealed-letter
                # encounter (1v1 contract) against zero or multiple NPCs.
                # The OTEL span at the validator already recorded the
                # rejection; here we keep the turn alive — the narrator's
                # prose stands, no structured encounter instantiates.
                logger.warning(
                    "encounter.sealed_letter_arity_rejected confrontation=%s player=%s reason=%s",
                    result.confrontation,
                    player_name,
                    exc,
                )

        # (b) Apply beat selections (dice-replay turns short-circuit)
        enc = snapshot.encounter
        # SOUL "The Test" gate — drop PC-side beats inferred from prose.
        # Production callers leave from_explicit_action=False so every
        # narrator-driven turn passes through this filter; explicit
        # DICE_THROW commits arrive via dispatch_dice_throw, which never
        # reaches this branch. See _filter_inferred_pc_beats docstring.
        #
        # Sealed-letter encounters (dogfight) bypass the gate: that
        # confrontation type's UI is itself a private secret-commit form,
        # so the narrator-extracted commits ARE the explicit-consent
        # frame for both pilots. The gate is scoped to legacy apply_beat
        # PC selections — the path that the playtest [S2-BUG] exposed.
        gated_selections = result.beat_selections
        gate_active = (
            enc is not None
            and not from_explicit_action
            and result.beat_selections
            and _gate_applies_to_encounter(enc, pack)
        )
        if gate_active:
            # Seat-aware SOUL gate: companion-NPCs on the player side are
            # narrator-driven (no consent contract), so the gate must
            # only reject seats that map to live players. Without this
            # filter every recruited hireling's beat would be silently
            # dropped (playtest 2026-05-06 Donut defend regression).
            seated_pc_names = set(snapshot.player_seats.values()) if snapshot.player_seats else None
            gated_selections = _filter_inferred_pc_beats(
                result.beat_selections,
                enc,
                narrating_player=player_name,
                seated_pc_names=seated_pc_names,
            )

        if enc is not None and not enc.resolved and gated_selections:
            cdef = find_confrontation_def(
                pack.rules.confrontations if pack.rules else [],
                enc.encounter_type,
            )
            if cdef is None:
                raise ValueError(f"active encounter type {enc.encounter_type!r} not in pack")

            # ---- Sealed-letter lookup branch (T5, dogfight port) ----
            # When the confrontation declares ResolutionMode.sealed_letter_lookup
            # we resolve via cross-product cell lookup instead of the legacy
            # apply_beat path. Maneuver IDs and beat IDs share a namespace by
            # content convention (the dogfight beats ARE the maneuvers — see
            # tests/genre/test_dogfight_content_loading.py::
            # test_dogfight_beats_cover_every_consumed_maneuver), so we
            # repurpose ``beat_selections[].beat_id`` as the maneuver commit
            # for that actor. The resolver raises ValueError when commits are
            # missing a role or when a maneuver isn't in maneuvers_consumed.
            #
            # Sealed-letter resolution is EXCLUSIVE of the legacy beat loop —
            # because maneuver IDs collide with beat IDs by content design,
            # falling through to apply_beat would double-apply mechanics.
            if cdef.resolution_mode == ResolutionMode.sealed_letter_lookup:
                if cdef.interaction_table is None:
                    raise ValueError(
                        f"confrontation {enc.encounter_type!r} declares "
                        f"resolution_mode=sealed_letter_lookup but has no "
                        f"interaction_table — cannot dispatch sealed-letter "
                        f"resolution"
                    )

                commits: dict[str, str] = {}
                for sel in gated_selections:
                    actor = enc.find_actor(sel.actor)
                    if actor is None:
                        raise ValueError(
                            f"beat_selection actor {sel.actor!r} not found "
                            f"on sealed-letter encounter "
                            f"{enc.encounter_type!r}"
                        )
                    commits[actor.role] = sel.beat_id

                sl_outcome = resolve_sealed_letter_lookup(
                    enc,
                    commits,
                    cdef.interaction_table,
                )
                outcome.sealed_letter = sl_outcome
                # Replace, do not append: only the most recent cell's hint
                # is relevant context for the next narrator turn.
                # ``narrator_hints`` is consumed by
                # ``sidequest.agents.encounter_render`` which "; "-joins
                # the list into the prompt — appending across turns would
                # bloat the prompt with stale hints (turn 1's "merge"
                # hint is misleading once turn 5 is a knife fight).
                if sl_outcome.narration_hint:
                    enc.narrator_hints = [sl_outcome.narration_hint]
                else:
                    enc.narrator_hints = []
                # Status-change processing further down still runs because
                # we only short-circuit the beat-selection block, not the
                # whole snapshot mutation phase.
                # Fall-through: skip beat loop by NOT defining beat_by_id
                # and gating the loop below.
                _legacy_beat_path = False
            elif cdef.resolution_mode == ResolutionMode.opposed_check:
                # ---- Opposed-check resolution branch (combat fairness, 2026-04-26) ----
                # Both sides roll d20 + modifier; tier comes from the shift.
                # The player's roll arrived via DICE_THROW and is stashed on
                # session_data; ``dispatch_dice_throw`` deferred apply_beat
                # for the player so this branch can derive the tier from the
                # opposing roll instead of the legacy roll-vs-DC tier.
                #
                # Spec: ``.archive/handoffs/opposed-checks-design.md``.
                #
                # Awaiting-dice short-circuit (playtest 2026-04-30 4-player
                # MP): production reaches a state where the encounter is
                # opposed_check but the player submitted text instead of
                # rolling, so ``pending_player_d20`` is None.
                # ``_filter_inferred_pc_beats`` above already dropped any
                # PC beats the narrator inferred (SOUL "The Test" gate),
                # but opponent-side beats remain — and the resolver below
                # raises ValueError on absent stash (its loud-fail contract
                # is correct for the dice path, see
                # test_narration_apply_opposed_check_hard_fails_without_
                # pending_state). For the narrator-prose path
                # (``from_explicit_action=False``), redirect to "wait for
                # dice": drop the opponent selections so the resolver
                # doesn't fire, let prose apply, encounter persists, next
                # DICE_THROW completes the round. The dice path
                # (``from_explicit_action=True``) preserves the raise — if
                # ``dispatch_dice_throw`` reached us without stashing, that
                # IS a programming error and should fail loud.
                if opposed_player_d20 is None and not from_explicit_action:
                    for sel in gated_selections:
                        _watcher_publish(
                            "state_transition",
                            {
                                "field": "encounter",
                                "op": "opposed_check_awaiting_dice_drop",
                                "actor": sel.actor,
                                "beat_id": sel.beat_id,
                                "encounter_type": enc.encounter_type,
                            },
                            component="confrontation",
                            severity="info",
                        )
                    logger.info(
                        "encounter.opposed_check_awaiting_dice "
                        "encounter=%r dropped %d beat selection(s) — "
                        "narrator prose applied, encounter persists, "
                        "awaiting DICE_THROW",
                        enc.encounter_type,
                        len(gated_selections),
                    )
                else:
                    outcome_obj = _resolve_opposed_check_branch(
                        encounter=enc,
                        cdef=cdef,
                        selections=gated_selections,
                        pack_beats={b.id: b for b in cdef.beats},
                        pending_player_d20=opposed_player_d20,
                        pending_player_beat_id=opposed_player_beat_id,
                        pending_player_actor=opposed_player_actor,
                        turn=snapshot.turn_manager.interaction,
                        snapshot=snapshot,
                    )
                    if outcome_obj.encounter_resolved:
                        snapshot.pending_resolution_signal = _build_resolution_signal(enc)
                _legacy_beat_path = False
            else:
                _legacy_beat_path = True
                beat_by_id = {b.id: b for b in cdef.beats}
        else:
            _legacy_beat_path = False

        if _legacy_beat_path:
            selections = gated_selections
            if dice_failed is not None and selections:
                # Dice-replay turns: dispatch/dice.py already applied the
                # rolling actor's beat. Drop just THAT actor's selection
                # (would otherwise double-apply); keep every other actor's
                # selection so opponent-side beats actually advance the
                # opponent dial. Playtest 2026-04-25 [P0]: dropping all
                # selections wholesale left opponent_metric stuck at 0
                # forever and combat was structurally one-sided.
                kept: list = []
                for sel in selections:
                    actor = enc.find_actor(sel.actor)
                    side = actor.side if actor else "unknown"
                    is_rolling_actor = dice_actor is not None and sel.actor == dice_actor
                    # Fallback when dice_actor wasn't threaded through (older
                    # call sites): drop player-side selections to preserve
                    # the prior no-double-apply guarantee, but no longer
                    # blanket-drop opponent-side selections.
                    if dice_actor is None and side == "player":
                        is_rolling_actor = True
                    if is_rolling_actor:
                        with encounter_beat_skipped_span(
                            reason="dice_replay_turn",
                            actor=sel.actor,
                            actor_side=side,
                            beat_id=sel.beat_id,
                        ):
                            pass
                        _watcher_publish(
                            "state_transition",
                            {
                                "field": "encounter",
                                "op": "beat_skipped",
                                "reason": "dice_replay_turn",
                                "actor": sel.actor,
                                "actor_side": side,
                                "beat_id": sel.beat_id,
                            },
                            component="encounter",
                        )
                        continue
                    kept.append(sel)
                selections = kept

            turn_num = snapshot.turn_manager.interaction
            for sel in selections:
                actor = enc.find_actor(sel.actor)
                if actor is None:
                    raise ValueError(f"unknown actor {sel.actor!r} in beat selection")
                beat = beat_by_id.get(sel.beat_id)
                if beat is None:
                    raise ValueError(
                        f"unknown beat_id {sel.beat_id!r} for encounter {enc.encounter_type!r}"
                    )
                # Renamed from `outcome` to `tier` to avoid shadowing the
                # function-scoped `outcome = NarrationApplyOutcome()`. The
                # legacy beat path was silently returning RollOutcome from
                # the last selection instead of the apply-outcome dataclass.
                tier = sel.outcome  # narrator-declared tier
                result_apply = apply_beat(
                    enc,
                    actor,
                    beat,
                    tier,
                    turn=turn_num,
                    edge_resolver=snapshot.find_creature_core,
                )
                if result_apply.skipped_reason is not None:
                    with encounter_beat_skipped_span(
                        reason=result_apply.skipped_reason,
                        actor=actor.name,
                        actor_side=actor.side,
                        beat_id=sel.beat_id,
                    ):
                        pass
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "encounter",
                            "op": "beat_skipped",
                            "reason": result_apply.skipped_reason,
                            "actor": actor.name,
                            "actor_side": actor.side,
                            "beat_id": sel.beat_id,
                        },
                        component="encounter",
                    )
                    continue
                # Beat was applied successfully — emit ENCOUNTER_BEAT_APPLIED
                own_delta = result_apply.deltas.own if result_apply.deltas else 0
                opp_delta = result_apply.deltas.opponent if result_apply.deltas else 0
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "encounter",
                        "op": "beat_applied",
                        "actor": actor.name,
                        "actor_side": actor.side,
                        "beat_id": sel.beat_id,
                        "beat_kind": str(beat.kind.value)
                        if hasattr(beat.kind, "value")
                        else str(beat.kind),
                        "outcome_tier": sel.outcome.value
                        if hasattr(sel.outcome, "value")
                        else str(sel.outcome),
                        "own_delta": own_delta,
                        "opponent_delta": opp_delta,
                        "metric_target": enc.encounter_type,
                        "turn": turn_num,
                    },
                    component="encounter",
                )
                # Story 45-9: bump total_beats_fired counter + OTEL.
                # Every non-skipped apply_beat is one real beat fire; the
                # campaign-maturity ladder in world_materialization reads
                # this counter, so the increment must be unconditional
                # here (CLAUDE.md no silent fallbacks).
                snapshot.record_beat_fired(
                    beat_id=sel.beat_id,
                    encounter_type=enc.encounter_type,
                    turn=turn_num,
                    source="narrator_beat",
                )

                # ─── B/X resource_deltas consumption hook (§5.6 #4 fix) ────
                # Apply per-beat resource_deltas to the magic_state ledger.
                # This is the V1 wire-up for cast_spell consumption (B/X
                # memorization): a Mage with spell_slots=1.0 can cast once;
                # after this hook runs the bar is 0.0 and beats_available_for
                # will filter cast_spell out of the menu on the next turn.
                # Clamp at 0.0 — slot ledger cannot go negative.
                if beat.resource_deltas:
                    magic_state = snapshot.magic_state
                    if magic_state is not None:
                        from sidequest.magic.state import BarKey

                        for resource_name, delta in beat.resource_deltas.items():
                            bar_key = BarKey(
                                scope="character",
                                owner_id=actor.name,
                                bar_id=resource_name,
                            )
                            try:
                                bar = magic_state.get_bar(bar_key)
                            except KeyError:
                                # Character has no ledger entry for this
                                # resource — skip silently (non-magic actor
                                # or bar not declared for this world).
                                continue
                            new_value = max(0.0, bar.value + delta)
                            magic_state.set_bar_value(bar_key, new_value)
                            _watcher_publish(
                                "state_transition",
                                {
                                    "field": "magic_state",
                                    "op": "resource_delta",
                                    "resource": resource_name,
                                    "delta": delta,
                                    "owner": actor.name,
                                    "new_value": new_value,
                                    "beat_id": beat.id,
                                },
                                component="magic",
                            )

                # ─── Story 47-10: innate_v1 cast resolution ───────────────
                # When the cast_spell beat fires AND the narrator named a
                # specific spell in the sidecar AND the world has loaded
                # spell catalogs AND the actor has the spell prepared,
                # invoke resolve_innate_v1_cast to drive the save branch
                # and emit the innate_v1.cast OTEL span. Each guard logs a
                # watcher event on miss so the GM panel can surface
                # "infrastructure present but cast didn't fire" — per
                # CLAUDE.md OTEL principle (lie detector for wiring gaps).
                if beat.id == "cast_spell":
                    _resolve_innate_cast_for_beat(
                        sel=sel,
                        actor=actor,
                        snapshot=snapshot,
                    )

                # ─── B/X morale per-beat hook (Task 9, architect feedback
                # 2026-05-08) ───────────────────────────────────────────
                # Fire morale-trigger detection on every beat that
                # advanced ``player_metric`` (the dial that tracks
                # "opponents being defeated"). Without this hook morale
                # only fires at encounter resolution (player_victory),
                # which is too late — the spec exit criterion §5.6.2
                # requires combats to END in flight or surrender, which
                # means morale must fire BEFORE the dial saturates.
                #
                # Dial-as-pseudo-HP approximation: see
                # ``_emit_morale_triggers`` docstring for the full
                # deviation note. Briefly: dial value = pseudo-HP taken,
                # threshold = pseudo-initial-side-size. ``leader_killed``
                # is False here (no per-actor KO at the dial seam);
                # ``mindless`` defaults False per ``_all_opponents_mindless``.
                #
                # Selection of dial: ``player_metric`` advances when the
                # PLAYER side scores success — that maps to "opponents
                # taking damage" in this codebase's dual-track engine
                # (player_metric.current → threshold = players win =
                # opponents defeated). Note: the architect feedback's
                # spec referenced ``opponent_metric``, but this codebase
                # uses ``player_metric`` for player-progress-toward-win;
                # see the dual-track design (spec 2026-04-25). Using
                # ``player_metric`` produces the right semantic
                # ("first hit lands → first_blood fires").
                if result_apply.deltas is not None:
                    # The actor's beat may have advanced player_metric via
                    # ``own`` (player-side actor) or via ``opponent`` (cross
                    # delta — e.g. opponent-side ``brace`` draining player
                    # dial; that is a NEGATIVE delta so it does not advance).
                    # We only care about positive advances of player_metric.
                    if actor.side == "player":
                        morale_dial_delta = max(result_apply.deltas.own, 0)
                    else:
                        # Opponent-side actor's beat: ``opponent`` delta is
                        # cross-side. For an opponent strike on player_metric
                        # this would be negative (drain) by spec, so only
                        # positive values count. In practice opponent strikes
                        # advance ``opponent_metric`` (their own dial), not
                        # ``player_metric``, so morale_dial_delta is 0 here.
                        morale_dial_delta = max(result_apply.deltas.opponent, 0)
                    if morale_dial_delta > 0:
                        pm = enc.player_metric
                        threshold = max(pm.threshold, 1)
                        post_value = pm.current
                        pre_value = max(0, post_value - morale_dial_delta)
                        pseudo_initial = threshold
                        pseudo_pre_alive = max(0, threshold - pre_value)
                        pseudo_post_alive = max(0, threshold - post_value)
                        if pseudo_pre_alive != pseudo_post_alive:
                            opp_actors_for_morale = [a for a in enc.actors if a.side == "opponent"]
                            all_mindless = _all_opponents_mindless(opp_actors_for_morale, pack)
                            pre_states = [
                                OpponentState(
                                    id=str(i),
                                    alive=(i < pseudo_pre_alive),
                                    mindless=all_mindless,
                                )
                                for i in range(pseudo_initial)
                            ]
                            post_states = [
                                OpponentState(
                                    id=str(i),
                                    alive=(i < pseudo_post_alive),
                                    mindless=all_mindless,
                                )
                                for i in range(pseudo_initial)
                            ]
                            # leader_killed: dial-based wire cannot detect
                            # which actor was KO'd; V1 keeps False here.
                            # Task 10's intimidated sidecar covers explicit
                            # narrator-emitted leader-takedown signals.
                            morale_fired = _emit_morale_triggers(
                                enc,
                                cdef,
                                enc.encounter_type,
                                pre_states,
                                post_states,
                                False,
                                Random(),
                            )
                            _apply_flee_consequences(enc, cdef, morale_fired)

                if result_apply.resolved:
                    with encounter_resolved_span(
                        encounter_type=enc.encounter_type,
                        outcome=enc.outcome or "",
                        source="narrator_beat",
                    ):
                        pass
                    snapshot.pending_resolution_signal = _build_resolution_signal(enc)
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "encounter",
                            "op": "resolved",
                            "encounter_type": enc.encounter_type,
                            "outcome": enc.outcome or "",
                            "source": "narrator_beat",
                            "final_player_metric": enc.player_metric.current,
                            "final_opponent_metric": enc.opponent_metric.current,
                        },
                        component="encounter",
                    )
                    # Phase 5 (Story 47-3): when the resolved encounter
                    # is a magic confrontation, fire its mandatory_outputs
                    # and stash the CONFRONTATION_OUTCOME payload on the
                    # snapshot for the room's outbound dispatcher to
                    # forward to the UI overlay reveal panel. Non-magic
                    # encounters return None — pass-through.
                    _resolve_magic_confrontation_if_applicable(
                        snapshot=snapshot,
                        encounter_type=enc.encounter_type,
                        outcome=enc.outcome or "",
                        actor=actor.name,
                    )
                    # B/X morale: per-beat dial-advance hook (above)
                    # already fired any morale triggers caused by the
                    # beat that resolved this encounter. No additional
                    # call here.
                    # Scratch sweep at encounter resolution. Encounter end
                    # is the canonical "scene end" trigger that the Scratch
                    # severity tier promises in game/status.py — without
                    # this sweep, Scratches accumulate forever (Bug #1).
                    # Now also advances the story-time clock via Session.
                    room.session.end_scene("scene_end", turn=turn_num)
                    break

    if result.status_changes:
        from sidequest.game.status import StatusSeverity
        from sidequest.server.status_clear import apply_explicit_status_clears

        turn_num = snapshot.turn_manager.interaction
        encounter_type = snapshot.encounter.encounter_type if snapshot.encounter else None
        # Explicit clears first — process every {"actor": ..., "clear": "<text>"}
        # entry so a single turn can clear an old status and add a new one
        # without the new ADD getting steamrolled. The clear path is the
        # narrator's tool for ending Wound/Scar conditions narratively
        # ("she wriggles free", "the medic binds the gash").
        apply_explicit_status_clears(
            snapshot,
            status_changes=result.status_changes,
            turn=turn_num,
        )
        for entry in result.status_changes:
            # An entry is EITHER a clear OR an add — never both. Clears
            # were handled above; skip them here.
            if entry.get("clear"):
                continue
            actor_name = str(entry.get("actor", "")).strip()
            status_payload = entry.get("status") or {}
            text = str(status_payload.get("text", "")).strip()
            severity_raw = str(status_payload.get("severity", "Scratch"))
            try:
                severity = StatusSeverity(severity_raw)
            except ValueError:
                logger.warning(
                    "status_change.invalid_severity actor=%s severity=%s",
                    actor_name,
                    severity_raw,
                )
                continue
            if not actor_name or not text:
                continue
            target = resolve_status_target(
                snapshot,
                actor_name=actor_name,
                turn_num=turn_num,
                trigger="status_change",
            )
            if target is None:
                logger.warning(
                    "status_change.unknown_actor actor=%s text=%s",
                    actor_name,
                    text,
                )
                continue
            _append_status_to_actor(
                target=target,
                actor=actor_name,
                text=text,
                severity=severity,
                source="narrator_extraction",
                turn_num=turn_num,
                encounter_type=encounter_type,
            )

    _apply_companion_changes(
        snapshot=snapshot,
        added=getattr(result, "companions_added", []) or [],
        dismissed=getattr(result, "companions_dismissed", []) or [],
        acting_character_name=acting_character_name or player_name,
        player_name=player_name,
    )

    return outcome


def _apply_companion_changes(
    *,
    snapshot: GameSnapshot,
    added: list,
    dismissed: list,
    acting_character_name: str | None,
    player_name: str,
) -> None:
    """Apply narrator-declared companion roster mutations.

    Playtest 2026-05-06 wiring fix. The narrator describes hiring NPCs in
    prose ("Donut takes the contract") and now ALSO emits
    ``companions_added`` / ``companions_dismissed`` in its game_patch
    sidecar. This seam mutates ``snapshot.companions`` and emits one
    ``party.recruit`` / ``party.dismiss`` watcher span per change so
    Sebastien's GM panel sees the mechanical event paired with the prose.

    Add semantics: append a fresh ``Companion`` for each unique name not
    already on the roster. Re-hiring an already-on-roster name is a
    silent no-op (the narrator may re-mention the contract without
    intending a duplicate). Dismissal removes the first matching name
    (case-insensitive); unmatched names log + emit a ``party.dismiss``
    span with ``status=unmatched`` so the GM panel can see the prose
    referenced a companion that wasn't on the roster.
    """
    if not added and not dismissed:
        return

    from sidequest.game.session import Companion

    turn_num = snapshot.turn_manager.interaction
    existing_names_lower = {c.name.casefold() for c in snapshot.companions}
    recruited_count = 0
    duplicate_count = 0

    for entry in added:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            logger.warning(
                "party.recruit_skipped reason=blank_name entry=%r turn=%d",
                entry,
                turn_num,
            )
            continue
        if name.casefold() in existing_names_lower:
            duplicate_count += 1
            _watcher_publish(
                "state_transition",
                {
                    "kind": "party.recruit_duplicate",
                    "name": name,
                    "turn_number": turn_num,
                    "player_name": player_name,
                },
                component="party",
            )
            continue
        companion = Companion(
            name=name,
            role=str(entry.get("role", "")).strip(),
            description=str(entry.get("description", "")).strip(),
            notes=str(entry.get("notes", "")).strip(),
            recruited_turn=turn_num,
            recruited_by=str(entry.get("recruited_by", "") or acting_character_name or "").strip(),
        )
        snapshot.companions.append(companion)
        existing_names_lower.add(companion.name.casefold())
        recruited_count += 1
        logger.info(
            "party.recruit name=%r role=%r recruited_by=%r turn=%d",
            companion.name,
            companion.role,
            companion.recruited_by,
            turn_num,
        )
        _watcher_publish(
            "state_transition",
            {
                "kind": "party.recruit",
                "name": companion.name,
                "role": companion.role,
                "description": companion.description,
                "notes": companion.notes,
                "recruited_by": companion.recruited_by,
                "turn_number": turn_num,
                "roster_size_after": len(snapshot.companions),
                "player_name": player_name,
            },
            component="party",
        )

    dismissed_count = 0
    unmatched_count = 0
    for raw_name in dismissed:
        name = str(raw_name).strip()
        if not name:
            continue
        match_idx: int | None = None
        for i, c in enumerate(snapshot.companions):
            if c.name.casefold() == name.casefold():
                match_idx = i
                break
        if match_idx is None:
            unmatched_count += 1
            logger.warning(
                "party.dismiss_unmatched name=%r turn=%d roster=%s",
                name,
                turn_num,
                [c.name for c in snapshot.companions],
            )
            _watcher_publish(
                "state_transition",
                {
                    "kind": "party.dismiss",
                    "name": name,
                    "status": "unmatched",
                    "turn_number": turn_num,
                    "player_name": player_name,
                },
                component="party",
            )
            continue
        removed = snapshot.companions.pop(match_idx)
        existing_names_lower.discard(removed.name.casefold())
        dismissed_count += 1
        logger.info(
            "party.dismiss name=%r role=%r served_turns=%d turn=%d",
            removed.name,
            removed.role,
            turn_num - removed.recruited_turn,
            turn_num,
        )
        _watcher_publish(
            "state_transition",
            {
                "kind": "party.dismiss",
                "name": removed.name,
                "role": removed.role,
                "served_turns": turn_num - removed.recruited_turn,
                "status": "ok",
                "turn_number": turn_num,
                "roster_size_after": len(snapshot.companions),
                "player_name": player_name,
            },
            component="party",
        )

    if recruited_count or duplicate_count or dismissed_count or unmatched_count:
        logger.info(
            "party.companion_mutations recruited=%d duplicates=%d "
            "dismissed=%d unmatched_dismiss=%d roster_size=%d turn=%d",
            recruited_count,
            duplicate_count,
            dismissed_count,
            unmatched_count,
            len(snapshot.companions),
            turn_num,
        )


def _build_resolution_signal(enc: object) -> object:
    from sidequest.game.resolution_signal import ResolutionSignal

    return ResolutionSignal(
        encounter_type=enc.encounter_type,
        outcome=enc.outcome or "",
        final_player_metric=enc.player_metric.current,
        final_opponent_metric=enc.opponent_metric.current,
        yielded_actors=tuple(),
        edge_refreshed=0,
    )


# Phase 5 (Story 47-3): magic-confrontation outcome → branch mapping.
# Conservative for ambiguous strings ('win', 'loss'): these flatten to
# clear_win / clear_loss because the encounter system does not yet
# surface a separate pyrrhic axis. Explicit 'pyrrhic_win' / 'pyrrhic'
# strings are preserved when narrators or dispatch logic emits them,
# so the four-branch enum is reachable end-to-end. Architect addendum
# §6 is the slot for adding a secondary-metric pyrrhic detector later;
# until then the magic-confrontation narration carries the pyrrhic
# distinction in prose rather than dispatch metadata.
_OUTCOME_TO_BRANCH = {
    "win": "clear_win",
    "clear_win": "clear_win",
    "pyrrhic_win": "pyrrhic_win",
    "pyrrhic": "pyrrhic_win",
    "loss": "clear_loss",
    "clear_loss": "clear_loss",
    "refused": "refused",
    "yield": "refused",
    "yielded": "refused",
}


def _drain_pending_status_promotions(*, snapshot: GameSnapshot) -> None:
    """Move queued status promotions into ``Character.core.statuses``.

    ``apply_mandatory_outputs`` queues entries onto
    ``MagicState.pending_status_promotions`` rather than appending to
    Character.core.statuses directly so the dispatcher stays decoupled
    from the character roster (the magic state and the snapshot are
    populated through different paths). This drainer runs at the
    encounter-resolution seam alongside the CONFRONTATION_OUTCOME
    dispatch, finds each promotion's actor on the snapshot, and
    appends a Status with the queued severity + text.

    Promotions whose actor is missing from the roster (NPC magic
    confrontations, save-state races) are left in the queue and
    surfaced through a single warning watcher event so the GM panel
    sees the orphan rather than silently absorbing it.
    """
    if snapshot.magic_state is None:
        return
    state = snapshot.magic_state
    if not state.pending_status_promotions:
        return

    from sidequest.game.status import Status, StatusSeverity

    turn_num = snapshot.turn_manager.interaction if hasattr(snapshot, "turn_manager") else 0
    encounter_type = snapshot.encounter.encounter_type if snapshot.encounter else None

    remaining: list[dict] = []
    for promotion in state.pending_status_promotions:
        actor_name = promotion.get("actor", "")
        text = promotion.get("text", "")
        severity_str = promotion.get("severity", "")
        target = next(
            (c for c in snapshot.characters if c.core.name == actor_name),
            None,
        )
        if target is None:
            remaining.append(promotion)
            _watcher_publish(
                "state_transition",
                {
                    "field": "magic_state",
                    "op": "status_promotion_orphaned",
                    "actor": actor_name,
                    "severity": severity_str,
                    "reason": "actor not in snapshot.characters",
                },
                component="magic",
                severity="warning",
            )
            continue
        try:
            severity = StatusSeverity(severity_str)
        except ValueError:
            remaining.append(promotion)
            _watcher_publish(
                "state_transition",
                {
                    "field": "magic_state",
                    "op": "status_promotion_invalid_severity",
                    "actor": actor_name,
                    "severity": severity_str,
                },
                component="magic",
                severity="warning",
            )
            continue
        target.core.statuses.append(
            Status(
                text=text,
                severity=severity,
                absorbed_shifts=0,
                created_turn=turn_num,
                created_in_encounter=encounter_type,
            )
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "status_added",
                "actor": actor_name,
                "text": text,
                "severity": severity.value,
                "source": "magic_confrontation_outcome",
                "turn": turn_num,
                "encounter_type": encounter_type,
            },
            component="encounter",
        )
    state.pending_status_promotions = remaining


def _resolve_magic_confrontation_if_applicable(
    *,
    snapshot: GameSnapshot,
    encounter_type: str,
    outcome: str,
    actor: str,
) -> None:
    """Fire magic-confrontation mandatory_outputs at encounter resolution.

    No-op when the resolved encounter type does not match a magic
    confrontation id, or when ``magic_state`` is unloaded — the
    encounter system handles a much wider catalog than just the named
    magic confrontations, so non-matches must pass through cleanly.

    Emits a ``magic`` watcher event with ``op=confrontation_outcome``
    on success so the GM panel sees the resolved branch + outputs that
    fired. The resolved payload is also stashed on
    ``snapshot.pending_magic_confrontation_outcome`` for the room
    dispatcher to forward to the UI as ``CONFRONTATION_OUTCOME``.
    """
    if snapshot.magic_state is None:
        return
    if not any(c.id == encounter_type for c in snapshot.magic_state.confrontations):
        return
    branch = _OUTCOME_TO_BRANCH.get(outcome.lower())
    if branch is None:
        # Outcome string does not map cleanly to a four-branch outcome.
        # Log loud per CLAUDE.md no-silent-fallback so authoring can
        # reconcile the encounter outcome catalog with the magic
        # confrontation branch enum, but don't raise — the encounter
        # has already resolved and the player has already lived through
        # it; refusing to fire mandatory_outputs would orphan the
        # confrontation more than logging the mismatch does.
        logger.error(
            "magic.confrontation_outcome_unmapped encounter_type=%s outcome=%s actor=%s",
            encounter_type,
            outcome,
            actor,
        )
        return

    # ``branch`` is constrained to the four-branch literal by the .get()
    # lookup above (None-case returned earlier), so the cast is safe.
    typed_branch = cast(BranchName, branch)

    payload = resolve_magic_confrontation(
        snapshot=snapshot,
        confrontation_id=encounter_type,
        branch=typed_branch,
        actor=actor,
    )
    if payload is None:
        return

    # GM-panel watcher event — Sebastien's mechanical-visibility lens.
    _watcher_publish(
        "state_transition",
        {
            "field": "magic_state",
            "op": "confrontation_outcome",
            "confrontation_id": encounter_type,
            "branch": branch,
            "actor": actor,
            "mandatory_outputs": list(payload["mandatory_outputs"]),
        },
        component="magic",
    )

    # Stash the payload for the session handler to dispatch as a
    # CONFRONTATION_OUTCOME WebSocket message. The handler clears the
    # field after dispatch.
    snapshot.pending_magic_confrontation_outcome = payload

    # Drain pending_status_promotions into the actor's Character.core.statuses
    # so the player's Status panel reflects the new Wound/Scar/Boon
    # alongside the bar updates. Promotions for actors not in
    # ``snapshot.characters`` (NPC magic confrontations, save-state
    # races) stay queued; the handler can clear them or surface them as
    # warnings.
    if snapshot.magic_state is not None:
        _drain_pending_status_promotions(snapshot=snapshot)


@dataclass
class _OpposedBranchOutcome:
    """Outcome of one opposed_check dispatch branch — kept terse."""

    encounter_resolved: bool


def _roll_d20_server_side() -> int:
    """Server-side d20 roll for the opponent in an opposed-check turn.

    Recommended in ``.archive/handoffs/opposed-checks-design.md`` §Open
    questions (1): opponent's d20 is server-side, no animation. The
    rolling player keeps their physics-settled die; the opponent's roll
    appears in the result pane after both sides commit.

    Wrapped so tests can monkey-patch the import for deterministic
    coverage of the shift bands without touching the global RNG.
    """
    import random

    return random.randint(1, 20)


def _opposed_dc(beat: Any) -> int:
    """Per-side DC derived from beat ``base`` magnitude, clamped 10..=30.

    Mirrors ``sidequest.server.dispatch.dice._compute_dc`` so a player
    using the dispatch path and an opponent using this resolver land on
    the same DC for the same beat.
    """
    return max(10, min(30, 10 + abs(getattr(beat, "base", 1)) * 2))


_DECISIVE_MARGIN = 10  # mirrors sidequest.game.dice.DECISIVE_MARGIN


def _classify_legacy_tier(d20: int, modifier: int, difficulty: int) -> RollOutcome:
    """Per-side tier from one d20 face-value vs a DC.

    Mirrors ``resolve_dice_with_faces`` for the d20-only common case so
    each side of an opposed_check resolves *its own* roll against *its
    own* DC (Keith's rule, playtest 2026-05-06): plain Success on one
    side must not depend on the other side's shift.

    Crit rules (locked 2026-04-11, Keith):

    - nat20 → CritSuccess
    - nat1  → CritFail
    - total ≥ DC + 10 → CritSuccess (decisive margin)
    - total > DC → Success
    - total = DC → Tie
    - total < DC → Fail
    """
    if d20 == 20:
        return RollOutcome.CritSuccess
    if d20 == 1:
        return RollOutcome.CritFail
    total = d20 + modifier
    if total >= difficulty + _DECISIVE_MARGIN:
        return RollOutcome.CritSuccess
    if total > difficulty:
        return RollOutcome.Success
    if total == difficulty:
        return RollOutcome.Tie
    return RollOutcome.Fail


_TIER_DOWNGRADE: dict[RollOutcome, RollOutcome] = {
    RollOutcome.CritSuccess: RollOutcome.Success,
    RollOutcome.Success: RollOutcome.Tie,
}


def _downgrade_one_step(tier: RollOutcome) -> RollOutcome:
    """Counteract reduces an offensive tier by one step.

    Per Keith's rule (playtest 2026-05-06): a successful defender does
    not zero out the attacker — the attacker still rolled — but the
    defender's success robs the attacker of the Success-tier bonus.
    CritSuccess → Success (loses fleeting Opening tag); Success → Tie
    (still grants base // 2 momentum). Failed offenses don't get worse
    from a successful defense.
    """
    return _TIER_DOWNGRADE.get(tier, tier)


def _brace_counteracts(
    *,
    defender_beat: Any,
    defender_selection: Any,
    attacker_actor_name: str,
    attacker_target: str | None,
) -> bool:
    """True iff the defender's brace counteracts the attacker.

    "Counteract" recognises two narration patterns (both observed in
    playtest):

    1. **Brace against attacker.** Defender's beat is ``brace`` and its
       ``target`` matches the attacker's actor name. This is the
       "opponent braces against the player's attack" case — the
       opponent has named the source of the threat as their target.
    2. **Shield the attacker's target.** Defender's beat is ``brace``
       and its ``target`` matches the actor the attacker is hitting.
       This is the "companion-NPC ally shields the patron" case —
       Donut's ``defend target='Carl'`` while Sumpdrake's ``attack
       target='Carl'``. The narrator's prose puts Donut between the
       drake and Carl; the engine must read that intent from the
       ``target`` field rather than treating it as self-defense.

    Both forms produce the same mechanical effect: the attacker's tier
    downgrades by one step (CritSuccess → Success, Success → Tie).
    Failed offenses don't get worse from a successful defense.

    A brace with ``target=<self>`` (defender shielding themselves
    while the attacker is hitting someone else) is NOT a counteract —
    self-defense doesn't intercept attacks on third parties. A brace
    with no ``target`` field is also not a counteract; the narrator
    must name an actor explicitly so the GM panel can audit the gate
    decision.

    Match is case-insensitive and trim-tolerant (narrator output is
    not always whitespace-clean).
    """
    from sidequest.game.beat_kinds import BeatKind

    if getattr(defender_beat, "kind", None) != BeatKind.brace:
        return False
    target_raw = getattr(defender_selection, "target", None)
    if not target_raw:
        return False
    target_norm = str(target_raw).strip().lower()
    if target_norm == attacker_actor_name.strip().lower():
        return True
    return bool(attacker_target and target_norm == str(attacker_target).strip().lower())


# Legacy alias retained for any in-repo callers / tests that imported
# the old name. New code should use ``_brace_counteracts``.
def _is_brace_targeting(
    *,
    defender_beat: Any,
    defender_selection: Any,
    attacker_actor_name: str,
) -> bool:
    return _brace_counteracts(
        defender_beat=defender_beat,
        defender_selection=defender_selection,
        attacker_actor_name=attacker_actor_name,
        attacker_target=None,
    )


def _resolve_opposed_check_branch(
    *,
    encounter,
    cdef,
    selections,
    pack_beats,
    pending_player_d20: int | None,
    pending_player_beat_id: str | None,
    pending_player_actor: str | None,
    turn: int,
    snapshot: GameSnapshot,
) -> _OpposedBranchOutcome:
    """Run the opposed-check dispatch branch.

    Pulls the player's roll + beat from the pending stash (set by
    ``dispatch_dice_throw``), finds the narrator-emitted opponent beat
    in ``selections`` (the SOUL gate will already have dropped any
    PC-side selections — the opponent beat is what survives), rolls the
    opponent's d20 server-side, runs ``resolve_opposed_check`` to derive
    the tier, emits the lie-detector OTEL span, and finally calls
    ``apply_beat`` for both sides with the engine-derived tier.

    Hard-fails-loud (CLAUDE.md no-silent-fallback) when:

    - ``pending_player_*`` are None — opposed_check requires a preceding
      DICE_THROW frame; the legacy narrator-only path is structurally
      ineligible because PC mechanical actions must trace back to an
      explicit player consent.
    - The player's beat_id is not in ``pack_beats``.
    - No opponent-side beat selection is present in ``selections``.
    - The opponent's beat_id is not in ``pack_beats``.
    """
    from sidequest.game.beat_kinds import apply_beat
    from sidequest.game.opposed_check import resolve_opponent_modifier, resolve_opposed_check
    from sidequest.telemetry.spans import (
        encounter_beat_skipped_span,
        encounter_opposed_roll_resolved_span,
        encounter_resolved_span,
    )

    if pending_player_d20 is None or pending_player_beat_id is None or pending_player_actor is None:
        raise ValueError(
            f"opposed_check encounter {encounter.encounter_type!r} narration "
            f"arrived without a pending DICE_THROW player roll — "
            f"opposed_check is dice-throw-only (no narrator-only path). "
            f"Bug: dispatch_dice_throw should have stashed "
            f"pending_opposed_player_d20 / _beat_id / _actor on session_data."
        )

    player_actor = encounter.find_actor(pending_player_actor)
    if player_actor is None:
        # Fall back to the first player-side actor — same fallback logic
        # the dice dispatcher applies, kept symmetric so a stash mismatch
        # doesn't destroy the resolution.
        player_actor = next(
            (a for a in encounter.actors if a.side == "player"),
            None,
        )
    if player_actor is None:
        raise ValueError(
            f"opposed_check: no player actor found for pending roll "
            f"(actor name {pending_player_actor!r} not in encounter; "
            f"no fallback player-side actor available)"
        )

    player_beat = pack_beats.get(pending_player_beat_id)
    if player_beat is None:
        raise ValueError(
            f"opposed_check: pending player beat_id "
            f"{pending_player_beat_id!r} not in pack beats for encounter "
            f"{encounter.encounter_type!r}"
        )

    opponent_selection = None
    for sel in selections:
        sel_actor = encounter.find_actor(sel.actor)
        if sel_actor is None:
            continue
        if sel_actor.side == "opponent" and not sel_actor.withdrawn:
            opponent_selection = sel
            break

    if opponent_selection is None:
        raise ValueError(
            f"opposed_check: narrator emitted no opponent-side beat "
            f"selection for encounter {encounter.encounter_type!r}. The "
            f"engine cannot derive an opposed tier without an opposing "
            f"beat. (Narrator prompt regression — see narrator gate text "
            f"for opposed_check.)"
        )

    # Gather player-side companion selections (player-side actors that
    # are NOT the rolling player). These are companion-NPC beats: the
    # narrator drives them per turn (Donut's defend target='Carl', etc.)
    # The opposed_check branch must apply these too — pre-fix they fell
    # off the floor because the loop only searched for the single
    # opponent. Playtest 2026-05-06 (sumpdrake fight) regression.
    companion_selections: list[Any] = []
    for sel in selections:
        sel_actor = encounter.find_actor(sel.actor)
        if sel_actor is None or sel_actor.withdrawn:
            continue
        if sel_actor.side != "player":
            continue
        if sel_actor.name == player_actor.name:
            # The rolling player's own beat — not a companion. The
            # SOUL gate above should have stripped this for narrator-
            # extracted PC beats; defensive skip in case a future call
            # site passes the player's own selection through.
            continue
        companion_selections.append(sel)

    opponent_actor = encounter.find_actor(opponent_selection.actor)
    if opponent_actor is None:
        raise ValueError(
            f"opposed_check: opponent beat selection references actor "
            f"{opponent_selection.actor!r} not in encounter actors"
        )
    opponent_beat = pack_beats.get(opponent_selection.beat_id)
    if opponent_beat is None:
        raise ValueError(
            f"opposed_check: opponent beat_id {opponent_selection.beat_id!r} "
            f"not in pack beats for encounter {encounter.encounter_type!r}"
        )

    opponent_d20 = _roll_d20_server_side()

    # ``resolve_opposed_check`` is retained for its per-side modifier
    # resolution (stat lookup with hard-fail-loud on missing stats) and
    # the shift computation that the lie-detector OTEL span has carried
    # since 2026-04-26. The shift-derived tier on ``roll_result.tier`` is
    # NO LONGER used to drive ``apply_beat`` — see the per-side tier
    # block below for Keith's playtest 2026-05-06 rule.
    roll_result = resolve_opposed_check(
        player_actor=player_actor,
        opponent_actor=opponent_actor,
        player_beat=player_beat,
        opponent_beat=opponent_beat,
        cdef=cdef,
        player_roll=pending_player_d20,
        opponent_roll=opponent_d20,
        encounter=encounter,
        edge_resolver=snapshot.find_creature_core,
    )

    # ---- Per-side tier resolution + counteract detection ---------------
    # Bug fixed (playtest 2026-05-06, sumpdrake fight): the prior shift-
    # tier-applied-to-both-sides logic produced two structurally wrong
    # behaviors:
    #
    # 1. A player CritSuccess (shift +13) handed the OPPONENT CritSuccess
    #    too — sumpdrake's strike dial advanced +base on every Carl crit.
    # 2. A player Success against an opponent rolling normally (shift in
    #    [-1, +1]) collapsed to Tie tier, and a Success against an
    #    opponent rolling well (shift ≤ -2) collapsed to Fail. So plain
    #    Successes never moved the player dial — only nat20s did.
    #
    # Keith's rule (canonical): "a plain Success outcome on a beat MUST
    # shift momentum unless the opponent's parallel beat *successfully
    # counteracts* it (e.g. an opposed attack/defend pair where the
    # defender also passed). Crits are a bonus on top, not the only
    # mover."
    #
    # Implementation:
    #
    # - Each side resolves its own roll-vs-DC tier (legacy semantics,
    #   matches ``resolve_dice_with_faces``).
    # - "Counteract" = the opponent's beat is ``brace`` AND its target
    #   matches the attacker's actor name AND the defender's own tier is
    #   Success or CritSuccess.
    # - On counteract the attacker's tier downgrades by one step
    #   (CritSuccess → Success, Success → Tie). Tie/Fail/CritFail are
    #   unchanged (no further downgrade — failed offenses stay failed).
    #
    # The shift remains visible to the GM panel as a single scalar so the
    # old lie-detector span is still readable; it is no longer load-
    # bearing for ``apply_beat``.
    player_dc = _opposed_dc(player_beat)
    opponent_dc = _opposed_dc(opponent_beat)
    player_tier_raw = _classify_legacy_tier(pending_player_d20, roll_result.player_mod, player_dc)
    opponent_tier_raw = _classify_legacy_tier(opponent_d20, roll_result.opponent_mod, opponent_dc)

    # Roll + classify each companion's beat. Their tiers feed both the
    # ally-counteract gate (companion brace shielding the player from
    # the opponent's attack) and the per-companion ``apply_beat`` call
    # below. Stat lookup walks per_actor_state['stats'] first then
    # falls back to cdef.opponent_default_stats; both are valid sources
    # for a player-side companion (recruiter pipeline currently does
    # not seed companion per-actor stats, so the cdef-default path
    # applies — see ``resolve_opponent_modifier`` for the exact lookup).
    companion_rolls: list[dict[str, Any]] = []
    for c_sel in companion_selections:
        c_actor = encounter.find_actor(c_sel.actor)
        if c_actor is None:
            continue
        c_beat = pack_beats.get(c_sel.beat_id)
        if c_beat is None:
            # Skip with a loud span — pack-data inconsistency from the
            # narrator (named a beat_id that doesn't exist in the cdef).
            with encounter_beat_skipped_span(
                reason="unknown_beat_id",
                actor=c_actor.name,
                actor_side=c_actor.side,
                beat_id=c_sel.beat_id,
            ):
                pass
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "beat_skipped",
                    "reason": "unknown_beat_id",
                    "actor": c_actor.name,
                    "actor_side": c_actor.side,
                    "beat_id": c_sel.beat_id,
                    "source": "opposed_check_companion",
                },
                component="encounter",
            )
            continue
        c_d20 = _roll_d20_server_side()
        try:
            c_mod = resolve_opponent_modifier(
                actor=c_actor,
                cdef=cdef,
                stat_check=getattr(c_beat, "stat_check", "") or "",
            )
        except ValueError:
            # Hard-fail-loud per CLAUDE.md no-silent-fallback: if a
            # companion beat names a stat with no value source, the
            # pack data is broken. Emit and skip — failing the whole
            # round just because a hireling has no stat block would
            # block every combat turn the moment Donut joins.
            with encounter_beat_skipped_span(
                reason="missing_stat_source",
                actor=c_actor.name,
                actor_side=c_actor.side,
                beat_id=c_sel.beat_id,
            ):
                pass
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "beat_skipped",
                    "reason": "missing_stat_source",
                    "actor": c_actor.name,
                    "actor_side": c_actor.side,
                    "beat_id": c_sel.beat_id,
                    "stat_check": getattr(c_beat, "stat_check", "") or "",
                    "source": "opposed_check_companion",
                },
                component="encounter",
                severity="warning",
            )
            continue
        c_dc = _opposed_dc(c_beat)
        c_tier = _classify_legacy_tier(c_d20, c_mod, c_dc)
        companion_rolls.append(
            {
                "actor": c_actor,
                "beat": c_beat,
                "selection": c_sel,
                "beat_id": c_sel.beat_id,
                "d20": c_d20,
                "mod": c_mod,
                "dc": c_dc,
                "tier": c_tier,
            }
        )

    opponent_counteracts_player = _brace_counteracts(
        defender_beat=opponent_beat,
        defender_selection=opponent_selection,
        attacker_actor_name=player_actor.name,
        attacker_target=getattr(player_beat, "target_tag", None),
    ) and opponent_tier_raw in (RollOutcome.Success, RollOutcome.CritSuccess)
    # Player→opponent counteract requires knowing the player's beat
    # target. The dispatch path does not currently plumb that through,
    # so v1 leaves player counteracts as False; opponent always resolves
    # with their own tier. A follow-up will plumb player target via the
    # session_data stash so attacker-symmetry can be implemented for
    # both sides.
    player_counteracts_opponent = False

    # Ally counteract: a companion brace whose ``target`` matches the
    # opponent (bracing against the opponent) OR matches the opponent's
    # target (shielding the player the opponent is attacking). The
    # opponent's beat must be threatening someone for the shield form
    # to apply. Either form, with companion tier ≥ Success, downgrades
    # the opponent's tier by one step. Playtest 2026-05-06 (sumpdrake):
    # Donut's ``defend target='Carl'`` while Sumpdrake's ``attack
    # target='Carl'`` — narration described Donut shielding Carl, but
    # the engine never modeled it. Now it does.
    opponent_target = getattr(opponent_selection, "target", None)
    ally_counteract_sources: list[str] = []
    for cr in companion_rolls:
        if cr["tier"] not in (RollOutcome.Success, RollOutcome.CritSuccess):
            continue
        if _brace_counteracts(
            defender_beat=cr["beat"],
            defender_selection=cr["selection"],
            attacker_actor_name=opponent_actor.name,
            attacker_target=opponent_target,
        ):
            ally_counteract_sources.append(cr["actor"].name)
    ally_counteracts_opponent = bool(ally_counteract_sources)

    player_tier_final = (
        _downgrade_one_step(player_tier_raw) if opponent_counteracts_player else player_tier_raw
    )
    # Opponent gets downgraded if either the player counteracts (v1
    # placeholder) OR an ally counteracts. Single-step downgrade
    # regardless of how many shields land — one step is one step.
    opponent_tier_final = (
        _downgrade_one_step(opponent_tier_raw)
        if (player_counteracts_opponent or ally_counteracts_opponent)
        else opponent_tier_raw
    )

    # Lie-detector span (back-compat shape: one tier scalar). We surface
    # ``player_tier_final`` as the headline tier so a GM auditing the
    # span sees what actually drove the player's metric_advance. Full
    # per-side breakdown lives on the watcher event below — that is the
    # GM-panel feed.
    with encounter_opposed_roll_resolved_span(
        encounter_type=encounter.encounter_type,
        player_roll=roll_result.player_roll,
        player_mod=roll_result.player_mod,
        opponent_roll=roll_result.opponent_roll,
        opponent_mod=roll_result.opponent_mod,
        player_num_advantage=roll_result.player_num_advantage,
        opponent_num_advantage=roll_result.opponent_num_advantage,
        shift=roll_result.shift,
        tier=player_tier_final.value,
    ):
        pass
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "opposed_roll_resolved",
            "encounter_type": encounter.encounter_type,
            "player_roll": roll_result.player_roll,
            "player_mod": roll_result.player_mod,
            "player_dc": player_dc,
            "player_tier_raw": player_tier_raw.value,
            "player_tier_final": player_tier_final.value,
            "opponent_roll": roll_result.opponent_roll,
            "opponent_mod": roll_result.opponent_mod,
            "player_num_advantage": roll_result.player_num_advantage,
            "opponent_num_advantage": roll_result.opponent_num_advantage,
            "shift": roll_result.shift,
            "opponent_counteracts_player": opponent_counteracts_player,
            "player_counteracts_opponent": player_counteracts_opponent,
            "ally_counteracts_opponent": ally_counteracts_opponent,
            "ally_counteract_sources": ally_counteract_sources,
            "companion_count": len(companion_rolls),
            "companion_rolls": [
                {
                    "actor": cr["actor"].name,
                    "beat_id": cr["beat_id"],
                    "d20": cr["d20"],
                    "mod": cr["mod"],
                    "dc": cr["dc"],
                    "tier": cr["tier"].value,
                }
                for cr in companion_rolls
            ],
            # Back-compat alias — older watchers read ``tier`` (scalar).
            "tier": player_tier_final.value,
        },
        component="encounter",
    )

    encounter_resolved = False
    # Apply player beat first (matches threshold-cross order in apply_beat
    # docstring — "player_metric first, then opponent_metric"). Each side
    # gets its OWN final tier (per-side resolution above) — the prior
    # behavior of feeding the same shift-tier to both apply_beat calls
    # was the structural bug. Companions apply LAST (after opponent) so
    # threshold-cross order is preserved when a companion brace drains
    # the opponent's dial below the resolution threshold.
    # Apply targets carry a per-actor source label (player/opponent/
    # companion) so beat_applied watcher events distinguish the
    # companion path from the primary opposed pair. Sebastien's
    # mechanics-first lens needs this — a companion brace draining the
    # opponent's dial should be visibly separate from the opponent
    # rolling their own beat.
    apply_targets: list[tuple[Any, Any, str, RollOutcome, str]] = [
        (
            player_actor,
            player_beat,
            pending_player_beat_id,
            player_tier_final,
            "opposed_check_player",
        ),
        (
            opponent_actor,
            opponent_beat,
            opponent_selection.beat_id,
            opponent_tier_final,
            "opposed_check_opponent",
        ),
    ]
    for cr in companion_rolls:
        apply_targets.append(
            (
                cr["actor"],
                cr["beat"],
                cr["beat_id"],
                cr["tier"],
                "opposed_check_companion",
            ),
        )
    for sel_actor, sel_beat, beat_id, sel_tier, sel_source in apply_targets:
        applied = apply_beat(
            encounter,
            sel_actor,
            sel_beat,
            sel_tier,
            turn=turn,
            edge_resolver=snapshot.find_creature_core,
        )
        if applied.skipped_reason is not None:
            with encounter_beat_skipped_span(
                reason=applied.skipped_reason,
                actor=sel_actor.name,
                actor_side=sel_actor.side,
                beat_id=beat_id,
            ):
                pass
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "beat_skipped",
                    "reason": applied.skipped_reason,
                    "actor": sel_actor.name,
                    "actor_side": sel_actor.side,
                    "beat_id": beat_id,
                    "source": sel_source,
                },
                component="encounter",
            )
            continue
        own_delta = applied.deltas.own if applied.deltas else 0
        opp_delta = applied.deltas.opponent if applied.deltas else 0
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter",
                "op": "beat_applied",
                "actor": sel_actor.name,
                "actor_side": sel_actor.side,
                "beat_id": beat_id,
                "beat_kind": (
                    sel_beat.kind.value if hasattr(sel_beat.kind, "value") else str(sel_beat.kind)
                ),
                "outcome_tier": sel_tier.value,
                "own_delta": own_delta,
                "opponent_delta": opp_delta,
                "metric_target": encounter.encounter_type,
                "source": sel_source,
            },
            component="encounter",
        )
        # Story 45-9: bump total_beats_fired counter + OTEL.
        snapshot.record_beat_fired(
            beat_id=beat_id,
            encounter_type=encounter.encounter_type,
            turn=turn,
            source=sel_source,
        )

        # B/X morale per-beat hook (Task 9, architect feedback 2026-05-08).
        # Mirrors the legacy beat-loop site above. ``pack`` is not threaded
        # into this branch — pass None to ``_all_opponents_mindless``;
        # V1 returns False either way (see ``_all_opponents_mindless``
        # deviation note). See ``_emit_morale_triggers`` docstring for the
        # full dial-as-pseudo-HP approximation.
        if applied.deltas is not None:
            if sel_actor.side == "player":
                morale_dial_delta = max(applied.deltas.own, 0)
            else:
                morale_dial_delta = max(applied.deltas.opponent, 0)
            if morale_dial_delta > 0:
                pm = encounter.player_metric
                threshold = max(pm.threshold, 1)
                post_value = pm.current
                pre_value = max(0, post_value - morale_dial_delta)
                pseudo_initial = threshold
                pseudo_pre_alive = max(0, threshold - pre_value)
                pseudo_post_alive = max(0, threshold - post_value)
                if pseudo_pre_alive != pseudo_post_alive:
                    opp_actors_for_morale = [a for a in encounter.actors if a.side == "opponent"]
                    all_mindless = _all_opponents_mindless(opp_actors_for_morale, None)
                    pre_states = [
                        OpponentState(
                            id=str(i),
                            alive=(i < pseudo_pre_alive),
                            mindless=all_mindless,
                        )
                        for i in range(pseudo_initial)
                    ]
                    post_states = [
                        OpponentState(
                            id=str(i),
                            alive=(i < pseudo_post_alive),
                            mindless=all_mindless,
                        )
                        for i in range(pseudo_initial)
                    ]
                    morale_fired = _emit_morale_triggers(
                        encounter,
                        cdef,
                        encounter.encounter_type,
                        pre_states,
                        post_states,
                        False,
                        Random(),
                    )
                    _apply_flee_consequences(encounter, cdef, morale_fired)

        if applied.resolved:
            with encounter_resolved_span(
                encounter_type=encounter.encounter_type,
                outcome=encounter.outcome or "",
                source=sel_source,
            ):
                pass
            _watcher_publish(
                "state_transition",
                {
                    "field": "encounter",
                    "op": "resolved",
                    "encounter_type": encounter.encounter_type,
                    "outcome": encounter.outcome or "",
                    "source": sel_source,
                    "final_player_metric": encounter.player_metric.current,
                    "final_opponent_metric": encounter.opponent_metric.current,
                },
                component="encounter",
            )
            encounter_resolved = True
            break

    return _OpposedBranchOutcome(encounter_resolved=encounter_resolved)


# ---------------------------------------------------------------------------
# Story 45-20 — trope resolution handshake.
# ---------------------------------------------------------------------------

# Guardrail length for ``active_stakes`` so runaway growth does not pollute
# the next narrator's state_summary prompt. The field is reflected verbatim
# into the prompt JSON; ~1024 chars is the soft cap.
_ACTIVE_STAKES_GUARDRAIL = 1024


def _handshake_resolved_tropes(
    snapshot: GameSnapshot,
    baseline_status: dict[str, str],
    *,
    player_name: str,
    source: str,
) -> None:
    """Diff ``baseline_status`` against the snapshot's current
    ``active_tropes`` and write the durable record for every trope whose
    current status is ``"resolved"``.

    For each detected trope:

    - If the baseline status was anything other than ``"resolved"``
      (including absent — a brand-new resolved trope from chapter
      promotion), this is a fresh resolution: write
      ``quest_log[f"trope_{id}"]`` (wrapped in ``quest_update_span`` so
      the existing GM-panel ``SPAN_QUEST_UPDATE`` route surfaces the
      entry) and append a resolution marker to ``active_stakes``,
      trimming if the field exceeds ``_ACTIVE_STAKES_GUARDRAIL``.
    - If the baseline status was ``"resolved"``, this is an idempotent
      re-detect: no rewrite, but the handshake span still fires with
      ``active_stakes_appended=False`` so the GM panel can distinguish
      "handshake correctly idempotent" from "handshake never engaged
      after turn N".

    The lie-detector contract is that one span fires per detected
    resolved trope, every turn. The bug Orin saw was zero spans firing
    at all.
    """

    interaction = snapshot.turn_manager.interaction
    fresh_writes: dict[str, str] = {}

    for trope in snapshot.active_tropes:
        if trope.status != "resolved":
            continue

        prior = baseline_status.get(trope.id, "")
        is_fresh = prior != "resolved"
        quest_log_key = f"trope_{trope.id}"

        if is_fresh:
            entry_text = f"Resolved at turn {interaction}"
            fresh_writes[quest_log_key] = entry_text

            marker = f"[Resolved: {trope.id} on turn {interaction}]"
            existing = snapshot.active_stakes
            if existing:
                snapshot.active_stakes = f"{existing}\n{marker}"
            else:
                snapshot.active_stakes = marker
            if len(snapshot.active_stakes) > _ACTIVE_STAKES_GUARDRAIL:
                # Trim oldest content but always keep the new marker
                # at the tail — that is the load-bearing field for the
                # next narrator.
                tail = marker
                head_budget = _ACTIVE_STAKES_GUARDRAIL - len(tail) - 1
                head = snapshot.active_stakes[:head_budget]
                snapshot.active_stakes = f"{head}\n{tail}"

        with trope_resolution_handshake_span(
            trope_id=trope.id,
            prior_status=prior,
            new_status="resolved",
            interaction=interaction,
            quest_log_key=quest_log_key,
            active_stakes_appended=is_fresh,
            source=source,
        ):
            pass

    if fresh_writes:
        with quest_update_span(
            updates=fresh_writes,
            player_name=player_name,
            turn_number=interaction,
        ):
            for key, status_text in fresh_writes.items():
                snapshot.quest_log[key] = status_text
            logger.info(
                "trope.resolution_handshake fresh_writes=%d player=%s turn=%d",
                len(fresh_writes),
                player_name,
                interaction,
            )
