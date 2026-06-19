"""Apply NarrationTurnResult mutations to GameSnapshot.

Extracted from session_handler.py — pure functions over snapshot + result.
Re-exported by session_handler for back-compat.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from random import Random
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import ValidationError

if TYPE_CHECKING:
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
    from sidequest.agents.sidecar_extractor import SidecarExtraction
    from sidequest.dungeon.lookahead_worker import LookaheadWorkerHandle
    from sidequest.game.character import Character
    from sidequest.game.encounter import EncounterActor, EncounterPhase, StructuredEncounter
    from sidequest.game.monster_manual import MonsterManual
    from sidequest.genre.models.world import CartographyConfig
    from sidequest.genre.names.generator import NameGenerator
    from sidequest.magic.confrontations import ConfrontationDefinition
    from sidequest.server.session_room import SessionRoom

from sidequest.game.alias_accretion import (
    accrete_npc_aliases,
    extract_epithets_for_npc,
)
from sidequest.game.alias_resolution import _phrase_matches
from sidequest.game.disposition import Disposition
from sidequest.game.dogfight_shot import (
    GunSolution,
    PendingDogfightShot,
    build_dogfight_shot_inputs,
    build_dogfight_weapon_lookup,
    frame_hp_resolver,
    resolve_dogfight_shots,
)
from sidequest.game.encounter_classifier import is_player_victory, yield_side_for
from sidequest.game.item_catalog_resolution import resolve_gained_item_dict
from sidequest.game.morale import (
    MoraleOutcome,
    OpponentSideState,
    OpponentState,
    maybe_check_morale,
)
from sidequest.game.npc_development import (
    ACQUAINTANCE_AT,
    DISPOSITION_DRIFT_PER_MILESTONE,
    develop_npc_on_engagement,
    engagement_beat_reason,
    tier_for_interactions,
)
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.region_validation import (
    canonicalize_region_name,
    resolve_known_region_id,
    validate_region_name,
)
from sidequest.game.ruleset.registry import get_ruleset_module

# Story 105-2: seam registry — static→procedural crossing recovery.
from sidequest.game.seams import SeamCrossingError, get_seam_resolver, seam_route_for
from sidequest.game.session import (
    _ACTIVE_STAKES_GUARDRAIL,  # re-exported; canonical home is session.py (Story 77-2)
    ContainerState,
    GameSnapshot,
    Npc,
    RegionTransition,
    RoomState,
    upsert_quest_status,
)
from sidequest.game.table.types import TableCommit
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import FleeConsequence, MoraleTrigger, ResolutionMode
from sidequest.genre.names import generator as _namegen_module
from sidequest.genre.names.generator import has_stem_collision
from sidequest.magic.confrontations import (
    BranchName,
    evaluate_auto_fire_triggers,
)
from sidequest.magic.models import Flag, MagicWorking
from sidequest.magic.state import ApplyWorkingResult, ThresholdCrossingEvent
from sidequest.magic.validator import validate as magic_validate
from sidequest.protocol.dice import RollOutcome
from sidequest.protocol.messages import DiceRequestMessage, DiceResultMessage
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
    SPAN_DISPOSITION_SHIFT,
    Span,
    container_retrieval_blocked_span,
    container_retrieval_recorded_span,
    inventory_narrator_extracted_span,
    location_drift_repaired_span,
    lore_established_span,
    magic_working_span,
    npc_auto_registered_span,
    npc_creature_bestiary_draw_span,
    npc_creature_preserved_span,
    npc_creature_reconciled_span,
    npc_developed_span,
    npc_epithet_preserved_span,
    npc_epithet_reconciled_span,
    npc_identity_seeded_span,
    npc_invented_name_routed_span,
    npc_invented_name_unrouted_span,
    npc_mentions_replay_suppressed_span,
    npc_observation_gate_order_violation_span,
    npc_pc_name_skipped_span,
    npc_referenced_span,
    npc_spawn_disposition_span,
    quest_update_span,
    quest_updates_legacy_emitted_span,
    region_entry_canonicalized_dedup_span,
    region_entry_rejected_span,
    state_patch_hp_span,
    table_commit_span,
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


def _entrance_room_name(
    *, crossing_region: str, lookahead_handle: LookaheadWorkerHandle | None
) -> str:
    """The authored entrance room's display name for the scene re-anchor.

    Loads ``rooms/<region>.yaml`` via the same loader path map_emit uses.
    A missing authored room is LOUD (warning + raw region id as the title)
    but not fatal — the crossing itself already happened mechanically.

    Story 105-2 §4 Piece 1: the narrator record must carry the REAL room
    name, not the confabulated heading.
    """
    from sidequest.game.room_file_loader import load_room_payload
    from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader

    if lookahead_handle is None or not lookahead_handle.genre_slug:
        return crossing_region
    try:
        loader = GenreLoader(search_paths=DEFAULT_GENRE_PACK_SEARCH_PATHS)
        world_dir = (
            loader.find(lookahead_handle.genre_slug) / "worlds" / lookahead_handle.world_slug
        )
        payload = load_room_payload(
            world_dir, crossing_region, genre_slug=lookahead_handle.genre_slug
        )
        # ``load_room_payload`` returns a TacticalGridPayload model (NOT a
        # dict) — the display name is ``room_name``. (Spec review caught a
        # ``payload.get("name")`` AttributeError here that the broad except
        # silently degraded to the region id.)
        return str(payload.room_name or crossing_region)
    except Exception as exc:  # noqa: BLE001 — loud, non-fatal
        logger.warning(
            "seam.entrance_room_name_unresolved region=%s error=%s — "
            "authored entrance room missing; using region id as scene title "
            "(author rooms/%s.yaml — spec 2026-06-12 §4 Piece 1)",
            crossing_region,
            exc,
            crossing_region,
        )
        return crossing_region


def _reanchor_location_ledger(
    snapshot: GameSnapshot,
    *,
    confabulated: str,
    replacement: str | None,
    actor_for_location: str | None,
) -> None:
    """Rewrite ``character_locations`` entries clobbered with a confabulated heading.

    Story 105-2 guard-path fix: the per-character ledger write (and the MP
    scene-cohort propagation) runs BEFORE region resolution, so by the time a
    guard decides the narrator's heading was a confabulation, the acting PC —
    and every co-located follower — already carries it. Guards call this to
    make ledger and scene agree again:

    * ``replacement`` is the honest value (restored prior location, canonical
      region name, or the re-anchored entrance room name).
    * ``replacement=None`` removes the actor's entry (there was no prior
      location to restore). Cohort followers only exist when there WAS a
      prior location (the follow is gated on ``old_loc`` truthy), so they are
      only rewritten, never popped.

    Only entries still equal to ``confabulated`` are touched — a peer at a
    genuinely different location (party split) is never dragged along.
    """
    if actor_for_location and snapshot.character_locations.get(actor_for_location) == confabulated:
        if replacement is None:
            snapshot.character_locations.pop(actor_for_location, None)
        else:
            snapshot.character_locations[actor_for_location] = replacement
    if replacement is not None:
        for seated_name in snapshot.player_seats.values():
            if not seated_name or seated_name == actor_for_location:
                continue
            if snapshot.character_locations.get(seated_name) == confabulated:
                snapshot.character_locations[seated_name] = replacement


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


def _resolve_wwn_cast_for_beat(
    *,
    sel: BeatSelection,
    actor: EncounterActor,
    snapshot: GameSnapshot,
    pack: GenrePack,
    encounter,
    cdef,
) -> None:
    """Drive WwnRulesetModule.resolve_spellcast for a cast_spell beat, apply
    rolled spell damage to the defender's HP, then run the SAME CWN/WWN downed
    seam the strike path uses. (Origin: WWN Content Plan 3 Task 7.)

    TWO ENTRY POINTS, one implementation (epic 102 "Reuse-first"): the
    narrator apply_beat path (``spell_id`` from the BeatSelection sidecar)
    and the dice path (``dispatch_dice_throw``, story 102-2 — ``spell_id``
    from ``DiceThrowPayload``, the UI spell picker). Asymmetry note: a
    missing ``spell_id`` here is a narrator omission and degrades to a
    watcher-warning no-op (``wwn.cast_spell_no_spell_id``); the dice path
    pre-validates and raises ``DiceDispatchError`` instead, because a
    pickerless cast commit is a malformed client request.

    Mirrors ``_resolve_innate_cast_for_beat``'s guard idiom: each missing
    precondition publishes a ``_watcher_publish`` event (the lie-detector) and
    the cast is refused-but-recorded — never a silent no-op, never a raise on a
    normal miss. ``resolve_spellcast`` emits the ``wwn.spell.cast`` span on every
    call (including a validation refusal); this dispatch applies its damage and
    drives the downed seam.
    """
    spell_id = getattr(sel, "spell_id", None)
    if not spell_id:
        _watcher_publish(
            "wwn.cast_spell_no_spell_id",
            {"actor": actor.name, "beat_id": "cast_spell"},
            component="magic",
            severity="warning",
        )
        return

    # Genre/world boundary correction (epic 94, supersedes ADR-120
    # "mechanics-in-genre"): the spell catalog is a world-tier CAST/CATALOG
    # surface. Resolve it world-first from the bound world, falling through to the
    # genre-tier catalog only when the world ships none. The resolver emits a
    # state_transition (op=resolved, tier=world|genre) span so the GM panel can
    # prove the cast read the catalog from the world tier, not improvised it.
    from sidequest.server.dispatch.wwn_spell_catalog_resolve import resolve_wwn_spell_catalog

    catalog = resolve_wwn_spell_catalog(pack, snapshot.world_slug)
    if catalog is None:
        _watcher_publish(
            "wwn.cast_spell_no_catalog",
            {"actor": actor.name, "spell_id": spell_id},
            component="magic",
            severity="warning",
        )
        return
    try:
        spell = catalog.get(spell_id)
    except KeyError:
        _watcher_publish(
            "wwn.cast_spell_unknown",
            {
                "actor": actor.name,
                "spell_id": spell_id,
                "available_ids": [s.id for s in catalog.spells],
            },
            component="magic",
            severity="warning",
        )
        return

    caster_core = snapshot.find_creature_core(actor.name)
    if caster_core is None:
        _watcher_publish(
            "wwn.cast_spell_no_caster_core",
            {"actor": actor.name, "spell_id": spell_id},
            component="magic",
            severity="warning",
        )
        return

    # Defender resolution (mirror the strike path): the first live actor on the
    # side OPPOSITE the caster. resolve_spellcast still spends the cast with
    # save_made=None when there is no defender (per its docstring), so a missing
    # defender is NOT a guard-refusal here — it is a valid no-target cast.
    from sidequest.game.beat_kinds import _opposite_side_first_actor
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.ruleset.wwn import WwnRulesetModule
    from sidequest.server.dispatch.downed_seam import run_cwn_wwn_downed_seam

    down_name = _opposite_side_first_actor(encounter, actor.side)
    target_core = snapshot.find_creature_core(down_name) if down_name is not None else None

    module = get_ruleset_module("wwn")
    assert isinstance(module, WwnRulesetModule)
    cfg = pack.rules.ruleset_config()

    # Defender ability scores — sourced the SAME way the downed seam resolves
    # them (PC stats block / confrontation opponent scores). resolve_spellcast
    # rolls the defender save itself; we only supply the stat dict.
    target_stats: dict[str, int] | None = None
    if target_core is not None and spell.save is not None:
        pc = next((c for c in snapshot.characters if c.core.name == down_name), None)
        target_stats = dict(pc.stats) if pc is not None else cdef.opponent_ability_scores()

    result = module.resolve_spellcast(
        caster_core=caster_core,
        spell=spell.to_cast_input(),
        target_core=target_core if target_stats is not None else None,
        target_stats=target_stats,
        cfg=cfg,
        rng=random,
    )

    # Apply rolled spell damage to the defender's HP channel the SAME way the
    # strike path does (apply_beat_hp_channel emits the state_patch.hp span),
    # then run the shared CWN/WWN downed seam if the defender hit 0 HP.
    if result.cast and result.damage > 0 and target_core is not None:
        from sidequest.game.beat_kinds import apply_beat_hp_channel
        from sidequest.game.hp_depletion import check_hp_depletion

        apply_beat_hp_channel(
            target=target_core,
            channel="strike",
            damage_total=result.damage,
            target_mitigation=0,
            source_beat_id=f"cast_spell:{spell_id}",
        )
        run_cwn_wwn_downed_seam(
            ruleset=module,
            snapshot=snapshot,
            encounter=encounter,
            cdef=cdef,
            pack=pack,
            actor_side=actor.side,
            rng=random,
        )
        # BUG 2a (eh-opp-damage): FIRE THE WIN CONDITION. The strike path resolves
        # hp_depletion inside ``apply_beat``; the cast path bypasses that branch
        # (cast_spell carries damage_channel=none, so ``apply_beat`` runs with no
        # damage_resolver and the defender still has full HP when its
        # check_hp_depletion runs — the spell damage lands HERE, afterward).
        # Without this call the opponent could be dropped to 0 HP by a spell and
        # the encounter would never resolve (resolved=False / active=True forever,
        # the same beats re-offered) — combat could not be WON by a caster.
        # ``check_hp_depletion`` is unconditional + idempotent (no-op above 0 HP
        # and when already resolved) and emits encounter.resolved with
        # source="hp_depletion" — the same win-condition span + GM-panel
        # lie-detector the strike/reprisal/dogfight paths use.
        check_hp_depletion(
            encounter,
            snapshot.find_creature_core,
            beat_id=f"cast_spell:{spell_id}",
        )


def _resolve_mutation_for_beat(
    *,
    sel,
    actor,
    snapshot,
    pack,
) -> None:
    """Story 102-7 — drive ``use_mutation`` for a ``mutation_resolution`` beat.

    The AWN Plan 2 §6.3 wiring: a beat carrying the marker routes through the
    mutation engine (ownership, usage limits, Strain cost, save-vs) via the
    ``BeatSelection.mutation_id`` sidecar — the exact ``cast_spell``/
    ``spell_id`` pattern. Mirrors ``_resolve_wwn_cast_for_beat``'s guard
    idiom: every miss is LOUD (an ``awn.mutation.refused`` span the GM panel
    can see), never a silent fall-through to bare narration.
    """
    from sidequest.game.ruleset.awn import AwnRulesetModule
    from sidequest.game.ruleset.registry import get_ruleset_module
    from sidequest.mutation.use_ops import use_mutation
    from sidequest.telemetry.spans.awn import awn_mutation_refused_span

    mutation_id = getattr(sel, "mutation_id", None)
    if not mutation_id:
        # The pre-wiring bug shape: a mutation beat with no mutation named.
        # Mirror of magic.cast_spell_no_spell_id — loud, inert.
        awn_mutation_refused_span(actor=actor.name, mutation_id="", reason="beat_no_mutation_id")
        return
    catalog = getattr(pack, "mutations", None)
    state = snapshot.mutation_state
    if catalog is None or state is None:
        awn_mutation_refused_span(
            actor=actor.name, mutation_id=mutation_id, reason="no_mutation_surface"
        )
        return
    rules = getattr(pack, "rules", None)
    module = get_ruleset_module(rules.ruleset) if rules is not None else None
    if not isinstance(module, AwnRulesetModule):
        awn_mutation_refused_span(
            actor=actor.name, mutation_id=mutation_id, reason="non_awn_ruleset"
        )
        return
    core = snapshot.find_creature_core(actor.name)
    if core is None:
        awn_mutation_refused_span(actor=actor.name, mutation_id=mutation_id, reason="no_actor_core")
        return
    try:
        catalog.positive_by_id(mutation_id)
    except KeyError:
        awn_mutation_refused_span(
            actor=actor.name, mutation_id=mutation_id, reason="unknown_mutation"
        )
        return

    # v1 save handling matches the use_mutation tool: the narrator narrates
    # the target's save from the returned save_stat; opposed-save dice wiring
    # rides the dice protocol in a later plan. "fail" applies the full effect.
    use_mutation(
        state=state,
        catalog=catalog,
        module=module,
        cfg=rules.ruleset_config(),
        core=core,
        actor=actor.name,
        mutation_id=mutation_id,
        target_id=getattr(sel, "target", None) or "",
        save_resolver=lambda stat, target: "fail",
    )


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
    extraction. Table_resolution (poker/auction) is the analogous case: each
    seat's per-decision-point commit is the player's explicit consent frame
    (a classified "I raise"/"I fold"), not a beat the narrator inferred from
    free prose. Excluding both modes from the gate keeps their production
    paths working — without the exemption the gate's _filter_inferred_pc_beats
    would silently DROP every PC table commit on the live narrator path
    (from_explicit_action=False) — while still locking the legacy beat-loop
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
    return cdef.resolution_mode not in (
        ResolutionMode.sealed_letter_lookup,
        ResolutionMode.table_resolution,
        # spec 2026-06-17 §2 (Westley major M1): a Fate Contest resolves ONLY via
        # FATE_ACTION (the 4dF exchange engine), never the legacy apply_beat dial.
        # Excluding it here keeps _filter_inferred_pc_beats from touching contest
        # selections; the contest branch below short-circuits them before the dial
        # engine can run (REPLACE, not layer — ADR-144).
        ResolutionMode.contest,
    )


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
    classified_intent: str = "unspecified"
    pending_dogfight_shot: PendingDogfightShot | None = None
    # Free-for-all N-seat table (table_resolution): set when the table resolved
    # to showdown. Carries recipient, stake_kind, stake_descriptor so callers
    # can surface the award in the UI. Money stakes are applied to snapshot
    # (gold mutation) directly inside the branch — see narration_apply.py Task 12.
    # Non-money stakes (item/favor/information) are recorded here for narrator
    # context but the inventory transfer is deferred to content-driven handling.
    table_pot_award: dict | None = None


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


def _synthetic_creature_dict(name: str) -> dict:
    """Generate a minimal encountergen-shaped dict for a narrator-invented
    creature that has no pre-fetched Monster Manual entry.

    ``creature_id`` is a deterministic slug of the name so that story 83-3
    (recurring-threat reconciliation) can key on it across separate promotion
    calls for the same species.  All other fields are conservative defaults
    appropriate for a tier-2 wild creature.
    """
    creature_id = name.lower().replace(" ", "_").replace("'", "").replace("-", "_")
    return {
        "name": name,
        "creature_id": creature_id,
        "threat_level": 2,
        "hp": 16,
        "abilities": [f"{name} — natural attack"],
        "morale": "steady",
        "role": "wild creature",
    }


def _promote_creature_to_npc(member: NpcPoolMember) -> Npc:
    """Promote a creature-classified pool member to an ``Npc`` with Monster
    Manual bestiary identity (ADR-059, story 83-1).

    Uses ``member.creature_data`` when available (pre-fetched from MM at
    pool-mint time); otherwise synthesises a minimal stat block from the
    pool member's name.  In both cases the resulting ``Npc`` carries
    ``creature_id``, ``threat_level``, ``abilities``, ``morale``, and HP
    drawn from the bestiary — never the 10/10 person placeholder.

    Emits two OTEL spans:
    - ``npc.spawn_disposition`` — existing lie-detector for materialisation.
    - ``npc.creature_bestiary_draw`` — story 83-1 lie-detector proving the
      engine drew a bestiary identity instead of improvising a placeholder.
    """
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.server.dispatch.monster_manual_inject import _creature_patch_from_enemy

    enemy = (
        member.creature_data
        if member.creature_data is not None
        else _synthetic_creature_dict(member.name)
    )
    patch = _creature_patch_from_enemy(enemy, tier=2, location=None)
    if patch is None:
        # No Silent Fallbacks: this should never happen with a well-formed dict.
        raise ValueError(
            f"_creature_patch_from_enemy returned None for creature {member.name!r}; "
            "cannot promote creature pool member without a valid patch"
        )

    hp_val = patch.hp if patch.hp is not None else 16
    core = CreatureCore(
        name=member.name,
        description=member.appearance or patch.description or "wild creature",
        personality=patch.role or "aggressive",
        level=patch.threat_level or 2,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=hp_val, max=hp_val, base_max=hp_val),
    )
    npc = Npc(
        core=core,
        pronouns=None,
        appearance=member.appearance,
        pool_origin=member.name,
        disposition=member.disposition,
        creature_id=patch.creature_id,
        threat_level=patch.threat_level,
        abilities=list(patch.abilities) if patch.abilities else [],
        morale=patch.morale,
    )

    # Story 72-5: spawn-disposition lie-detector.
    provenance = "default_neutral" if int(member.disposition) == 0 else "carried_from_pool"
    with npc_spawn_disposition_span(
        npc_name=npc.core.name,
        disposition=int(npc.disposition),
        provenance=provenance,
        is_creature=True,
        pool_origin=member.name,
    ):
        pass

    # Story 83-1: bestiary-draw lie-detector — proves the engine produced a
    # real creature identity rather than the person-shaped placeholder.
    # source="mm" when the creature matched a real Monster Manual entry
    # (pre-fetched via find_enemy_by_name at pool-mint time); source="synthesized"
    # when the narrator invented a creature with no MM entry (slug-derived id).
    draw_source = "mm" if member.creature_data is not None else "synthesized"
    with npc_creature_bestiary_draw_span(
        npc_name=npc.core.name,
        creature_id=patch.creature_id or "",
        threat_level=patch.threat_level or 2,
        hp=hp_val,
        source=draw_source,
    ):
        pass

    return npc


def _promote_pool_member_to_npc(member: NpcPoolMember) -> Npc:
    """Build an ``Npc`` from an ``NpcPoolMember``, preserving identity
    (name, pronouns, appearance, role) and recording ``pool_origin`` so
    the mechanical-visibility lens can trace the NPC back to the pool entry
    it was promoted from.

    Creature-classified members (``is_creature=True``) are routed to
    ``_promote_creature_to_npc`` which draws a Monster Manual bestiary
    identity (ADR-059, story 83-1) instead of the person-shaped placeholder.
    Person members receive the existing placeholder stat block (HP 10/10,
    level 1) and OCEAN seeding via ``_seed_invented_npc_identity``.
    """
    if member.is_creature:
        return _promote_creature_to_npc(member)

    from sidequest.game.creature_core import (
        CreatureCore,
        HpPool,
        Inventory,
    )

    core = CreatureCore(
        name=member.name,
        description=member.appearance or "No description",
        personality=member.role or "Unknown",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    npc = Npc(
        core=core,
        pronouns=member.pronouns,
        appearance=member.appearance,
        pool_origin=member.name,
        # sq-playtest 2026-06-07 double-mint: the original→mint binding must
        # survive promotion or the narrator's next "Varra" misses the Npc and
        # re-mints at the pool tier.
        invented_from=member.invented_from,
        # Story 72-2: carry the scaffold's recorded disposition through
        # promotion. A bartender the table befriended (e.g. +18) promotes
        # friendly instead of silently flattening to neutral. Narrator-
        # invented / legacy members default neutral-0, preserving 72-5.
        disposition=member.disposition,
    )
    # Story 72-5: emit the spawn-disposition span so the GM panel can
    # confirm what disposition a promoted NPC spawned with and why. When
    # the scaffold carried no relationship (default neutral-0) the NPC is
    # born neutral (``default_neutral``); when 72-2 carries a known value
    # through, provenance is ``carried_from_pool`` so the lie-detector
    # dial reflects the truth rather than reporting a false default.
    provenance = "default_neutral" if int(member.disposition) == 0 else "carried_from_pool"
    with npc_spawn_disposition_span(
        npc_name=npc.core.name,
        disposition=int(npc.disposition),
        provenance=provenance,
        is_creature=False,
        pool_origin=member.name,
    ):
        pass
    return npc


def _promote_engaged_pool_member(
    *,
    snapshot: GameSnapshot,
    member: NpcPoolMember,
    turn_num: int,
    trigger: str,
    actor_loc: str | None,
) -> Npc:
    """Story 97-1: promote an ENGAGED pool member to a full ``Npc``.

    Wraps ``_promote_pool_member_to_npc`` (which carries identity,
    ``invented_from``, and disposition) and additionally carries the
    97-1 engagement state: interaction count, resolution tier, and the
    last-seen stamps. For the ``tier`` trigger the ADR-128 acquaintance
    milestone fires here — disposition warms by the milestone drift and
    the named milestone beat is recorded (mirroring
    ``develop_npc_on_engagement``'s escalation leg). The ``valence_beat``
    trigger leaves disposition/beat authoring to the caller (the
    ``update_npc_disposition`` handler records the narrator's own beat).

    Per the 97-1 design spec ("one identity, one source") the pool entry
    is REMOVED — unlike the legacy mechanical-promotion path which
    shadows it. Deviation tracked in the 97-1 session file.

    Emits ``npc.promoted_from_pool`` (trigger=tier|valence_beat) — the
    AC-3 lie-detector for the promotion decision.
    """
    npc = _promote_pool_member_to_npc(member)
    npc.non_transactional_interactions = member.non_transactional_interactions
    npc.resolution_tier = tier_for_interactions(member.non_transactional_interactions)
    npc.last_seen_turn = turn_num
    npc.last_seen_location = actor_loc or member.last_seen_location

    if trigger == "tier":
        npc.disposition = Disposition(int(npc.disposition) + DISPOSITION_DRIFT_PER_MILESTONE)
        npc.record_disposition_beat(
            turn=turn_num,
            delta=DISPOSITION_DRIFT_PER_MILESTONE,
            reason=engagement_beat_reason(npc.resolution_tier),
            location=npc.last_seen_location,
        )

    snapshot.npcs.append(npc)
    snapshot.npc_pool.remove(member)

    with Span.open(
        "npc.promoted_from_pool",
        {
            "npc_name": npc.core.name,
            "trigger": trigger,
            "interactions": npc.non_transactional_interactions,
            "tier": npc.resolution_tier,
            "turn_number": turn_num,
        },
    ):
        pass
    logger.info(
        "npc.promoted_from_pool name=%r trigger=%s interactions=%d tier=%s turn=%d",
        npc.core.name,
        trigger,
        npc.non_transactional_interactions,
        npc.resolution_tier,
        turn_num,
    )
    return npc


def _seed_invented_npc_identity(
    *,
    npc: Npc,
    member: NpcPoolMember,
    snapshot: GameSnapshot,
    turn_num: int,
) -> None:
    """Story 72-9: seed the three identity surfaces onto a *narrator-invented*
    NPC at the moment it first becomes mechanical.

    A human DM who invents a stranger gives them a personality, an attitude
    toward the party, and — in a mystery — a stake in the plot. The engine
    didn't: an invented person promoted to ``Npc`` carried ``ocean=None`` and
    was invisible to the scenario ``belief_state`` graph. This seeds:

    - **OCEAN** (ADR-042): a flat-baseline ``OceanProfile`` (all 5.0). There is
      no random/jitter generator in the codebase (ocean.py docstring), so a
      deterministic baseline is the honest seed — a real profile, never an
      empty ``{}`` stub.
    - **Disposition** (ADR-020): already neutral via ``_promote_pool_member_to_npc``
      (72-2/72-5); not re-touched here.
    - **Scenario ``belief_state``** (ADR-053): when a scenario is active, the NPC
      is registered into ``scenario_state.npc_roles`` as ``innocent`` — a
      mid-session walk-on is never the pre-selected ``guilty_npc`` — and its
      ``BeliefState`` (already live on the ``Npc``) becomes the gossip/questioning
      mutation surface. Mirrors ``bind_scenario``'s authored-NPC seeding.

    Fires only for the ``drawn_from="narrator_invented"`` lineage; authored / MM
    NPCs get their identity through ``world_materialization`` / ``_npc_from_patch``
    and must not be double-wired. Skips an NPC that already holds an OCEAN
    profile so a re-touch never clobbers learned identity.
    """
    if member.drawn_from != "narrator_invented":
        return
    if member.is_creature:
        # Story 83-1: creatures have stat blocks, not Big-Five personalities.
        # A Forest Lion should never receive an OCEAN profile — skip seeding.
        return
    if npc.ocean:
        # Already seeded — never re-seed (would clobber learned identity).
        return

    from sidequest.game.scenario_state import ScenarioRole
    from sidequest.genre.models.ocean import OceanProfile

    npc.ocean = OceanProfile().model_dump()

    scenario_registered = False
    scenario_role = ""
    scenario_state = snapshot.scenario_state
    if scenario_state is not None:
        name = npc.core.name
        if name not in scenario_state.npc_roles:
            scenario_state.npc_roles[name] = ScenarioRole.Innocent
        scenario_role = scenario_state.npc_roles[name]
        scenario_registered = True

    with npc_identity_seeded_span(
        npc_name=npc.core.name,
        ocean_seeded=True,
        disposition=int(npc.disposition),
        scenario_registered=scenario_registered,
        scenario_role=scenario_role,
    ):
        logger.info(
            "npc.identity_seeded name=%r ocean_seeded=True disposition=%d "
            "scenario_registered=%s role=%r turn=%d",
            npc.core.name,
            int(npc.disposition),
            scenario_registered,
            scenario_role,
            turn_num,
        )


def resolve_status_target(
    snapshot: GameSnapshot,
    *,
    actor_name: str,
    turn_num: int,
    trigger: str,
    narration_text: str | None = None,
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
    # Story 72-9: seed OCEAN + scenario belief_state onto narrator-invented
    # NPCs at the promotion seam (where ``snapshot`` is in scope). No-op for
    # authored / MM lineages and for already-seeded NPCs.
    _seed_invented_npc_identity(
        npc=promoted,
        member=pool_match,
        snapshot=snapshot,
        turn_num=turn_num,
    )
    snapshot.npcs.append(promoted)
    _watcher_publish(
        "state_transition",
        {
            "field": "npcs",
            "op": "promoted_from_pool",
            "name": pool_match.name,
            "pool_origin": pool_match.name,
            "drawn_from": pool_match.drawn_from,
            # Story 72-2: carry the preserved disposition (and its attitude
            # band) so the GM panel can verify the relationship survived
            # promotion rather than resetting to neutral.
            "disposition": int(promoted.disposition),
            "attitude": promoted.disposition.attitude().value,
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
    # Story 84-2 (WI-5, ADR-118 §A4): accrete any appositive epithets the promotion
    # turn's narration attached to this NPC ("Borin, the old smith") into
    # ``promoted.aliases`` so a later player reference by epithet resolves to it.
    # Conservative extraction (alias correctness is load-bearing); the accreter is
    # idempotent and emits ``entity.alias_accreted`` only on a real accretion.
    if narration_text:
        epithets = extract_epithets_for_npc(narration_text, promoted.core.name)
        if epithets:
            accrete_npc_aliases(promoted, epithets, turn=turn_num)
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


# Story 72-4: bound on the namegen re-roll loop for a narrator-invented NPC.
# Mirrors the ``namegen`` CLI's 10-attempt budget; the corpus produces ample
# variety so a clean candidate is found in 1-2 draws in practice — the budget
# only guards against a pathological corpus that keeps tripping the
# stem-collision / existing-name filters.
_INVENTED_NAME_MAX_ATTEMPTS = 10


def _generate_invented_name(
    *,
    name_generator: NameGenerator,
    snapshot: GameSnapshot,
    fallback: str,
) -> tuple[str, bool]:
    """Generate a culture-true name for a narrator-invented NPC (Story 72-4).

    Draws from the culture-bound ``NameGenerator`` and rejects a candidate
    that either trips ``has_stem_collision`` (the "Frandrew Andrew" artifact)
    or collides case-folded with an existing PC / ``Npc`` / pool-member name —
    re-rolling until a clean candidate is found. The existing-name reject is
    AC5(c): a generated name equal to an existing store member must never mint
    a duplicate identity.

    Returns ``(name, collision_reroll)`` where ``collision_reroll`` is True
    when at least one candidate was rejected before a clean one was drawn. On
    exhaustion within ``_INVENTED_NAME_MAX_ATTEMPTS`` the last candidate (or
    ``fallback`` if the generator yielded nothing) is returned so the turn
    keeps moving.
    """
    existing = {
        c.core.name.casefold()
        for c in snapshot.characters
        if getattr(getattr(c, "core", None), "name", None)
    }
    existing |= {npc.core.name.casefold() for npc in snapshot.npcs}
    existing |= {member.name.casefold() for member in snapshot.npc_pool}

    collision_reroll = False
    candidate = fallback
    for _ in range(_INVENTED_NAME_MAX_ATTEMPTS):
        candidate = name_generator.generate_person()
        if has_stem_collision(candidate) or candidate.casefold() in existing:
            collision_reroll = True
            continue
        return candidate, collision_reroll
    return candidate, collision_reroll


def _culture_mention_matches(culture: Any, mention_name: str) -> str | None:
    """Return culture.name if mention_name identifies this culture; else None.

    Story 83-2 (ADR-091 self-match). Checks in order:
    1. Exact word-boundary name match (``_phrase_matches`` discipline).
    2. Engine-side plural heuristic: culture name + "s" (e.g. "Munchkin"~"Munchkins").
    3. Any authored alias in ``culture.aliases`` (content-controlled demonyms).

    Reuses the word-boundary discipline from :mod:`sidequest.game.alias_resolution`
    (ADR-118) so name-match and alias-match never drift apart (CLAUDE.md "Don't
    Reinvent"). The plural heuristic covers the common English case; content authors
    can cover irregular forms via ``culture.aliases``.
    """
    if not mention_name:
        return None
    if _phrase_matches(culture.name, mention_name):
        return culture.name
    if _phrase_matches(culture.name + "s", mention_name):
        return culture.name
    for alias in getattr(culture, "aliases", None) or []:
        if _phrase_matches(alias, mention_name):
            return culture.name
    return None


def _resolve_invented_naming_context(
    pack: GenrePack | None, world: str | None, mention_name: str | None = None
) -> tuple[NameGenerator | None, str | None, str | None, bool]:
    """Resolve the culture-bound naming context for narrator-invented NPCs.

    Story 72-4. Returns
    ``(name_generator, culture_name, culture_source, naming_unresolved)``.

    Culture is resolved via ``Pack.effective_cultures(world)`` — NOT raw
    ``pack.cultures``; reading the genre set while the world binds its own is
    the perseus_cloud session-894 divergence (0 NPCs seeded). The corpus dir
    mirrors the ``namegen`` CLI: ``<pack.source_dir>/corpus`` plus the shared
    ``sidequest-content/corpus/shared`` fallback.

    - No pack in context → all-None/False: the legacy context-free path (no
      routing, no loud span).
    - Pack present but the world resolves no culture, or the generator cannot
      be built → ``naming_unresolved=True`` so the mint seam fails loud (No
      Silent Fallbacks) and deliberately degrades to the raw narrator string.

    Story 83-2: when ``mention_name`` is supplied and matches a bound culture by
    name, plural form, or authored alias (see :func:`_culture_mention_matches`),
    that culture is tried FIRST — deterministic self-match before the shuffle.
    If the matched culture's corpus fails to build the function returns
    ``naming_unresolved=True`` immediately (No-Silent-Fallbacks: never silently
    continue to a different culture for a named people-group). Only when no
    culture matches the mention does the code fall into the shuffle loop.
    """
    if pack is None:
        return None, None, None, False

    cultures, culture_source = pack.effective_cultures(world)
    if not cultures or pack.source_dir is None:
        return None, None, None, True

    corpus_dir = pack.source_dir / "corpus"
    fallback_dirs = [pack.source_dir.parent.parent / "corpus" / "shared"]

    # Story 83-2: self-match — try to find the culture the narrator named before
    # falling back to the shuffle. The first culture whose name, plural, or alias
    # matches mention_name wins. On a corpus failure for the MATCHED culture we
    # degrade loud (No-Silent-Fallbacks) rather than silently retrying a different
    # one — the mention was specific and wrong-culture names would be a lie.
    if mention_name:
        for culture in cultures:
            if _culture_mention_matches(culture, mention_name) is not None:
                try:
                    generator = _namegen_module.build_from_culture(
                        culture, corpus_dir, fallback_dirs=fallback_dirs
                    )
                    return generator, culture.name, culture_source, False
                except (FileNotFoundError, ValueError):
                    logger.warning(
                        "namegen.self_match_corpus_failed culture=%r world=%r "
                        "mention=%r — matched culture corpus unavailable or below "
                        "floor; loud degrade (No-Silent-Fallbacks: will not shuffle "
                        "to a different culture for a named people-group)",
                        culture.name,
                        world,
                        mention_name,
                    )
                    return None, None, None, True

    # No self-match (unaffiliated stranger or no mention_name supplied): try each
    # bound culture (shuffled, so invented NPCs vary across a multi-culture world)
    # and use the first whose corpus actually builds. A culture with a missing or
    # below-floor corpus raises inside ``build_from_culture`` — which already
    # emitted ``namegen.fail_loud`` / ``namegen.thin_corpus`` — so skip it and try
    # the next rather than failing the whole route on one thin culture
    # (perseus_cloud's provisional Yulan corpus is exactly this case). Only when NO
    # bound culture can build do we surface the loud-degrade condition.
    candidates = list(cultures)
    random.shuffle(candidates)
    for culture in candidates:
        try:
            generator = _namegen_module.build_from_culture(
                culture, corpus_dir, fallback_dirs=fallback_dirs
            )
        except (FileNotFoundError, ValueError):
            logger.warning(
                "namegen.invented_build_failed culture=%r world=%r — corpus "
                "unavailable or below floor; trying next bound culture",
                culture.name,
                world,
            )
            continue
        return generator, culture.name, culture_source, False

    logger.warning(
        "namegen.invented_all_cultures_failed world=%r cultures=%s — no bound "
        "culture could be built; invented names degrade loud",
        world,
        [c.name for c in cultures],
    )
    return None, None, None, True


def _comma_flip_name(name: str) -> str | None:
    """Flip a single ``"Surname, Given"`` dossier-register name into natural
    ``"Given Surname"`` order. Returns ``None`` when there isn't exactly one
    comma with non-empty parts on both sides — the transform is deterministic
    and reversible, so it only fires on the unambiguous inversion register
    (e.g. the ``space_opera/coyote_star`` Hegemonic
    ``"{family_name}, {given_name}"`` pattern). Anything else is left alone to
    avoid false-positive identity merges.
    """
    parts = name.split(",")
    if len(parts) != 2:
        return None
    surname, given = parts[0].strip(), parts[1].strip()
    if not surname or not given:
        return None
    return f"{given} {surname}"


def _npc_name_match_keys(name: str) -> set[str]:
    """Casefolded match keys for an NPC name: the name itself plus its
    comma-flipped variant (when one exists). Two names reconcile to the same
    identity when their key sets intersect — so ``"Gilligan, Denis"`` matches a
    natural-order mention ``"Denis Gilligan"`` instead of minting a phantom
    duplicate (playtest 2026-06-01 ADR-072 split-identity repro).
    """
    keys = {name.casefold()}
    flipped = _comma_flip_name(name)
    if flipped is not None:
        keys.add(flipped.casefold())
    return keys


# Story 83-3: ongoing-threat reconciliation. Tokens too generic to identify a
# specific creature — excluded so "a hulking shadow" and "the lurking thing"
# don't false-match on filler. Kept small and conservative.
_RECONCILE_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "with",
        "and",
        "that",
        "is",
        "its",
        "his",
        "her",
        "their",
        "in",
        "on",
        "to",
        "at",
        "it",
        "this",
        "these",
        "those",
        "some",
    }
)


def _creature_tokens(*parts: str | None) -> set[str]:
    """Meaningful lowercase tokens from a creature's descriptors (name + role +
    appearance), stripped of punctuation and filler stopwords."""
    toks: set[str] = set()
    for part in parts:
        if not part:
            continue
        for raw in part.lower().replace(",", " ").replace("-", " ").split():
            tok = raw.strip(".'\"!?;:()")
            if tok and tok not in _RECONCILE_STOPWORDS:
                toks.add(tok)
    return toks


def _creature_similarity(
    mention: Any, *, name: str, role: str | None, appearance: str | None
) -> int:
    """Shared-token count between an incoming creature mention and an existing
    creature identity. Higher = more likely the same threat. Used only to
    disambiguate WHICH existing creature a re-description belongs to when more
    than one is active in the scene (the single-creature case uses the
    scene-guard lever and needs no scoring)."""
    incoming = _creature_tokens(mention.name, mention.role, mention.appearance)
    existing = _creature_tokens(name, role, appearance)
    return len(incoming & existing)


def _reconcile_ongoing_threat(
    *, snapshot: GameSnapshot, mention: Any
) -> tuple[Npc | None, NpcPoolMember | None, str] | None:
    """Resolve a continuity-flagged creature mention to an EXISTING creature
    identity instead of minting a Step-3 duplicate (story 83-3).

    Returns ``(npc, None, signal)`` for an authored/roster ``Npc`` target,
    ``(None, member, signal)`` for a prior pool member, or ``None`` when nothing
    safe to reconcile to exists (→ caller mints as before).

    Levers (story 83-3 / AC-3 ``signal``):
      * ``scene_guard``  — exactly one active creature in scene; the re-description
                           is that one threat (the "one active unnamed threat"
                           lever). No similarity needed.
      * ``similarity``   — several creatures active; pick the one whose role/
                           appearance/name tokens overlap the mention, and only
                           when there is a clear (>0) overlap. With no overlap we
                           decline and mint — conservative, never a false merge.

    Creature-ness: a roster ``Npc`` is a creature when it carries a
    ``creature_id`` (ADR-059); a pool member when ``is_creature`` is set.
    """
    npc_candidates = [n for n in snapshot.npcs if n.creature_id is not None]
    member_candidates = [m for m in snapshot.npc_pool if m.is_creature]
    total = len(npc_candidates) + len(member_candidates)
    if total == 0:
        return None
    if total == 1:
        # Scene-guard: one active threat — the re-description is it.
        if npc_candidates:
            return (npc_candidates[0], None, "scene_guard")
        return (None, member_candidates[0], "scene_guard")

    # Several active creatures — disambiguate by token similarity, reconcile only
    # on a clear best match (>0 shared tokens). Ties / zero overlap → mint.
    best: tuple[Npc | None, NpcPoolMember | None, str] | None = None
    best_score = 0
    for npc in npc_candidates:
        score = _creature_similarity(
            mention, name=npc.core.name, role=None, appearance=npc.appearance
        )
        if score > best_score:
            best_score, best = score, (npc, None, "similarity")
    for member in member_candidates:
        score = _creature_similarity(
            mention, name=member.name, role=member.role, appearance=member.appearance
        )
        if score > best_score:
            best_score, best = score, (None, member, "similarity")
    return best


_EPITHET_ARTICLES: tuple[str, ...] = ("the ", "a ", "an ")


def _is_descriptive_epithet(name: str) -> bool:
    """True when a person mention's name is a descriptive epithet, not a name.

    sq-playtest 2026-06-07 (five_points-2 epithet phantom): "The Heavy Man in
    Broadcloth" is a DESCRIPTION the narrator used for an already-identified
    roster NPC, yet it reached the Step-3 novel branch and was routed through
    the culture-bound person namer — minting phantom "Deacon Rutherford Lacy".
    The deterministic shape signal: real personal names are never article-led;
    article-led mentions ("The Hooded Stranger", "A Man with No Name") are
    descriptors by construction.
    """
    return name.casefold().lstrip().startswith(_EPITHET_ARTICLES)


def _epithet_names_a_culture(name: str, pack: GenrePack | None, world: str | None) -> bool:
    """Story 83-2 precedence: an article-led mention that NAMES a culture
    ("The Munchkins") carries real identity signal — it keeps the deterministic
    culture self-match route through the person namer. The epithet guard only
    intercepts descriptors with NO culture signal, which would otherwise draw a
    random culture + a random full name (the phantom shape)."""
    if pack is None:
        return False
    cultures, _ = pack.effective_cultures(world)
    return any(_culture_mention_matches(c, name) is not None for c in cultures)


def _reconcile_epithet_to_person(
    *, snapshot: GameSnapshot, mention: Any
) -> tuple[Npc | None, NpcPoolMember | None, str] | None:
    """Coreference an epithet-shaped person mention against existing person
    identities (epithet-phantom defect b — the described man was an
    already-referenced roster NPC).

    Same shape as ``_reconcile_ongoing_threat`` but for the person tier, and
    STRICTER: requires >=2 shared meaningful tokens with a unique best match
    (the creature guard accepts >0). A false person-merge misattributes
    disposition/relationship ledgers onto the wrong human, which is worse than
    a preserved-verbatim descriptor — so single generic-token overlap ("man")
    and ties decline, and the caller preserves the epithet instead.
    """
    best: tuple[Npc | None, NpcPoolMember | None, str] | None = None
    best_score = 0
    tied = False
    incoming = _creature_tokens(mention.name, mention.role, mention.appearance)
    for npc in snapshot.npcs:
        if npc.creature_id is not None:
            continue
        # ADR-118 §A4 accreted aliases are coreference evidence — the fiction's
        # own epithet binding ("the heavy man") made actionable.
        existing = _creature_tokens(npc.core.name, npc.appearance, *npc.aliases)
        score = len(incoming & existing)
        if score > best_score:
            best_score, best, tied = score, (npc, None, "similarity"), False
        elif score == best_score and score > 0:
            tied = True
    for member in snapshot.npc_pool:
        if member.is_creature:
            continue
        score = _creature_similarity(
            mention, name=member.name, role=member.role, appearance=member.appearance
        )
        if score > best_score:
            best_score, best, tied = score, (None, member, "similarity"), False
        elif score == best_score and score > 0:
            tied = True
    if best_score < 2 or tied:
        return None
    return best


def _engagement_is_hostile_context(snapshot: GameSnapshot, mention: object, npc: object) -> bool:
    """True when a narrator cite of ``npc`` is combat attention, not interest.

    Ping-pong 2026-06-07 ("naive +2-attention model"): the development tick
    must not fire for an NPC the party is actively FIGHTING — engagement-count
    is not valence. Two signals, either suffices:

    - the narrator marked this mention ``side="opponent"``;
    - the NPC is seated opponent-side (non-withdrawn) in the ACTIVE,
      unresolved encounter.

    A resolved encounter releases the gate — a beaten foe can become a
    rival-turned-contact through genuine post-fight engagement.
    """
    if (getattr(mention, "side", "") or "").strip().lower() == "opponent":
        return True
    enc = snapshot.encounter
    if enc is None or enc.resolved:
        return False
    # Story 97-1: the gate serves both entity tiers — ``Npc`` names live at
    # ``.core.name``, ``NpcPoolMember`` names at ``.name``.
    core = getattr(npc, "core", None)
    name_key = (core.name if core is not None else npc.name).casefold()
    return any(
        actor.side == "opponent" and not actor.withdrawn and actor.name.casefold() == name_key
        for actor in enc.actors
    )


def _apply_opponent_disengagements(
    *,
    snapshot: GameSnapshot,
    mentions: list[Any],
    turn_num: int,
) -> None:
    """ADR-116 §4 (social path) — withdraw a seated opponent the narrator marked
    ``disengaged``.

    The narrator signals a departed Other via an ``npcs_present`` mention with
    ``side="opponent"`` and ``disengaged=True`` (sq-playtest 2026-06-10
    long_foundry zombie negotiation: the Other walked back into the smoke but the
    Cold Negotiation stayed active with beats offered and plain Enter locked).
    For each such mention, flip the matching ``side="opponent"`` actor
    ``withdrawn`` so the end-on-no-Other sweep (``_resolve_if_no_opponent_remains``,
    run later this same turn) resolves the encounter — instead of trapping the
    player behind a contested DC Withdraw roll.

    Grounded signal only — never prose inference (No Silent Fallbacks): absence of
    the flag is never read as departure, and a ``disengaged`` flag on a non-
    opponent mention withdraws nothing (the ADR-116 asymmetry — a player
    disengaging is the player-side yield path). Name match reuses the comma-
    inversion-aware ``_npc_name_match_keys`` so a seated actor is found whether the
    mention is natural- or inverted-order. Emits
    ``confrontation.opponent_disengaged`` per withdrawn opponent so the GM panel
    can confirm the ENGINE — not improvisation — ended the scene.
    """
    enc = getattr(snapshot, "encounter", None)
    if enc is None or enc.resolved:
        return
    from sidequest.telemetry.spans import confrontation_opponent_disengaged_span

    for mention in mentions:
        if (getattr(mention, "side", "") or "").strip().lower() != "opponent":
            continue
        if not getattr(mention, "disengaged", False):
            continue
        name_key = mention.name.casefold()
        mention_keys = _npc_name_match_keys(mention.name)
        for actor in enc.actors:
            if actor.side != "opponent" or actor.withdrawn:
                continue
            if actor.name.casefold() == name_key or (
                _npc_name_match_keys(actor.name) & mention_keys
            ):
                actor.withdrawn = True
                with confrontation_opponent_disengaged_span(
                    encounter_type=enc.encounter_type,
                    name=actor.name,
                    turn_number=turn_num,
                ):
                    logger.info(
                        "confrontation.opponent_disengaged name=%r encounter_type=%s turn=%d",
                        actor.name,
                        enc.encounter_type,
                        turn_num,
                    )
                break


def _apply_npc_mentions(
    *,
    snapshot: GameSnapshot,
    mentions: list[Any],
    turn_num: int,
    acting_character_name: str | None = None,
    name_generator: NameGenerator | None = None,
    culture_name: str | None = None,
    culture_source: str | None = None,
    pack: GenrePack | None = None,
    world: str | None = None,
    monster_manual: MonsterManual | None = None,
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

    Story 72-4 — naming context for the Step-3 novel branch (ADR-091). Two
    ways to supply it, both routing a novel narrator name through the
    culture-bound generator instead of minting it verbatim:

    * **Pre-built generator** (the unit-test / direct-injection contract):
      pass ``name_generator`` plus ``culture_name``/``culture_source``. Used
      as-is.
    * **Lazy from pack** (the production path): pass ``pack`` + ``world`` and
      the seam resolves the culture via ``Pack.effective_cultures(world)`` and
      builds the generator **on the first novel mint** — so a turn with no
      invented NPC never pays the corpus-read + Markov-train cost, and a turn
      with several shares one generator.

    ``npc.invented_name_routed`` records the provenance (original vs generated
    name, resolved culture + source, collision-reroll flag). When a pack is in
    context but the active world resolves no culture — or the generator can't be
    built — the route fails loud via ``npc.invented_name_unrouted`` and
    deliberately degrades to the raw string (No Silent Fallbacks; never a silent
    swallow). With neither generator nor pack supplied (the legacy context-free
    call) the branch keeps its original raw-mint behavior and fires no reroute
    span.
    """
    pc_name_lookup = {
        c.core.name.lower(): c.core.name
        for c in snapshot.characters
        if getattr(getattr(c, "core", None), "name", None)
    }

    # Story 72-4: lazy naming-context state. A directly-injected generator is
    # "already resolved"; a pack is resolved on the first novel mint below.
    # ``naming_unresolved`` records the loud-degrade condition (pack present but
    # no culture bound / generator build failed) once resolution is attempted.
    naming_resolved = name_generator is not None
    naming_unresolved = False
    # Story 72-1: the development tick is one engagement *event* per NPC per
    # turn — interest is boolean-per-turn, not per-utterance. A name the
    # narrator cites twice in one turn's mention list develops once.
    developed_this_turn: set[str] = set()

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
        # Comma-inversion-aware match keys (playtest 2026-06-01): reconcile a
        # natural-order mention against a comma-inverted stored name (and vice
        # versa) so an already-rostered NPC is matched, not duplicated.
        mention_keys = _npc_name_match_keys(mention.name)

        # Step 1: existing Npc shadows everything else. Prefer an exact match;
        # only fall back to comma-normalized reconciliation when no exact one
        # exists (so an exact hit later in the roster is never lost to an
        # earlier comma-variant).
        npc_hit: Npc | None = None
        npc_match_form = "exact"
        for npc in snapshot.npcs:
            if npc.core.name.casefold() == name_key:
                npc_hit = npc
                break
        if npc_hit is None:
            for npc in snapshot.npcs:
                if _npc_name_match_keys(npc.core.name) & mention_keys:
                    npc_hit = npc
                    npc_match_form = "comma_normalized"
                    break
        if npc_hit is None:
            # Invented-name alias leg (original→mint binding cache): a pool
            # member minted under a culture name and later PROMOTED carries
            # ``invented_from`` — the narrator may still be calling the NPC by
            # its original invented name (sq-playtest 2026-06-07 double-mint).
            for npc in snapshot.npcs:
                if npc.invented_from is not None and (
                    _npc_name_match_keys(npc.invented_from) & mention_keys
                ):
                    npc_hit = npc
                    npc_match_form = "invented_from"
                    break
        if npc_hit is not None:
            # ``Npc`` has no string ``role`` field (only the archetype-id
            # ``npc_role_id``, which is not narrator-cited prose). Pass
            # ``None`` so the drift detector skips the role check; pronouns
            # drift is still meaningful.
            # Story 72-7: the npcs_hit path stays warn-only (no identity
            # write onto the stateful ``Npc``); record ``applied=False`` so the
            # span reflects observed-not-applied. Authoritative overwrite lives
            # on the pool-hit path below.
            _detect_npc_identity_drift(
                existing_name=npc_hit.core.name,
                existing_role=None,
                existing_pronouns=npc_hit.pronouns,
                mention=mention,
                turn_num=turn_num,
                applied=False,
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
                match_form=npc_match_form,
                matched_name=npc_hit.core.name,
            ):
                logger.info(
                    "npc.referenced name=%r match=npcs_hit form=%s matched=%r "
                    "pool_origin=%r turn=%d",
                    mention.name,
                    npc_match_form,
                    npc_hit.core.name,
                    npc_hit.pool_origin,
                    turn_num,
                )
            # Story 72-1: interest-driven development tick. Rides this
            # ``npcs_hit`` engagement signal (ADR-014 coal->diamond on player
            # interest; ADR-020 disposition evolves through interaction).
            # De-duped per turn so a name cited twice develops once — and
            # (ping-pong 2026-06-07 turn-1 double-write) across apply CALLS
            # via ``last_development_turn``, since two passes in one turn
            # each start a fresh ``developed_this_turn`` set.
            #
            # Hostile-context gate (ping-pong 2026-06-07 "naive +2-attention
            # model"): a cite of an NPC the party is actively FIGHTING —
            # narrator-marked side="opponent", or seated opponent-side in the
            # live encounter — is transactional combat attention, not the
            # non-transactional interest ADR-014 promotes on. Pre-gate, firing
            # on the Thari cutter every round accrued +2/turn and rendered it
            # "Warm ↗" on the Relationships tab. Skip the tick entirely and
            # emit the decision (OTEL lie-detector — the skip must be visible).
            if name_key not in developed_this_turn and npc_hit.last_development_turn != turn_num:
                developed_this_turn.add(name_key)
                if _engagement_is_hostile_context(snapshot, mention, npc_hit):
                    with Span.open(
                        "npc.development_skipped",
                        {
                            "npc_name": npc_hit.core.name,
                            "reason": "hostile_context",
                            "mention_side": mention.side or "",
                            "turn_number": turn_num,
                        },
                    ):
                        pass
                    logger.info(
                        "npc.development_skipped name=%r reason=hostile_context "
                        "mention_side=%r turn=%d",
                        npc_hit.core.name,
                        mention.side,
                        turn_num,
                    )
                    continue
                npc_hit.last_development_turn = turn_num
                tick = develop_npc_on_engagement(npc_hit)
                with npc_developed_span(
                    npc_name=npc_hit.core.name,
                    non_transactional_interactions=tick.interactions,
                    resolution_tier_before=tick.tier_before,
                    resolution_tier_after=tick.tier_after,
                    turn_number=turn_num,
                ):
                    logger.info(
                        "npc.developed name=%r interactions=%d tier=%s->%s turn=%d",
                        npc_hit.core.name,
                        tick.interactions,
                        tick.tier_before,
                        tick.tier_after,
                        turn_num,
                    )
                # Reuse the live ``disposition.shift`` contract (50-11) for the
                # drift leg. Skip when the value didn't actually move (clamped
                # at +-100) so the GM panel never shows a phantom shift.
                if tick.disposition_delta != 0:
                    with Span.open(
                        SPAN_DISPOSITION_SHIFT,
                        {
                            "npc_name": npc_hit.core.name,
                            "delta": tick.disposition_delta,
                            "before": tick.disposition_before,
                            "after": tick.disposition_after,
                            "before_attitude": tick.attitude_before,
                            "after_attitude": tick.attitude_after,
                            "crossed": tick.attitude_crossed,
                        },
                    ):
                        pass
                    # ADR-136: persist the why behind this shift — the rapport
                    # MILESTONE, named (drift only fires on tier escalation).
                    npc_hit.record_disposition_beat(
                        turn=turn_num,
                        delta=tick.disposition_delta,
                        reason=engagement_beat_reason(tick.tier_after),
                        location=actor_loc,
                    )
            continue

        # Step 2: pool member match. Exact first, comma-normalized fallback
        # (same precedence as Step 1), then the invented-name alias (the
        # original→mint binding cache — sq-playtest 2026-06-07 perseus
        # double-mint: the narrator keeps saying "Varra" while the member is
        # stored under its minted name "Rifenna Muse"; without this leg every
        # re-narration of the original falls through to Step 3 and mints a
        # fresh identity).
        pool_hit: NpcPoolMember | None = None
        pool_match_form = "exact"
        for member in snapshot.npc_pool:
            if member.name.casefold() == name_key:
                pool_hit = member
                break
        if pool_hit is None:
            for member in snapshot.npc_pool:
                if _npc_name_match_keys(member.name) & mention_keys:
                    pool_hit = member
                    pool_match_form = "comma_normalized"
                    break
        if pool_hit is None:
            for member in snapshot.npc_pool:
                if member.invented_from is not None and (
                    _npc_name_match_keys(member.invented_from) & mention_keys
                ):
                    pool_hit = member
                    pool_match_form = "invented_from"
                    break
        if pool_hit is not None:
            # Story 72-7: narrator drift is authoritative for narrator-sourced
            # identities — a later mention that disagrees overwrites the
            # canonical pronoun/role (the session-894 Sitä-minutta fix). A
            # *human-authored* member (``drawn_from="world_authored"``) is
            # protected: the narrator must not silently overrule what an author
            # (Jade/Keith) wrote into the world pack ("Yes, And" + No Silent
            # Fallbacks — the disagreement is still span-visible, marked
            # applied=False).
            apply_overwrite = pool_hit.drawn_from != "world_authored"
            _detect_npc_identity_drift(
                existing_name=pool_hit.name,
                existing_role=pool_hit.role,
                existing_pronouns=pool_hit.pronouns,
                mention=mention,
                turn_num=turn_num,
                applied=apply_overwrite,
            )
            # Identity upsert (Story 72-7): fill-empty always; on a disagreeing
            # re-mention, overwrite role/pronouns when the entry is
            # narrator-sourced. ``appearance`` stays additive (fill-empty only)
            # — a re-described look is accretion, not an identity correction.
            if mention.role and (
                not pool_hit.role
                or (
                    apply_overwrite
                    and mention.role.strip().lower() != pool_hit.role.strip().lower()
                )
            ):
                pool_hit.role = mention.role
            if mention.pronouns and (
                not pool_hit.pronouns
                or (
                    apply_overwrite
                    and mention.pronouns.strip().lower() != pool_hit.pronouns.strip().lower()
                )
            ):
                pool_hit.pronouns = mention.pronouns
            if mention.appearance and not pool_hit.appearance:
                pool_hit.appearance = mention.appearance
            with npc_referenced_span(
                npc_name=mention.name,
                match_strategy="pool_hit",
                pool_origin=pool_hit.name,
                turn_number=turn_num,
                match_form=pool_match_form,
            ):
                logger.info(
                    "npc.referenced name=%r match=pool_hit form=%s turn=%d",
                    mention.name,
                    pool_match_form,
                    turn_num,
                )
            # Story 97-1: scene-presence stamp + engagement tick at the pool
            # tier, mirroring the npcs_hit branch above. Presence is stamped
            # unconditionally (the cite happened); the INTEREST tick is
            # deduped per turn (incl. across apply calls — the 97-5
            # double-apply shape) and suppressed under the #742
            # hostile-context gate, so a live combat Other never accrues
            # engagement toward a relationship card or promotion.
            pool_actor_loc = snapshot.party_location(perspective=acting_character_name)
            if pool_actor_loc:
                pool_hit.last_seen_location = pool_actor_loc
            pool_hit.last_seen_turn = turn_num
            if name_key not in developed_this_turn and pool_hit.last_development_turn != turn_num:
                developed_this_turn.add(name_key)
                if _engagement_is_hostile_context(snapshot, mention, pool_hit):
                    with Span.open(
                        "npc.development_skipped",
                        {
                            "npc_name": pool_hit.name,
                            "reason": "hostile_context",
                            "mention_side": mention.side or "",
                            "turn_number": turn_num,
                            "tier": "pool",
                        },
                    ):
                        pass
                    logger.info(
                        "npc.development_skipped name=%r reason=hostile_context "
                        "tier=pool mention_side=%r turn=%d",
                        pool_hit.name,
                        mention.side,
                        turn_num,
                    )
                    continue
                pool_hit.last_development_turn = turn_num
                pool_hit.non_transactional_interactions += 1
                # Tier trigger (97-1 design spec): crossing ``acquaintance``
                # promotes the member to a full Npc — sustained engagement is
                # the ADR-128 milestone, the promotion is the ADR-014
                # coal→diamond commitment.
                if pool_hit.non_transactional_interactions >= ACQUAINTANCE_AT:
                    _promote_engaged_pool_member(
                        snapshot=snapshot,
                        member=pool_hit,
                        turn_num=turn_num,
                        trigger="tier",
                        actor_loc=pool_actor_loc,
                    )
            continue

        # Story 83-3: ongoing-threat reconciliation guard. A creature the narrator
        # re-describes under a fresh descriptor each turn ("a snarling beast" ->
        # "the lurking predator" -> "the shadow that stalks") misses the name-only
        # Steps 1/2 above and would mint a NEW pool member every turn — the
        # #74-deferred bug where one forest threat became three monsters and
        # shadowed the authored Cowardly Lion. When the narrator flags the mention
        # as NOT new (``is_new=False``, the continuity signal), resolve it to an
        # existing creature identity instead of minting.
        #
        # Gate: creature mentions only, continuity-flagged only. A genuinely-new
        # creature (``is_new=True``) always falls through to Step 3, so two
        # distinct threats in one scene stay distinct (no false merge, mirroring
        # the comma-inversion false-positive guard). The match is conservative and
        # always span-visible — never a silent identity collapse.
        if mention.is_creature and not mention.is_new:
            reconciled = _reconcile_ongoing_threat(snapshot=snapshot, mention=mention)
            if reconciled is not None:
                reconciled_npc, reconciled_member, signal = reconciled
                if reconciled_npc is not None:
                    # Mirror the npcs_hit presence stamp (Step 1) — the authored
                    # creature was referenced this turn, just under a new descriptor.
                    actor_loc = snapshot.party_location(perspective=acting_character_name)
                    if actor_loc:
                        reconciled_npc.last_seen_location = actor_loc
                    reconciled_npc.last_seen_turn = turn_num
                    reconciled_to = reconciled_npc.core.name
                    target_store = "npcs"
                else:
                    # Fill-empty upsert onto the surviving pool member: a
                    # re-described look/role accretes; existing values win
                    # (same additive precedent as the Step-2 pool_hit upsert).
                    assert reconciled_member is not None
                    if mention.role and not reconciled_member.role:
                        reconciled_member.role = mention.role
                    if mention.pronouns and not reconciled_member.pronouns:
                        reconciled_member.pronouns = mention.pronouns
                    if mention.appearance and not reconciled_member.appearance:
                        reconciled_member.appearance = mention.appearance
                    reconciled_to = reconciled_member.name
                    target_store = "pool"
                with npc_creature_reconciled_span(
                    incoming=mention.name,
                    reconciled_to=reconciled_to,
                    signal=signal,
                    target_store=target_store,
                    turn_number=turn_num,
                ):
                    logger.info(
                        "npc.creature_reconciled incoming=%r reconciled_to=%r "
                        "signal=%s store=%s turn=%d — recurring threat collapsed, "
                        "no phantom mint",
                        mention.name,
                        reconciled_to,
                        signal,
                        target_store,
                        turn_num,
                    )
                continue

        # Step 3: novel — narrator invented a name not in any store.
        # Story 72-4: route the bare narrator string through the ADR-091
        # culture-bound generator so invented NPCs are genre/culture-true by
        # construction (Steps 1-2 already shadowed every known name, so
        # reaching here means a fresh identity). The minted name — generated,
        # raw-degraded, or legacy-raw — is what every downstream span reports.
        original_name = mention.name
        minted_name = original_name
        creature_data: dict | None = None
        if mention.is_creature:
            # ping-pong #74: a creature (wild animal / beast / monster) belongs
            # to NO culture or faction, so it must NOT be routed through the
            # culture-bound person namer — that path mints a person-name + a
            # random culture ("a lion called Keeper Goldbraid of the Emerald
            # City"). Preserve the narrator's descriptive name verbatim, mark
            # the member creature-typed, and emit the OTEL lie-detector span
            # proving the engine declined the namer.
            # Story 83-1: if a MonsterManual is in context, look up the creature
            # name in the pre-generated encounter pool. On a match, embed the MM
            # enemy dict so the promotion seam receives a real bestiary stat block
            # rather than synthesized identity. Narrator-invented creatures with no
            # MM entry keep creature_data=None; _promote_creature_to_npc synthesizes
            # a deterministic identity from the name (source="synthesized").
            if monster_manual is not None:
                match = monster_manual.find_enemy_by_name(original_name)
                if match is not None:
                    creature_data = match[0]
            with npc_creature_preserved_span(
                npc_name=original_name,
                turn_number=turn_num,
            ):
                logger.info(
                    "npc.creature_preserved name=%r turn=%d mm_matched=%s — creature mention, "
                    "person namer declined (no culture)",
                    original_name,
                    turn_num,
                    creature_data is not None,
                )
        elif _is_descriptive_epithet(original_name) and not _epithet_names_a_culture(
            original_name, pack, world
        ):
            # Epithet guard (sq-playtest 2026-06-07, five_points-2 phantom):
            # a descriptive epithet is not a mintable name. First try
            # coreference — the epithet may RE-DESCRIBE an existing person
            # (defect b: "The Heavy Man in Broadcloth" was roster NPC Isaiah
            # Rynders, already referenced and present); on a clear overlap,
            # collapse onto that identity instead of forking a phantom.
            reconciled = _reconcile_epithet_to_person(snapshot=snapshot, mention=mention)
            if reconciled is not None:
                reconciled_npc, reconciled_member, signal = reconciled
                if reconciled_npc is not None:
                    actor_loc = snapshot.party_location(perspective=acting_character_name)
                    if actor_loc:
                        reconciled_npc.last_seen_location = actor_loc
                    reconciled_npc.last_seen_turn = turn_num
                    reconciled_to = reconciled_npc.core.name
                    target_store = "npcs"
                else:
                    # Fill-empty upsert (story 72-7 additive precedent): the
                    # re-description accretes; existing values win.
                    assert reconciled_member is not None
                    if mention.role and not reconciled_member.role:
                        reconciled_member.role = mention.role
                    if mention.pronouns and not reconciled_member.pronouns:
                        reconciled_member.pronouns = mention.pronouns
                    if mention.appearance and not reconciled_member.appearance:
                        reconciled_member.appearance = mention.appearance
                    reconciled_to = reconciled_member.name
                    target_store = "pool"
                with npc_epithet_reconciled_span(
                    incoming=mention.name,
                    reconciled_to=reconciled_to,
                    signal=signal,
                    target_store=target_store,
                    turn_number=turn_num,
                ):
                    logger.info(
                        "npc.epithet_reconciled incoming=%r reconciled_to=%r "
                        "signal=%s store=%s turn=%d — described figure collapsed "
                        "onto the existing person, no phantom mint",
                        mention.name,
                        reconciled_to,
                        signal,
                        target_store,
                        turn_num,
                    )
                continue
            # No safe coreference target — preserve the epithet VERBATIM
            # (defect a: the namer must decline). An honest descriptor in the
            # registry beats a phantom culture-minted full name with fake
            # provenance; Step-2 exact match re-cites it on later turns.
            with npc_epithet_preserved_span(
                npc_name=original_name,
                turn_number=turn_num,
            ):
                logger.info(
                    "npc.epithet_preserved name=%r turn=%d — descriptive epithet, "
                    "person namer declined (not a mintable name)",
                    original_name,
                    turn_num,
                )
        else:
            # Person: route the bare narrator string through the ADR-091
            # culture-bound generator so invented NPCs are genre/culture-true.
            # Lazy resolution: build the generator from the pack on the first
            # novel *person* mint only (skipped on quiet turns, on creature
            # mentions, and when a generator was injected directly).
            if not naming_resolved and pack is not None:
                (
                    name_generator,
                    culture_name,
                    culture_source,
                    naming_unresolved,
                ) = _resolve_invented_naming_context(pack, world, mention_name=original_name)
                naming_resolved = True
            if name_generator is not None and culture_name is not None:
                minted_name, collision_reroll = _generate_invented_name(
                    name_generator=name_generator,
                    snapshot=snapshot,
                    fallback=original_name,
                )
                # Story 83-2: determine resolution strategy for the OTEL span
                # (lie-detector so the GM panel sees whether culture routing was
                # deterministic self-match or shuffle-based fallback). We check
                # per-mention so the strategy reflects this NPC's mention_name,
                # not the first resolved mention in the turn.
                _resolution_strategy = "shuffle_fallback"
                _matched_token = ""
                # Story 83-2 self-match needs the pack's culture list to detect a
                # deterministic mention→culture match. The pre-built-generator
                # path (docstring: name_generator + culture_name passed directly,
                # no pack) legitimately has pack=None — there is no Pack to resolve
                # cultures from, so we keep the "shuffle_fallback" default rather
                # than crash. Self-match still fires on the lazy path where pack
                # is resolved.
                if original_name and pack is not None:
                    _cultures_for_strategy, _ = pack.effective_cultures(world)
                    _matched_culture = next(
                        (c for c in _cultures_for_strategy if c.name == culture_name), None
                    )
                    if _matched_culture is not None:
                        _tok = _culture_mention_matches(_matched_culture, original_name)
                        if _tok is not None:
                            _resolution_strategy = "self_match"
                            _matched_token = _tok
                with npc_invented_name_routed_span(
                    original_name=original_name,
                    npc_name=minted_name,
                    culture=culture_name,
                    culture_source=culture_source or "",
                    collision_reroll=collision_reroll,
                    turn_number=turn_num,
                    resolution_strategy=_resolution_strategy,
                    matched_token=_matched_token,
                ):
                    logger.info(
                        "npc.invented_name_routed original=%r minted=%r culture=%r "
                        "source=%r reroll=%s strategy=%r turn=%d",
                        original_name,
                        minted_name,
                        culture_name,
                        culture_source,
                        collision_reroll,
                        _resolution_strategy,
                        turn_num,
                    )
            elif naming_unresolved:
                # No Silent Fallbacks: the active world bound no culture, so the
                # route could not run. Fail loud, then deliberately degrade to
                # the raw narrator string — a span-recorded degrade, never a
                # silent swallow (recovery choice per the story's AC4 latitude).
                with npc_invented_name_unrouted_span(
                    original_name=original_name,
                    reason="no_culture_bound",
                    world=world or "",
                    turn_number=turn_num,
                ):
                    logger.warning(
                        "npc.invented_name_unrouted original=%r world=%r "
                        "reason=no_culture_bound turn=%d — no culture bound for "
                        "the active world; degrading to the raw narrator name",
                        original_name,
                        world,
                        turn_num,
                    )

        new_member = NpcPoolMember(
            name=minted_name,
            role=mention.role or None,
            pronouns=mention.pronouns or None,
            appearance=mention.appearance or None,
            archetype_id=None,
            drawn_from="narrator_invented",
            is_creature=mention.is_creature,
            # sq-playtest 2026-06-07 (perseus double-mint): bind the narrator's
            # original to the mint so a re-narration of "Varra" reconciles to
            # this member (Step 1/2 alias matching) instead of re-minting a
            # second identity per turn.
            invented_from=(original_name if minted_name != original_name else None),
            # Story 83-1: embed MM creature data when the name matched a
            # pre-generated bestiary entry so the promotion seam receives a
            # real stat block (source="mm"). None for person members and for
            # narrator-invented creatures with no MM match (source="synthesized").
            creature_data=creature_data if mention.is_creature else None,
        )
        snapshot.npc_pool.append(new_member)
        with npc_referenced_span(
            npc_name=minted_name,
            match_strategy="invented",
            pool_origin=None,
            turn_number=turn_num,
        ):
            logger.info(
                "npc.referenced name=%r match=invented turn=%d",
                minted_name,
                turn_num,
            )
        # Preserve pre-Wave-2A auto-registered telemetry — the
        # ``WatcherSpanProcessor`` re-emits the state_transition event
        # via ``SPAN_ROUTES[SPAN_NPC_AUTO_REGISTERED]``. ``registry_len``
        # now reflects pool length.
        with npc_auto_registered_span(
            npc_name=minted_name,
            pronouns=mention.pronouns or "",
            role=mention.role or "",
            turn_number=turn_num,
            registry_len=len(snapshot.npc_pool),
        ):
            logger.info(
                "npc.auto_registered name=%r pronouns=%r role=%r turn=%d",
                minted_name,
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


def _is_consumable_item(item: dict[str, object]) -> bool:
    """True only for genuine single-use items the ``items_consumed`` lane may
    remove. The consume lane means "spent on use", so a reusable item — a tool
    (Pocket Handkerchief), weapon, armor, or quest item — must NOT be destroyed
    when the narrator incidentally describes using it (playtest 2026-06-04, oz
    turn 7). An item qualifies as consumable when its ``category`` is
    ``consumable`` OR it carries a ``consumable`` tag (authored single-use
    food/potions/scrolls). Everything else is preserved; explicit destruction
    flows through the separate ``items_lost`` lane.
    """
    category = str(item.get("category", "") or "").strip().lower()
    if category == "consumable":
        return True
    tags = item.get("tags") or []
    if isinstance(tags, (list, tuple, set)):
        return any(str(tag).strip().lower() == "consumable" for tag in tags)
    return False


_HEAL_EXPR_RE = re.compile(r"^\s*(\d+d\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)


def _parse_heal_spec(expr: object) -> DamageSpec | None:
    """Parse an item ``heal_amount`` expression (``"1d6+2"``) into a
    :class:`DamageSpec` (reusing its validated NdM+bonus roller). Returns
    None for a blank/absent value (most consumables don't heal) and raises
    nothing for a malformed one — the caller logs the miss loudly so a
    content typo is visible on the GM panel rather than silently no-healing.
    """
    if not expr:
        return None
    m = _HEAL_EXPR_RE.match(str(expr))
    if not m:
        return None
    dice = m.group(1)
    bonus = int(m.group(2).replace(" ", "")) if m.group(2) else 0
    return DamageSpec(dice=dice, bonus=bonus)


def _apply_consumable_heal(
    item: dict[str, object],
    recipient: Character,
    *,
    player_name: str,
    turn_num: int,
) -> int | None:
    """Apply a consumed item's ``heal_amount`` to the recipient's HpPool and
    emit the ``state_patch.hp`` lie-detector span (ADR-114 §6).

    Story 106-4: the consume lane removed the item but never applied its
    effect — a drunk Potion of Mending healed ZERO HP. This is the effect
    half. Returns the HP actually restored (post-clamp delta), or None when
    the item carries no heal effect. Magnitude is authored on the item
    (``heal_amount``, e.g. ``"1d6+2"`` per the WWN-SRD ruling) — not invented
    here; a malformed expression is surfaced loudly (No Silent Fallbacks).
    """
    raw = item.get("heal_amount")
    if not raw:
        return None
    spec = _parse_heal_spec(raw)
    if spec is None:
        logger.warning(
            "state.consumable_heal_malformed player=%s turn=%d item=%r heal_amount=%r "
            "reason=not_NdM_plus_bonus",
            player_name,
            turn_num,
            str(item.get("name", "") or ""),
            raw,
        )
        return None
    pool = recipient.core.hp
    before = pool.current
    rolled = spec.roll(random.Random())
    new_current = recipient.core.apply_hp_delta(rolled)
    delta = new_current - before
    state_patch_hp_span(
        actor=recipient.core.name,
        delta=delta,
        source="consumable_heal",
        current=new_current,
        maximum=pool.max,
        item=str(item.get("name", "") or ""),
        rolled=rolled,
    )
    logger.info(
        "state.consumable_heal player=%s turn=%d item=%r heal_amount=%r rolled=%d "
        "applied=%d hp=%d/%d",
        player_name,
        turn_num,
        str(item.get("name", "") or ""),
        str(raw),
        rolled,
        delta,
        new_current,
        pool.max,
    )
    return delta


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


def _apply_room_graph_transition_effects(
    snapshot: GameSnapshot,
    *,
    actor: str,
    from_room: str,
    to_room: str,
) -> None:
    """ADR-055 / Story 71-15: room-graph traversal side-effects.

    Fired once per room transition in room-graph navigation. Two effects,
    each emitting an OTEL span (CLAUDE.md observability mandate — the GM
    panel must see traversal turning the dungeon clock):

    1. **Trope progression span.** Records which progressing tropes this
       movement advanced. The single per-turn advance is done by the trope
       engine (``tick_tropes`` in ``_execute_narration_turn``); this span
       correlates that progression with the transition that earned it. It
       does NOT re-advance — re-invoking the tick would double-count
       against the per-turn tick (AC2 no-double-tick).

    2. **Movement-consumed item depletion.** Each of the acting character's
       items carrying a finite ``uses_remaining`` (torch model) burns one
       use. At zero the item is flagged ``exhausted`` and kept in inventory
       — never silently deleted (No Silent Fallbacks).
    """
    from sidequest.telemetry.spans import (
        item_resource_depleted_span,
        room_transition_tick_span,
    )

    progressing = [t.id for t in snapshot.active_tropes if t.status == "progressing"]
    with room_transition_tick_span(
        advanced_tropes=progressing,
        from_room=from_room,
        to_room=to_room,
        pc_name=actor,
    ):
        pass

    character = next((c for c in snapshot.characters if c.core.name == actor), None)
    if character is None:
        return
    for item in character.core.inventory.items:
        uses = item.get("uses_remaining")
        if uses is None or uses <= 0:
            continue
        before = uses
        after = before - 1
        item["uses_remaining"] = after
        exhausted = after == 0
        if exhausted:
            item["exhausted"] = True
        with item_resource_depleted_span(
            item=item.get("id") or item.get("name") or "unknown",
            before=before,
            after=after,
            exhausted=exhausted,
            actor=actor,
        ):
            pass


class ObservationGateOrderError(RuntimeError):
    """Story 72-10: raised when ``_auto_mint_prose_only_npcs`` is reached with
    unresolved prior-turn ``observation_pending`` pool members still present —
    proof that ``_apply_npc_observation_gate`` did not run first this turn.

    This is a fail-loud dev invariant guarding a load-bearing pipeline order.
    In correct operation the gate resolves every pending member before the
    minter runs, so this never fires. If a refactor reorders, removes, or
    short-circuits the gate, the ratification step silently becomes a no-op and
    the phantom-NPC failure mode returns — this turns that silent degradation
    into an immediate, observable crash (CLAUDE.md "No Silent Fallbacks").
    """


def _assert_observation_gate_preceded_mint(
    *,
    snapshot: GameSnapshot,
    turn_num: int,
) -> None:
    """Story 72-10: ordering invariant for the NPC ratification pipeline.

    Invoked on the real apply path between ``_apply_npc_observation_gate`` and
    ``_auto_mint_prose_only_npcs``. The gate resolves every prior-turn
    ``observation_pending`` member (promote → flag cleared, or purge → removed),
    so a surviving pending member here means the gate did not run first. Emit a
    warning-severity violation span the GM panel can see, then raise.

    Behavioral, not source-text: it reads runtime pool state, so it survives
    refactors and only fires on a genuine ordering break (CLAUDE.md "No
    Source-Text Wiring Tests").
    """
    unresolved = [m for m in snapshot.npc_pool if m.observation_pending]
    if not unresolved:
        return
    pending_names = ", ".join(m.name for m in unresolved if m.name)
    with npc_observation_gate_order_violation_span(
        pending_count=len(unresolved),
        pending_names=pending_names,
        turn_number=turn_num,
    ):
        logger.warning(
            "npc.observation_gate_order_violation pending_count=%d names=%r turn=%d",
            len(unresolved),
            pending_names,
            turn_num,
        )
    raise ObservationGateOrderError(
        f"_apply_npc_observation_gate must run before _auto_mint_prose_only_npcs: "
        f"{len(unresolved)} prior-turn observation_pending pool member(s) survive "
        f"at the mint call site ({pending_names or '<unnamed>'}); the ratification "
        f"gate did not run first this turn (turn={turn_num})."
    )


def _resolve_heading_to_cartography(
    location: str, pack: GenrePack | None, world: str | None
) -> str | None:
    """Resolve a narrator location heading to a known cartography region id.

    Returns the cartography ``region_id`` when ``location`` (full form, or
    its leading ``Place — Epithet`` segment) names a region declared in
    ``pack.worlds[world].cartography``; otherwise ``None``. ``None`` for
    callers without a resolvable cartography (``pack=None`` test paths,
    worlds with no regions) so they fall through to the surface-form region
    path unchanged.
    """
    if pack is None or world is None:
        return None
    world_obj = pack.worlds.get(world)
    if world_obj is None:
        return None
    cartography = getattr(world_obj, "cartography", None)
    if cartography is None or not cartography.regions:
        return None
    return resolve_known_region_id(
        location,
        {rid: region.name for rid, region in cartography.regions.items()},
    )


# Story 98-5 (ADR-141): placeholder spike-drive rating until a per-ship drive
# subsystem sources it. SWN starter ships ship rating 1; the rating only feeds a
# route's ``drive_rating_min`` strain gate (extra fuel, never a block), so a
# placeholder is safe — see the Design Deviation logged for 98-5.
_DEFAULT_SHIP_DRIVE_RATING = 1


def _adjudicate_inter_system_jump_for_advance(
    *,
    cartography: CartographyConfig,
    from_region: str,
    to_region: str,
    ruleset: str,
    turn: int,
) -> None:
    """Adjudicate an orbital region advance as a campaign-scale inter-system jump
    (Story 98-5, ADR-141). The live movement seam reaching ``orbital/jump.py``.

    Only a real cartography adjacency is a jump edge; a non-adjacency advance
    (teleport, init, narrator leap) is NOT a jump and loud-skips. A bound ruleset
    with no jump model (e.g. a future orbital world on a non-SWN ruleset)
    loud-skips too rather than abandoning the already-applied region move."""
    from sidequest.orbital.jump import adjudicate_inter_system_jump

    from_obj = cartography.regions.get(from_region)
    if from_obj is None or to_region not in from_obj.adjacent:
        logger.info(
            "jump.skip_non_adjacency from=%r to=%r (region advance is not a "
            "cartography adjacency — not an inter-system jump)",
            from_region,
            to_region,
        )
        return
    # Resume-stable seed (ADR-128): same turn + edge → same hazard roll on replay.
    rng = random.Random(f"jump:{from_region}->{to_region}@{turn}")
    try:
        adjudicate_inter_system_jump(
            cartography=cartography,
            from_region=from_region,
            to_region=to_region,
            ruleset=ruleset,
            drive_rating=_DEFAULT_SHIP_DRIVE_RATING,
            rng=rng,
        )
    except NotImplementedError:
        logger.info(
            "jump.skip_no_ruleset_model ruleset=%r from=%r to=%r (bound ruleset has "
            "no inter-system jump model; region move stands)",
            ruleset,
            from_region,
            to_region,
        )


# A chase/escape is intrinsically continuous movement — the narrator advances
# the scene location every turn by design. Movement-category confrontations are
# MOBILE: they move WITH the party, so a scene/location change CONTINUES them
# rather than abandoning them (road_warrior chase bug, playtest 2026-06-04). Real
# endings still come via dial-threshold (escaped), opponent-yield, or a beat
# consequence ("Kill the Engine"). Anchored categories (social negotiation,
# combat in a room) keep the abandon-on-leave semantics.
_MOBILE_CONFRONTATION_CATEGORIES = frozenset({"movement"})


def _encounter_is_mobile(enc: object, pack: GenrePack | None) -> bool:
    """True when the active encounter is a mobile (movement-category) confrontation.

    Reads the self-describing ``enc.category`` first (stamped from
    ``ConfrontationDef.category`` at instantiation). For legacy saves predating
    that field (``category == ""``) it falls back to a live pack lookup so a
    resumed chase is still recognized as mobile — never silently mis-classify a
    chase as anchored (No Silent Fallbacks). Returns False when neither source
    resolves a category (degrades to the existing abandon-on-leave behavior).
    """
    cat = (getattr(enc, "category", "") or "").strip()
    if not cat and pack is not None and pack.rules:
        from sidequest.server.dispatch.confrontation import find_confrontation_def

        cdef = find_confrontation_def(pack.rules.confrontations, getattr(enc, "encounter_type", ""))
        cat = (cdef.category if cdef is not None else "") or ""
    return cat in _MOBILE_CONFRONTATION_CATEGORIES


def merge_sidecar_extraction_transactional(
    result: NarrationTurnResult, extraction: SidecarExtraction
) -> NarrationTurnResult:
    """Source the seven transactional bucket-B fields from the post-narration
    sidecar extractor onto the narration result (ADR-150 step 4, Story 151-4).

    The seven fields — ``items_gained`` / ``items_lost`` / ``items_discarded`` /
    ``items_consumed``, ``gold_change``, ``companions_added`` /
    ``companions_dismissed`` — are retired from the narrator ``game_patch``
    (``orchestrator.extract_structured_from_response``) and produced by the
    post-narration extractor instead. The extraction is the SOLE source: each
    field is OVERWRITTEN, never merged with the result's own (retired) value —
    No Silent Fallbacks, no second producer running in parallel (ADR-150).

    The WS turn handler calls this between the (pre-apply) extractor and
    ``_apply_narration_result_to_snapshot``; the apply machinery
    (``resolve_item_recipient`` attribution, the gold clamp,
    ``_apply_companion_changes`` dedup, and the ``inventory.narrator_extracted``
    catch-loop) is UNCHANGED and consumes these now-extractor-sourced fields.
    """
    result.items_gained = list(extraction.items_gained)
    result.items_lost = list(extraction.items_lost)
    result.items_discarded = list(extraction.items_discarded)
    result.items_consumed = list(extraction.items_consumed)
    result.gold_change = extraction.gold_change
    result.companions_added = list(extraction.companions_added)
    result.companions_dismissed = list(extraction.companions_dismissed)
    return result


def _engine_actor_sides(snapshot: GameSnapshot) -> dict[str, str]:
    """The engine-owned membership map: each seated actor's ``side`` keyed by name.

    This is the confrontation the IntentRouter already seated pre-narrator
    (ADR-150). Empty when no confrontation is engaged — every extracted mention
    then defaults to ``neutral``, so a prose-only "opponent" cannot spoof
    combatant membership.
    """
    encounter = getattr(snapshot, "encounter", None)
    if encounter is None:
        return {}
    return {actor.name: actor.side for actor in encounter.actors}


def merge_sidecar_extraction_npcs_present(
    result: NarrationTurnResult,
    extraction: SidecarExtraction,
    snapshot: GameSnapshot,
) -> NarrationTurnResult:
    """Source ``npcs_present`` from the post-narration extractor, with ENGINE-OWNED
    ``side`` (ADR-150 step 4, Story 151-5 cutover II).

    The extractor ENRICHES (name / pronouns / role / appearance, read from prose);
    the ENGINE ADJUDICATES membership: each mention's ``side`` is resolved from the
    confrontation the IntentRouter already seated pre-narrator
    (``snapshot.encounter.actors``), NOT from the extractor's prose-read claim. An
    actor the engine never seated defaults to ``neutral`` — closing the
    "wrong side breaks momentum routing" bug class (ADR-150 §Decision).

    When the extractor's CLAIMED side disagrees with the engine-resolved side, a
    ``sidecar_extraction.mismatch`` span fires (the relocated lie-detector — the GM
    panel sees the override). The extraction is the SOLE source: a stale
    ``result.npcs_present`` (a non-compliant narrator's retired game_patch leak) is
    OVERWRITTEN, never merged (No Silent Fallbacks).

    Called by the WS turn handler between the (pre-apply) extractor and
    ``_apply_narration_result_to_snapshot``; the downstream ``_apply_npc_mentions``
    machinery is UNCHANGED.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.telemetry.spans.sidecar_extraction import sidecar_extraction_mismatch_span

    engine_sides = _engine_actor_sides(snapshot)
    mentions: list[NpcMention] = []
    for raw in extraction.npcs_present:
        # ``side`` is ENGINE-OWNED and overwritten below, so the extractor's CLAIMED
        # side must NOT be allowed to crash the merge. The extractor is a Haiku reader
        # handed a free-form ``list[dict]`` schema with NO side-enum guidance, so an
        # out-of-enum claim (e.g. ``"hostile"``) is plausible — and
        # ``NpcMention.from_value`` RAISES ``ValueError`` on it. The post-narration
        # pass is NON-FATAL by contract (sidecar_extractor.py: "never raises into the
        # WS turn pipeline; the per-field catch-loops are the net"), so a bad claim is
        # a mismatch to RECORD, never a turn-killer. Capture the raw claim for the
        # witness, then parse enrichment with a validator-safe side (it is discarded
        # immediately by the engine override). Absent/empty claim → "neutral" (parity
        # with ``from_value``), so an unseated NPC with no claim fires no false mismatch.
        claimed_side = str((raw.get("side") if isinstance(raw, dict) else None) or "neutral")
        safe_raw = {**raw, "side": "neutral"} if isinstance(raw, dict) else raw
        # ``from_value`` parses the enrichment (name/pronouns/role/appearance/is_new/
        # is_creature/disengaged); the neutral side here is overwritten below.
        mention = NpcMention.from_value(safe_raw)
        engine_side = engine_sides.get(mention.name, "neutral")
        if claimed_side != engine_side:
            with sidecar_extraction_mismatch_span(
                field="npcs_present",
                evidence=(
                    f"extractor claimed side={claimed_side!r} for {mention.name!r}; "
                    f"engine seated side={engine_side!r}"
                ),
            ):
                pass
        # Engine adjudicates membership — overwrite the prose-read claim.
        mention.side = engine_side
        mentions.append(mention)
    result.npcs_present = mentions
    return result


def merge_sidecar_extraction_cosmetic(
    result: NarrationTurnResult, extraction: SidecarExtraction
) -> NarrationTurnResult:
    """Source the cosmetic bucket-B fields (``scene_mood`` / ``visual_scene`` /
    ``footnotes``) from the post-narration extractor (ADR-150 step 4, Story 151-5).

    These are presentation / feed fields with no engine ownership — copied from the
    extraction, the ``visual_scene`` dict rebuilt into the ``VisualScene`` model the
    result holds (the same conversion the result assembler does). A SEPARATE seam
    from the npcs merge so the cosmetic fields can be scheduled off the critical path
    (ADR-150 §Ordering). The extraction is the SOLE source: stale result values are
    OVERWRITTEN (No Silent Fallbacks).
    """
    from sidequest.agents.orchestrator import VisualScene

    result.scene_mood = extraction.scene_mood
    result.visual_scene = (
        VisualScene.from_dict(extraction.visual_scene)
        if isinstance(extraction.visual_scene, dict)
        else None
    )
    result.footnotes = list(extraction.footnotes)
    return result


def _apply_narration_result_to_snapshot(
    snapshot: GameSnapshot,
    result: object,
    player_name: str,
    *,
    room: SessionRoom,
    pack: GenrePack | None = None,
    world: str | None = None,
    dice_failed: bool | None = None,
    dice_actor: str | None = None,
    from_explicit_action: bool = False,
    opposed_player_d20: int | None = None,
    opposed_player_beat_id: str | None = None,
    opposed_player_actor: str | None = None,
    acting_character_name: str | None = None,
    monster_manual: MonsterManual | None = None,
    is_dice_replay: bool = False,
    # Story 105-2: seam-recovery guard needs the dungeon store (via
    # handle.persistence) to perform a missed crossing instead of
    # accepting a confabulated deep. None for non-dungeon worlds.
    lookahead_handle: LookaheadWorkerHandle | None = None,
) -> NarrationApplyOutcome:
    """Apply narrator-extracted fields to the snapshot.

    Phase 1: location, lore_established, npc_pool / npcs
    upsert, inventory items_gained / items_lost. (The legacy quest_updates
    lane was retired in 77-4; a stale ``quest_updates`` key on the raw
    game_patch is auto-forwarded to quest_log, not applied as a typed field.)
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

    ``is_dice_replay`` (Story 97-5): True when this apply runs on the
    dice-resolution replay re-entry of ``_execute_narration_turn``
    (``suppress_intent_router=True``). The replay carries no new player intent —
    the scene's NPCs were already applied on the player-action pass — so the
    entire NPC-mention application sub-block (mention apply, recurring-presence
    detect, observation gate, auto-mint) is skipped to stop every per-mention
    side effect (last_seen stamps, pool matching, mint paths, the disposition
    beat) from double-running within one interaction turn. A loud
    ``npc.mentions_replay_suppressed`` span fires in its place (never a silent
    skip). This is the upstream fix for the blackthorn 2026-06-07 turn-1
    double-apply; it makes Server #742's ``last_development_turn`` across-call
    dedupe dead for its stated purpose (kept as defense-in-depth).
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
        # Ping-pong 2026-06-07 ("region-mode location drift kills combat"):
        # set True by the region-resolution branches below when this turn's
        # location-string change is a narrator scene-title drift WITHIN the
        # party's current cartography region (region-mode worlds only) —
        # e.g. 'New Kowloon, Yula' → 'New Kowloon — Transit Promenade'.
        # Such a drift is NOT a scene boundary, so the encounter
        # abandon-on-location-change ladder must not treat it as one.
        _same_region_drift = False
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
        # Story 71-15 (ADR-055): room-graph traversal side-effects —
        # per-transition trope-progression span + movement-consumed item
        # depletion. Gated to room-graph navigation (``discovered_rooms`` is
        # populated only by ``init_room_graph_location`` in room_graph mode)
        # and to a genuine transition (old != new). Deliberately NOT routed
        # through ``process_room_entry`` — that is a chassis-confrontation
        # auto-fire hook that early-returns for ordinary room-graph rooms
        # (Story 71-15 finding; ADR-055 2026-05-28 amendment).
        if (
            actor_for_location
            and snapshot.discovered_rooms
            and old_loc is not None
            and result.location != old_loc
        ):
            _apply_room_graph_transition_effects(
                snapshot,
                actor=actor_for_location,
                from_room=old_loc,
                to_room=result.location,
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
            # MP scene-cohort propagation (sq-playtest 2026-05-11 per-player
            # location desync): the narrator emits a single ``location`` field
            # per turn, which is the *scene* location — not just the actor's.
            # When the acting PC's scene changes, every other seated PC who
            # was previously co-located with the actor follows them into the
            # new scene. PCs at a different prior location are a genuine
            # party split and stay put. PCs with no prior entry have already
            # been bootstrapped by ``_bootstrap_character_locations_from_opening``;
            # if they somehow have no entry here we leave them untouched
            # rather than guess.
            if old_loc and result.location != old_loc:
                cohort_followed: list[str] = []
                for seated_name in snapshot.player_seats.values():
                    if not seated_name or seated_name == actor_for_location:
                        continue
                    peer_old = snapshot.character_locations.get(seated_name)
                    if peer_old == old_loc:
                        snapshot.character_locations[seated_name] = result.location
                        cohort_followed.append(seated_name)
                if cohort_followed:
                    _watcher_publish(
                        "state_transition",
                        {
                            "kind": "scene_cohort_followed",
                            "actor": actor_for_location,
                            "followers": cohort_followed,
                            "old_location": old_loc,
                            "new_location": result.location,
                        },
                        component="game",
                    )
        # Story 90-6 (reconcile with #739): the region-mode determination depends
        # only on pack+world (cartography.navigation_mode), NOT the narrator
        # location string — so compute it BEFORE the validity gate. A rejected
        # heading (bracketed/multiline/too_long) in a region-mode world is not a
        # region exit either (current_region cannot advance on it), so a live
        # COMBAT must survive it; that needs _same_region_drift set here, which
        # the valid-region branches below cannot reach for a rejected heading.
        from sidequest.genre.models.world import NavigationMode

        _region_world_obj = (
            pack.worlds.get(world) if (pack is not None and world is not None) else None
        )
        _region_cart = (
            getattr(_region_world_obj, "cartography", None)
            if _region_world_obj is not None
            else None
        )
        _is_region_mode_world = (
            _region_cart is not None
            and getattr(_region_cart, "navigation_mode", None) == NavigationMode.region
        )
        # Story 45-16: filter narrator-emitted location before adding
        # to the region graph. Playtest 3 leaked
        # `(aside — narrator brief)` into discovered_regions because
        # this seam appended unconditionally. Reject + emit OTEL so
        # Sebastien's lie-detector sees the filter fire.
        is_valid_region, rejection_reason = validate_region_name(result.location)
        if not is_valid_region:
            # Story 90-6: a rejected heading is not a region exit. In a region-
            # mode world, flag it as a same-region drift so a live combat is not
            # abandoned on a garbage re-title (no causal span would link them).
            if _is_region_mode_world:
                _same_region_drift = True
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
            # Playtest 2026-05-31 (burning_peace): a narrator scene heading
            # that denotes a KNOWN cartography region ("Edo — The Shogunate's
            # Capital") must resolve to that region's id ("edo") rather than
            # forking a duplicate beside the bare slug seeded at region.init.
            # Match the heading (full form, then leading place) against the
            # world's cartography regions; unknown leading places fall through
            # to the surface-form path below, preserving narrator-invented
            # sub-area forking (story 45-17).
            known_region_id = _resolve_heading_to_cartography(result.location, pack, world)
            # Region-mode detection (``_is_region_mode_world``) is computed ABOVE
            # the validity gate now (Story 90-6 reconcile) and is available here.
            # In a region-mode world the cartography region set is AUTHORED/closed,
            # so an unresolved heading is a sub-location/POI within the current
            # region — not a new region to fork into discovered_regions. Room-graph
            # (dungeon) worlds manage current_region via the room graph / frontier
            # hook and legitimately fork narrator-invented sub-areas (Story 45-17).
            if known_region_id is not None:
                canonical_slug = canonicalize_region_name(known_region_id)
                already_present = any(
                    canonicalize_region_name(existing) == canonical_slug
                    for existing in snapshot.discovered_regions
                )
                if not already_present:
                    # Store the canonical region id (a real map-graph node),
                    # not the epithet heading — map_emit filters
                    # discovered_regions to node ids.
                    snapshot.discovered_regions.append(known_region_id)
                # sq-playtest 2026-06-02 (wry_whimsy/oz): advance current_region
                # to the region the narrator's heading names. In a region-mode
                # world the narrator reliably emits result.location (the scene
                # heading) but does NOT reliably emit the explicit
                # apply_world_patch(current_region=...) the cartography
                # RegionProjection asks for — so current_region froze at the
                # init region (stuck at munchkin_country three regions deep) and
                # the Location panel never refreshed (LOCATION_DESCRIPTION
                # re-emits only on a region change). Deriving it from the
                # already-resolved cartography region id is engine-deterministic
                # (no LLM compliance needed) and fires ONLY on a real region
                # match — a sub-area heading like "The Emerald City — The Throne
                # Room" resolves to the_emerald_city, so it never over-advances.
                # Gated to region-mode worlds (computed above): room-graph
                # (dungeon) worlds manage current_region via the room graph /
                # frontier hook.
                if _is_region_mode_world and snapshot.current_region == known_region_id:
                    # The heading resolved to the region the party is ALREADY
                    # in — a same-region scene-title drift, not a region
                    # change. Flag it so the encounter abandon ladder below
                    # treats this turn as scene-continuous.
                    _same_region_drift = True
                    # Story 105-2 drift-strip (Architect decision: strip, don't
                    # cross). A seam region is a THRESHOLD — a narrator
                    # sub-title over it is exactly how a confabulated deep gets
                    # narrated ("The Dropmouth — The Deep", the 2026-06-12
                    # turn-3 repro: the leading segment resolves to the seam
                    # region, so this drift branch — not the unresolvable-
                    # heading guard — catches that shape). We re-anchor to the
                    # region's canonical display name rather than firing a real
                    # crossing, because crossing off a drifted title would be
                    # text-classification teleportation (spec §3 rejected the
                    # lexical floor). Benign POI re-titles are flattened ONLY
                    # in seam-owning regions — acceptable cost. Seam-less
                    # regions (oz) keep the 90-6 cosmetic re-title unchanged.
                    if seam_route_for(_region_cart, known_region_id) is not None:
                        _drift_region_obj = getattr(_region_cart, "regions", {}).get(
                            known_region_id
                        )
                        # str() pin: getattr over the duck-typed cartography is
                        # Any — assigning Any into result.location would widen
                        # pyright's str-narrowing for the whole block below.
                        _canonical_display: str = str(
                            getattr(_drift_region_obj, "name", "") or known_region_id
                        )
                        if result.location != _canonical_display:
                            with region_entry_rejected_span(
                                entry=result.location,
                                reason="seam_region_sub_location_stripped",
                                caller_path="narration_apply.location_update",
                                player_name=player_name,
                            ):
                                logger.warning(
                                    "region.seam_region_sub_location_stripped "
                                    "entry=%r region=%s canonical=%r player=%s — "
                                    "sub-title over a seam region re-anchored to "
                                    "the canonical name (never narrate a deep the "
                                    "engine didn't open)",
                                    result.location,
                                    known_region_id,
                                    _canonical_display,
                                    player_name,
                                )
                            _reanchor_location_ledger(
                                snapshot,
                                confabulated=result.location,
                                replacement=_canonical_display,
                                actor_for_location=actor_for_location,
                            )
                            result.location = _canonical_display
                if _is_region_mode_world and snapshot.current_region != known_region_id:
                    _prior_region = snapshot.current_region
                    snapshot.current_region = known_region_id
                    snapshot.pc_regions[player_name] = known_region_id
                    # Story 59-30 — Site B: stamp the per-PC relocation receipt
                    # the movement engagement witness reads. MANDATORY here:
                    # region-mode worlds (oz/wonderland/gulliver) relocate at
                    # this seam, NOT through apply_world_patch — without it the
                    # witness false-negatives on every region-mode move. The
                    # witness is via-agnostic, so ``via="narration_apply"`` reads
                    # as engaged exactly like a ``world_patch`` stamp.
                    snapshot.region_transitions.append(
                        RegionTransition(
                            turn=snapshot.turn_manager.interaction,
                            pc_name=player_name,
                            from_region=_prior_region or None,
                            to_region=known_region_id,
                            via="narration_apply",
                        )
                    )
                    logger.info(
                        "region.current_region_advanced old=%r new=%r player=%s "
                        "caller=narration_apply.location_update",
                        _prior_region,
                        known_region_id,
                        player_name,
                    )
                    # OTEL lie-detector (CLAUDE.md OTEL principle): the GM panel
                    # must see the engine advance current_region so a regression
                    # back to the frozen-Location-panel state is visible — and so
                    # "the narrator moved the party" can be told from "the engine
                    # tracked it".
                    _watcher_publish(
                        "region_current_advanced",
                        {
                            "old_region": _prior_region or "",
                            "new_region": known_region_id,
                            "player_name": player_name,
                            "turn_number": snapshot.turn_manager.interaction,
                        },
                        component="location",
                    )
                    # Story 95-1 — Site B: re-center the per-location orrery on
                    # the party's new system. The identity join (region id ==
                    # star body id) means the region we just advanced into names
                    # the star the chart should center on. A no-op for non-orbital
                    # region-mode worlds (oz/wonderland: orbital_content is None);
                    # a loud-skip for an orbital region with no star body (emits
                    # orbital.scope_bind_skipped). MANDATORY here: orbital
                    # region-mode worlds (perseus_cloud) relocate at THIS seam —
                    # movement.py defers region-mode moves to this path — so the
                    # chart cannot follow the party without it.
                    room.session.bind_region_scope(known_region_id, trigger="relocation")
                    # Story 98-5 (ADR-141): a current_region advance in an ORBITAL
                    # region-mode world IS a campaign-scale inter-system jump
                    # (region id == star-system id). Adjudicate its cost through
                    # the bound ruleset (ADR-117) and emit the GM-panel jump spans
                    # — the live movement seam reaching orbital/jump.py. Gated to
                    # orbital worlds (orbital_content present): non-orbital
                    # region-mode worlds (oz/wonderland) have no jump scale.
                    _region_rules = getattr(pack, "rules", None) if pack is not None else None
                    if (
                        _prior_region
                        and room.session.orbital_content is not None
                        and _region_cart is not None
                        and _region_rules is not None
                    ):
                        _adjudicate_inter_system_jump_for_advance(
                            cartography=_region_cart,
                            from_region=_prior_region,
                            to_region=known_region_id,
                            ruleset=_region_rules.ruleset,
                            turn=snapshot.turn_manager.interaction,
                        )
                if known_region_id != result.location:
                    with region_entry_canonicalized_dedup_span(
                        entry=result.location,
                        canonical_slug=canonical_slug,
                        existing_surface_form=known_region_id,
                        caller_path="narration_apply.location_update",
                        resolution="cartography",
                    ):
                        logger.info(
                            "region.entry_resolved_to_cartography entry=%r region_id=%r "
                            "appended=%s caller=narration_apply.location_update",
                            result.location,
                            known_region_id,
                            not already_present,
                        )
            elif _is_region_mode_world:
                # Story 105-2: seam recovery. The heading didn't resolve to a
                # known cartography region. Before concluding it's a harmless
                # POI sub-title (the 90-6 path), check whether the PC stands on
                # a region that OWNS a seam route — if so, the unresolved
                # heading is the relocation signal the intent router missed (the
                # 2026-06-12 turn-3 repro: "The Dropmouth — The Deep").
                # An engine MUST NOT narrate a crossing it did not perform:
                # do the real crossing now, or reject the patch loud.
                # Never accept the confabulated scene (No Silent Fallbacks).
                _pc_region = snapshot.region_for(perspective=player_name) or ""
                _seam_route = seam_route_for(_region_cart, _pc_region)
                if _seam_route is not None:
                    # The ledger write above already stamped the confabulated
                    # heading onto the actor (and cohort followers); capture it
                    # so both guard outcomes can rewrite the ledger honestly.
                    _confab_heading = result.location
                    try:
                        _crossing = get_seam_resolver(str(_seam_route.to_id))(
                            snapshot=snapshot,
                            player_name=player_name,
                            route=_seam_route,
                            resolved_via="narration_seam_recovery",
                            dungeon_store=(
                                lookahead_handle.persistence
                                if lookahead_handle is not None
                                else None
                            ),
                        )
                    except SeamCrossingError as _seam_err:
                        # Crossing failed (no store, no entrance node, etc.).
                        # Fail loud — never silently accept the confabulation.
                        with region_entry_rejected_span(
                            entry=result.location,
                            reason="seam_crossing_unresolvable",
                            caller_path="narration_apply.location_update",
                            player_name=player_name,
                        ):
                            logger.error(
                                "region.seam_crossing_unresolvable entry=%r pc=%s "
                                "region=%s seam=%s reason=%s — location patch "
                                "REJECTED (No Silent Fallbacks)",
                                result.location,
                                player_name,
                                _pc_region,
                                _seam_route.to_id,
                                _seam_err.reason,
                            )
                        # Drop the patch. NOTE: we are already INSIDE the
                        # ``if result.location:`` block — emptying it here does
                        # NOT rewind the writes above (ledger: restored just
                        # below; sweep/abandon: neutralized via the drift flag
                        # just below). What the falsy value DOES protect is the
                        # re-gated consumers downstream of this point: the
                        # ``state.location_update`` log/watcher emit (re-gated
                        # on truthiness) and callers that re-check
                        # ``result.location`` after apply (e.g. the render
                        # trigger). PC stays put, honestly. Restore the ledger
                        # entries the pre-resolution write clobbered with the
                        # confabulation (back to old_loc; pop if there was none).
                        result.location = ""
                        _reanchor_location_ledger(
                            snapshot,
                            confabulated=_confab_heading,
                            replacement=old_loc,
                            actor_for_location=actor_for_location,
                        )
                        # The PC did NOT move — this rejected re-title is not a
                        # scene boundary. Without this flag, ``old_loc != ""``
                        # downstream would sweep Scratch and abandon an anchored
                        # combat — a regression vs the pre-105-2 drift handling
                        # of this exact heading shape.
                        _same_region_drift = True
                    else:
                        # Crossing performed: pc_region patch + movement.resolved
                        # span fired by the resolver. Re-anchor the scene to the
                        # authored entrance room's name so the narration record
                        # carries the REAL room, not the confabulation — and
                        # rewrite the ledger entries stamped with the confab so
                        # ledger and scene agree.
                        result.location = _entrance_room_name(
                            crossing_region=_crossing.to_region,
                            lookahead_handle=lookahead_handle,
                        )
                        _reanchor_location_ledger(
                            snapshot,
                            confabulated=_confab_heading,
                            replacement=result.location,
                            actor_for_location=actor_for_location,
                        )
                        _same_region_drift = False
                else:
                    # No seam route on this region — the heading is a
                    # sub-location/POI WITHIN the current region (the original
                    # 90-6 path: "Dunkelkurve — Inside the Tunnel" inside
                    # sturmichi). Do NOT fork it into discovered_regions.
                    # OTEL lie-detector: the GM panel must see the engine skip
                    # the pollution (No Silent Fallbacks — the skip is a
                    # decision, logged). See DRIVER 2026-06-04, the_circuit.
                    with region_entry_rejected_span(
                        entry=result.location,
                        reason="sub_location_in_region_mode_world",
                        caller_path="narration_apply.location_update",
                        player_name=player_name,
                    ):
                        logger.info(
                            "region.entry_skipped_sub_location entry=%r current_region=%r "
                            "player=%s caller=narration_apply.location_update "
                            "(region-mode world: scene title is a POI within the region, "
                            "not a new cartography region)",
                            result.location,
                            snapshot.current_region,
                            player_name,
                        )
                    # Ping-pong 2026-06-07: the engine just concluded this heading
                    # is a POI WITHIN the current region — the same-region signal
                    # the encounter abandon ladder below must respect (the perseus
                    # repro: combat seated, then deactivated the SAME turn by
                    # 'New Kowloon, Yula' → 'New Kowloon — Transit Promenade').
                    _same_region_drift = True
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
        # Story 105-2 lie-detector hygiene: the seam-recovery REJECT path
        # empties result.location mid-block (the patch was dropped; the PC
        # did NOT move). Emitting a location_update with after="" on that
        # turn would be the GM panel reporting a move the engine refused —
        # the rejection already emitted its own region.entry_rejected span.
        if result.location:
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
            # Story 97-4 (sibling of #739): the scratch sweep keyed on the raw
            # ``old_loc != result.location`` string, so a same-region scene-title
            # drift in a region-mode world (perseus: 'New Kowloon, Yula' ->
            # 'New Kowloon — Transit Promenade') wiped scene-bounded status
            # (Scratch/Boon) even though the party never left the scene. #739
            # fixed the encounter-abandon ladder for exactly this drift via the
            # ``_same_region_drift`` signal computed above; the sweep is the
            # sibling gate. Consult the SAME signal: on a same-region drift the
            # sweep is SKIPPED (this turn is scene-continuous). A genuine region
            # change leaves the flag False and the sweep runs unchanged
            # (scene-boundary semantics from Playtest 2026-04-26 Bug #1 preserved).
            if not _same_region_drift:
                from sidequest.server.status_clear import clear_scratch_on_scene_end

                clear_scratch_on_scene_end(
                    snapshot,
                    reason="location_change",
                    turn=snapshot.turn_manager.interaction,
                )
            else:
                # OTEL lie-detector (CLAUDE.md OTEL principle): the GM panel must
                # see the engine CHOSE to keep scene-bounded status across a
                # same-region drift — otherwise a regression that silently
                # resumes sweeping is invisible. Mirrors the encounter ladder's
                # ``confrontation_continued_same_region_drift`` keep span below.
                logger.info(
                    "status.scratch_sweep_skipped_same_region_drift "
                    "current_region=%r old_location=%r new_location=%r player=%s",
                    snapshot.current_region,
                    old_loc,
                    result.location,
                    player_name,
                )
                _watcher_publish(
                    "scratch_sweep_skipped_same_region_drift",
                    {
                        "current_region": snapshot.current_region or "",
                        "old_location": old_loc,
                        "new_location": result.location,
                        "player_name": player_name,
                        "turn_number": snapshot.turn_manager.interaction,
                    },
                    component="encounter",
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
            # Mobility exemption (road_warrior chase bug, playtest 2026-06-04).
            # A chase/escape (category="movement") legitimately moves WITH the
            # party — the narrator advances the scene location every turn by
            # design, so abandoning the encounter on any location-string change
            # makes the chase structurally un-runnable (it never survives to a
            # turn where the narrator is offered its beats → 0 beats ever fired).
            # The earlier "location change always resolves the encounter" rule
            # was the negotiation-walk-out fix (2026-04-30); the `category in
            # {chase, mobile}` skip it punted to "if the gap surfaces in
            # playtest" is now implemented via `_encounter_is_mobile`. Anchored
            # categories (social negotiation, combat in a room) keep the
            # abandon-on-leave semantics; a mobile encounter below threshold
            # CONTINUES (real endings still come via dial-threshold/opponent-
            # yield/beat-consequence, all checked first below).
            active_encounter = snapshot.encounter
            if active_encounter is not None and not active_encounter.resolved:
                abandoned_type = active_encounter.encounter_type
                # A location change at/after a met win threshold is the natural
                # CONSEQUENCE of winning, not an abandonment — escape/movement
                # encounters win precisely BY leaving the scene. If a non-beat
                # momentum path advanced the dial to threshold without running
                # apply_beat's victory check (sq-playtest 2026-06-02
                # wry_whimsy/oz: escape dial 8/8 yet total_beats_fired=0),
                # resolve on the met win condition so the player keeps victory
                # credit instead of being recorded as having walked away. Since
                # Story 59-31 this is a three-way branch: dial win → victory,
                # opponent yield → victory (opponent_yielded), and only a
                # genuinely-unfinished encounter (no threshold met AND no
                # opponent yield) falls through to abandoned_on_location_change.
                won_outcome = active_encounter.dial_threshold_outcome()
                yield_outcome = active_encounter.opponent_yield_outcome()
                # NOTE: `resolved=True` is set INSIDE each resolving branch below
                # (not unconditionally up front) so the mobile-continue branch can
                # leave a movement encounter ACTIVE. The opponent-yield branch sets
                # it via `_resolve_opponent_yield`.
                if won_outcome is not None:
                    from sidequest.game.encounter import EncounterPhase as _EncounterPhase

                    active_encounter.resolved = True
                    active_encounter.outcome = won_outcome
                    active_encounter.structured_phase = _EncounterPhase.Resolution
                    logger.info(
                        "encounter.resolved_on_location_change "
                        "encounter_type=%s outcome=%s old_location=%r "
                        "new_location=%r player=%s",
                        abandoned_type,
                        won_outcome,
                        old_loc,
                        result.location,
                        player_name,
                    )
                    # OTEL lie-detector (CLAUDE.md OTEL principle): the GM panel
                    # must see that the scene-boundary resolved the encounter as
                    # a WIN on its met dial — not as an abandonment — so a
                    # regression that loses the victory credit is visible.
                    _watcher_publish(
                        "confrontation_resolved_on_location_change",
                        {
                            "encounter_type": abandoned_type,
                            "outcome": won_outcome,
                            "old_location": old_loc,
                            "new_location": result.location,
                            "player_name": player_name,
                            "turn_number": snapshot.turn_manager.interaction,
                        },
                        component="confrontation",
                    )
                elif yield_outcome is not None:
                    # sq-playtest 2026-06-02 wry_whimsy/oz (Story 59-31): the
                    # opponent (the Cowardly Lion) backed down with no dial
                    # threshold met. Walking on from a cowed opponent is a player
                    # VICTORY, not a walk-away — resolve opponent_yielded before
                    # falling to abandoned. #576 explicitly punted this residual
                    # (no-threshold-met opponent yield) to Story 59-31.
                    logger.info(
                        "encounter.resolved_on_location_change_opponent_yield "
                        "encounter_type=%s old_location=%r new_location=%r player=%s",
                        abandoned_type,
                        old_loc,
                        result.location,
                        player_name,
                    )
                    _resolve_opponent_yield(
                        snapshot,
                        active_encounter,
                        trigger="opponent_yield_on_location_change",
                        turn_number=snapshot.turn_manager.interaction,
                    )
                elif _encounter_is_mobile(active_encounter, pack):
                    # Mobile (movement-category) confrontation below threshold:
                    # a chase/escape MOVES with the party, so a scene/location
                    # change CONTINUES it — do NOT resolve. The encounter stays
                    # live (resolved=False), so next turn the narrator is offered
                    # its beats again (in_chase stays true → build_encounter_context
                    # injects the beat menu) and real mechanical resolution can
                    # finally fire. OTEL lie-detector: the GM panel must see the
                    # engine CHOSE to continue (not silently skip the boundary).
                    logger.info(
                        "encounter.continued_across_location_change "
                        "encounter_type=%s category=%s old_location=%r "
                        "new_location=%r player=%s",
                        abandoned_type,
                        getattr(active_encounter, "category", "") or "",
                        old_loc,
                        result.location,
                        player_name,
                    )
                    _watcher_publish(
                        "confrontation_continued_across_location_change",
                        {
                            "encounter_type": abandoned_type,
                            "category": getattr(active_encounter, "category", "") or "",
                            "old_location": old_loc,
                            "new_location": result.location,
                            "player_name": player_name,
                            "turn_number": snapshot.turn_manager.interaction,
                            "player_metric": active_encounter.player_metric.current,
                            "opponent_metric": active_encounter.opponent_metric.current,
                        },
                        component="confrontation",
                    )
                elif (
                    _same_region_drift
                    and (getattr(active_encounter, "category", "") or "") == "combat"
                ):
                    # Story 90-6 (reconcile): region-mode same-region scene-title
                    # drift on an anchored COMBAT confrontation. Checked AFTER
                    # won/yield/mobile so a met-threshold combat still banks its
                    # win and a chase keeps its mobile-continue — only an
                    # unfinished combat continues. COMBAT-ONLY by design: a social
                    # negotiation walked out of within the region must still
                    # ABANDON (the 2026-04-30 negotiation-walk-out / puppet-NPC
                    # fix), so it falls through to the else below. This narrows
                    # #739, which continued ANY anchored encounter at the top of
                    # the ladder (social included, before the win/yield checks).
                    # OTEL lie-detector: emit confrontation_continued_same_region_drift
                    # so the GM panel sees the engine CHOSE to keep the fight live.
                    logger.info(
                        "encounter.continued_same_region_drift "
                        "encounter_type=%s current_region=%r old_location=%r "
                        "new_location=%r player=%s",
                        abandoned_type,
                        snapshot.current_region,
                        old_loc,
                        result.location,
                        player_name,
                    )
                    _watcher_publish(
                        "confrontation_continued_same_region_drift",
                        {
                            "encounter_type": abandoned_type,
                            "current_region": snapshot.current_region or "",
                            "old_location": old_loc,
                            "new_location": result.location,
                            "player_name": player_name,
                            "turn_number": snapshot.turn_manager.interaction,
                            "player_metric": active_encounter.player_metric.current,
                            "opponent_metric": active_encounter.opponent_metric.current,
                        },
                        component="confrontation",
                    )
                else:
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

    # Story 77-4 (ADR-137 AC-3): the legacy ``quest_updates`` lane is retired —
    # ``record_quest`` update-mode is the typed home, and the clean narrator
    # path no longer emits the key (it was dropped from extraction + the typed
    # NarrationTurnResult field). But a stale narrator can still put a
    # ``quest_updates`` key on the RAW game_patch. No Silent Fallbacks: rather
    # than drop it (status lost) or raise (a live turn crashes), auto-forward
    # the valid (string-status) items to record_quest update-mode semantics
    # (``upsert_quest_status`` — the same status-only mechanism record_quest
    # update-mode uses) and fire the loud, GM-visible
    # ``quest.updates.legacy_emitted`` span. The legacy ``SPAN_QUEST_UPDATE`` no
    # longer fires from this path; ``quest.updated`` is the successor for the
    # clean record_quest path.
    #
    # The span is this guard's lie-detector, so its counts must reflect what
    # ACTUALLY landed: ``updates_count``/``quest_ids`` are the forwarded items,
    # ``skipped_count`` is the dropped ones (non-str status, or a non-dict
    # value). Gate on key PRESENCE — an absent key is the normal clean path and
    # must stay silent — but a present non-dict value still emits an observable
    # signal (never silent, never raises).
    if "quest_updates" in result.game_patch_dict:
        _legacy_quest_updates = result.game_patch_dict["quest_updates"]
        if isinstance(_legacy_quest_updates, dict):
            _forwarded_ids: list[str] = []
            _skipped = 0
            for quest_id, status in _legacy_quest_updates.items():
                if isinstance(quest_id, str) and isinstance(status, str):
                    upsert_quest_status(snapshot.quest_log, quest_id, status)
                    _forwarded_ids.append(quest_id)
                else:
                    _skipped += 1
            # Empty dict → nothing forwarded, nothing dropped → benign no-op.
            if _forwarded_ids or _skipped:
                quest_updates_legacy_emitted_span(
                    quest_ids=_forwarded_ids,
                    updates_count=len(_forwarded_ids),
                    skipped_count=_skipped,
                    player_name=player_name,
                    turn_number=snapshot.turn_manager.interaction,
                )
                logger.warning(
                    "state.quest_updates.legacy_emitted forwarded=%d skipped=%d "
                    "player=%s — narrator emitted a retired quest_updates key; "
                    "valid items auto-forwarded to quest_log",
                    len(_forwarded_ids),
                    _skipped,
                    player_name,
                )
        else:
            # Non-dict value (list/str/number/None) — nothing can forward, but
            # the malformed stale emit must stay observable, never silent.
            quest_updates_legacy_emitted_span(
                quest_ids=[],
                updates_count=0,
                skipped_count=1,
                player_name=player_name,
                turn_number=snapshot.turn_manager.interaction,
            )
            logger.warning(
                "state.quest_updates.legacy_emitted malformed player=%s — narrator "
                "emitted a retired quest_updates key with a non-dict value (%s); "
                "nothing forwarded",
                player_name,
                type(_legacy_quest_updates).__name__,
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
            # Story 114-13: accept the CWN/WN weapon categories (melee_weapon /
            # ranged_weapon) alongside the legacy bespoke "weapon" so a
            # narrator-granted CWN weapon stays a weapon instead of demoting to
            # misc. The genre's bound ruleset, not this allowlist, owns the
            # weapon taxonomy.
            allowed = {
                "weapon",
                "melee_weapon",
                "ranged_weapon",
                "armor",
                "tool",
                "consumable",
                "quest",
                "treasure",
                "misc",
            }
            category = category_raw if category_raw in allowed else "misc"
            if category_raw and category != category_raw:
                # No Silent Fallbacks + OTEL lie-detector (114-13): the narrator
                # minted an off-taxonomy category. We coerce to "misc" rather than
                # crash, but surface the coercion so the GM panel sees the narrator
                # inventing a category instead of it vanishing silently.
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "inventory",
                        "op": "narrator_item_category_coerced",
                        "item": name_val,
                        "narrator_category": category_raw,
                        "stored_category": category,
                    },
                    component="inventory",
                    severity="warning",
                )
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
        preserved_consumes: list[str] = []

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

            # Resolve against the authored catalog first: a gained item whose
            # name/id matches a CatalogItem lands with the authored id +
            # mechanical fields (damage/mitigation/armor_class), so a gained
            # weapon/armor is combat-live instead of an inert narrator mint.
            # No match → bare mint (prior behaviour). Each decision fires an
            # OTEL event (the lie-detector: did this gained item bind to
            # authored mechanics, or was it improvised flavor?).
            # Epic 94: inventory is a world-tier CAST/CATALOG surface — resolve
            # world-first (genre fallback). Reading ``pack.inventory`` directly
            # returned None for migrated packs, so gained items could never bind
            # to authored mechanics in space_opera / heavy_metal worlds.
            from sidequest.server.dispatch.inventory_resolve import resolve_inventory

            _gain_inventory = (
                resolve_inventory(pack, snapshot.world_slug) if pack is not None else None
            )
            catalog = _gain_inventory.item_catalog if _gain_inventory is not None else None
            resolved = resolve_gained_item_dict(entry, catalog)
            if resolved is not None:
                item_dict = resolved
                _watcher_publish(
                    "item_gain.catalog_resolved",
                    {
                        "name": item_dict["name"],
                        "catalog_id": item_dict["id"],
                        "category": item_dict.get("category", ""),
                        "has_damage": "damage" in item_dict,
                        "genre": snapshot.genre_slug,
                        "world": snapshot.world_slug,
                        "player_name": narrating_name,
                        "turn_number": turn_num,
                    },
                    component="inventory",
                )
            else:
                item_dict = _narrator_item_dict(entry)
                _watcher_publish(
                    "item_gain.narrator_minted",
                    {
                        "name": item_dict["name"],
                        "category": item_dict.get("category", ""),
                        "genre": snapshot.genre_slug,
                        "world": snapshot.world_slug,
                        "player_name": narrating_name,
                        "turn_number": turn_num,
                    },
                    component="inventory",
                )
            recipient_char = resolve_item_recipient(
                snapshot,
                entry,
                narrating_character_name=narrating_name,
                lane="gained",
            )
            recipient_char.core.inventory.items.append(item_dict)
            added_names.append(str(item_dict["name"]))
            # ADR-144 / spec 2026-06-18: on a Fate-bound PC, a significant item
            # gained in play promotes to an invokable aspect on the FateSheet.
            # Gate on the recipient HAVING a fate sheet — the same per-character
            # signal the projection uses; a non-Fate PC is untouched. Aspects +
            # permissions only; matched-gear stunts are deferred (counted in the
            # span, not applied). The promoter never touches refresh/fate_points.
            if recipient_char.core.fate_sheet is not None:
                from sidequest.game.ruleset.fate_gear import resolve_fate_gear_catalog
                from sidequest.game.ruleset.fate_item_promotion import promote_gained_item

                # World-first effective Fate gear (story 126-25): the genre catalog
                # UNIONED with the active world's gear by id (world wins), so a
                # world-specific found-item (the Oz silver shoes) is in scope when
                # that world is active. Genre-only when no world gear (ADR-145 §D3).
                _gear_defs = resolve_fate_gear_catalog(pack, snapshot.world_slug)
                _promo = promote_gained_item(
                    sheet=recipient_char.core.fate_sheet,
                    item_id=str(item_dict["id"]),
                    item_name=str(item_dict["name"]),
                    gear_defs=_gear_defs,
                    actor=recipient_char.core.name,
                    narrator_aspect=(str(entry.get("grants_aspect", "") or "").strip() or None),
                )
                if _promo.promoted:
                    item_dict["promoted"] = True

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
                    matched = True
                    # Playtest 2026-06-04 (oz turn 7): the narrator emitted
                    # items_consumed for the chargen Pocket Handkerchief
                    # (category=tool) after Susan wiped rust with it — a
                    # reusable tool was silently destroyed by an incidental
                    # narrated use. The consume lane is "spent on use" and must
                    # only remove genuine single-use consumables; a tool /
                    # weapon / armor / quest item is reusable and stays. This
                    # also avoids a silent inventory deletion the prose never
                    # acknowledged (Keith's #1 consistency class). Explicit
                    # destruction is the items_lost lane, not consume. Per
                    # "No Silent Fallbacks" the refusal is surfaced on the OTEL
                    # span (preserved_consumes) + an INFO log, not swallowed.
                    if _is_consumable_item(existing):
                        # Story 106-4: apply the item's effect BEFORE removing
                        # it — a heal_amount consumable (Potion of Mending)
                        # restores HP to the recipient's pool + emits the
                        # state_patch.hp lie-detector span. The consume half
                        # was already wired; this is the effect half.
                        _apply_consumable_heal(
                            existing,
                            recipient_char,
                            player_name=player_name,
                            turn_num=turn_num,
                        )
                        recipient_char.core.inventory.items.pop(idx)
                        consumed_names.append(consume_name)
                    else:
                        display_name = str(existing.get("name", "") or "") or consume_name
                        preserved_consumes.append(display_name)
                        logger.info(
                            "state.inventory_consume_preserved player=%s turn=%d "
                            "name=%r category=%r reason=non_consumable_reusable",
                            player_name,
                            turn_num,
                            display_name,
                            str(existing.get("category", "") or ""),
                        )
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
            preserved=preserved_consumes,
            player_name=player_name,
            turn_number=turn_num,
            unmatched_discards_count=len(unmatched_discards),
            unmatched_consumes_count=len(unmatched_consumes),
        ):
            logger.info(
                "state.inventory_update player=%s turn=%d gained=%s lost=%s "
                "discarded=%s unmatched_discards=%s consumed=%s "
                "unmatched_consumes=%s preserved_consumes=%s",
                player_name,
                turn_num,
                added_names,
                removed_names,
                discarded_names,
                unmatched_discards,
                consumed_names,
                unmatched_consumes,
                preserved_consumes,
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
    # Story 72-4: thread the genre pack + active world so the Step-3 novel
    # branch can mint narrator-invented NPCs through the ADR-091 culture-bound
    # generator (culture resolves via Pack.effective_cultures(world) — the
    # perseus_cloud session-894 guard). Resolution is LAZY inside the seam:
    # the generator is built only when a genuinely novel name is about to be
    # minted, so quiet turns (no invented NPC) pay nothing.
    # Story 97-5: skip the ENTIRE NPC-mention application sub-block on a
    # dice-resolution replay re-entry. A dice-gated action runs
    # ``_execute_narration_turn`` twice in one interaction turn (pass 1 = player
    # action, pass 2 = the dice handler's ``[BEAT_RESOLVED]`` replay with
    # ``suppress_intent_router=True``). Story 91-2 gave the intent router this
    # guard; the mention-apply never got it, so every per-mention side effect
    # below (last_seen stamps, pool matching, the observation gate, mint paths,
    # the disposition beat) double-ran — the blackthorn 2026-06-07 turn-1
    # double-apply. The replay introduces no new player intent; the scene's
    # NPCs were already applied on the player-action pass. Emit a LOUD span in
    # place of the work (never a silent skip — CLAUDE.md OTEL principle).
    if is_dice_replay:
        with npc_mentions_replay_suppressed_span(
            mention_count=len(result.npcs_present),
            turn_number=turn_num,
        ):
            logger.info(
                "npc.mentions_replay_suppressed turn=%d mentions=%d — "
                "dice-replay re-entry, NPCs already applied on the "
                "player-action pass (story 97-5)",
                turn_num,
                len(result.npcs_present),
            )
    else:
        _apply_npc_mentions(
            snapshot=snapshot,
            mentions=list(result.npcs_present),
            turn_num=turn_num,
            acting_character_name=acting_character_name,
            pack=pack,
            world=world,
            monster_manual=monster_manual,
        )

        # ADR-116 §4 (social path) — a narrator-signalled opponent departure
        # (``disengaged=True``) withdraws the matching opponent actor so the
        # end-on-no-Other sweep below resolves the confrontation instead of
        # zombie-ing it (sq-playtest 2026-06-10 long_foundry). Runs after the
        # mention apply so the actor roster reflects this turn before the sweep.
        _apply_opponent_disengagements(
            snapshot=snapshot,
            mentions=list(result.npcs_present),
            turn_num=turn_num,
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
            # Prose re-citation ratifies (sq-playtest 2026-06-07 purge/mint
            # deadlock) — a pending member named in this turn's narration is
            # observed, not phantom, even when npcs_present omits them.
            narration_text=result.narration or "",
        )

        # Story 72-10: ordering invariant. The gate above resolves every
        # prior-turn observation_pending member, so the pool must hold zero
        # pending entries before the minter runs. A survivor here means the
        # gate did not precede the mint — fail loud + emit a violation span
        # rather than let the ratification gate silently degrade into the
        # phantom-NPC failure mode.
        _assert_observation_gate_preceded_mint(snapshot=snapshot, turn_num=turn_num)

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
        from sidequest.telemetry.spans import (
            confrontation_unengaged_turn_span,
            encounter_beat_skipped_span,
            encounter_resolved_span,
        )

        _mismatch = _validate_intent(
            getattr(result, "action_rewrite", None),
            result.confrontation,
            pack,
            active_encounter=snapshot.encounter is not None and not snapshot.encounter.resolved,
        )

        _intent_text = (
            getattr(getattr(result, "action_rewrite", None), "intent", "") or ""
        ).strip()
        _classified_intent_value = _intent_text or "unspecified"

        # Story 59-1 — no-emission lie-detector. The intent-mismatch path below
        # only fires when the narrator emitted an ``action_rewrite.intent`` to
        # tokenize. The 2026-05-21 Glenross playtest hit the OTHER blind spot:
        # a textbook standoff in prose with NO confrontation field, NO active
        # encounter, AND NO intent — so ``_validate_intent`` returned None and
        # nothing engaged, silently. Emit a STRUCTURAL watcher so the GM panel
        # sees the miss. NOT prose keyword-scanning (the deleted
        # ``_CONFRONTATION_TRIGGER_PATTERNS`` regex stays dead).
        #
        # Precision (no false-positive storm): the structural confrontation-
        # shape signal is an OPPONENT-side actor in ``npcs_present`` — the
        # narrator named an adversary but engaged nothing and emitted no intent.
        # A quiet travel/dialogue/rest turn has no opponent actor and does not
        # fire.
        _no_active_encounter = snapshot.encounter is None or snapshot.encounter.resolved
        _named_opponent = any(m.side == "opponent" for m in result.npcs_present)
        if (
            not result.confrontation
            and _no_active_encounter
            and not _intent_text
            and _named_opponent
        ):
            with confrontation_unengaged_turn_span(
                player_name=player_name,
                genre_slug=snapshot.genre_slug or "",
            ):
                logger.warning(
                    "confrontation.unengaged_turn player=%s genre=%s — turn named "
                    "an opponent but engaged no confrontation and emitted no intent "
                    "(validator blind spot); GM panel should review for a winged standoff",
                    player_name,
                    snapshot.genre_slug or "",
                )

        if _mismatch is not None:
            # Story 59-3 retired the reprompt response to a validator mismatch.
            # The validator's INFORMATIONAL span still fires (so the GM panel
            # sees an intent-vs-declared mismatch), but the soft_suggest path
            # is the only remaining structural reaction; reprompt severity now
            # downgrades to a soft suggest (preserved genre-pack compat —
            # packs declaring ``on_intent_mismatch: reprompt`` still validate;
            # the severity literal is unchanged in confrontation_intent_validator.py).
            # Engagement failure detection now lives in the router-driven
            # dispatch_engagement_watcher (one mechanism per problem).
            _effective_severity = _mismatch.severity
            if _effective_severity == "reprompt":
                _effective_severity = "soft_suggest"

            _classified_intent_value = _mismatch.matched_type

            from sidequest.telemetry.spans import confrontation_intent_mismatch_span

            with confrontation_intent_mismatch_span(
                matched_type=_mismatch.matched_type,
                declared_type=_mismatch.declared,
                severity=_effective_severity,
                matched_tokens=_mismatch.matched_tokens,
                reprompt_attempted=False,
            ):
                pass

            if _effective_severity == "soft_suggest":
                snapshot.next_turn_directives.append(
                    f"Last turn's intent suggested {_mismatch.matched_type}. "
                    f"If this scene is in fact a {_mismatch.matched_type}, open the "
                    f"encounter on this turn."
                )

        outcome.classified_intent = _classified_intent_value

        # (a) Narrator-initiated encounter creation — REMOVED Story 59-4 (ADR-113).
        # Confrontation engagement is now router-driven via the Intent Router
        # pre-narrator pass (``sidequest.server.intent_router_pass``) which
        # calls ``sidequest.agents.subsystems.confrontation.run_confrontation_dispatch``
        # against the canonical snapshot BEFORE narration_apply runs. The
        # field ``result.confrontation`` is no longer set on the SDK path
        # (the ``_assemble_turn_result_sdk`` lift was removed in the same
        # cutover) and the retired ``begin_confrontation`` tool no longer
        # exists. If a stale code path ever sets the field, this block being
        # absent is the load-bearing guard that prevents encounter creation
        # outside the router path — single mechanism per problem (memory
        # rule ``feedback_one_mechanism_per_problem``).

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

                if pack is None:
                    raise ValueError(
                        f"sealed-letter dogfight {enc.encounter_type!r} requires a pack "
                        "(SWN binding + frame stats) but pack is None"
                    )
                pc_char = next((c for c in snapshot.characters if c.core.name == player_name), None)
                if pc_char is None:
                    raise ValueError(
                        f"sealed-letter dogfight: PC {player_name!r} not found in "
                        "snapshot.characters"
                    )
                # PC attributes come from the real sheet; pilot_skill / attack_bonus
                # use the authored frame default (player_default_stats) for MVP —
                # the character model does not yet carry an SWN Pilot skill. This is
                # an authored default, not a silent fallback.
                pc_pilot_skill = int((cdef.player_default_stats or {}).get("pilot_skill", 0))
                pc_attack_bonus = int((cdef.player_default_stats or {}).get("attack_bonus", 0))

                # Story 114-15: the dogfight resolves its ship weapon from the
                # genre-tier ``ship_weapons`` collection (carried through the world
                # merge by resolve_inventory), NOT the personal item_catalog — a ship
                # weapon is native-subsystem config, kept off the personal-gear
                # surface. build_dogfight_weapon_lookup emits the weapon_resolved span.
                from sidequest.server.dispatch.inventory_resolve import resolve_inventory

                _dogfight_inventory = resolve_inventory(pack, snapshot.world_slug)
                shot_inputs, geo_mods = build_dogfight_shot_inputs(
                    ruleset_slug=pack.rules.ruleset,
                    cdef=cdef,
                    encounter=enc,
                    pc_stats=pc_char.stats,
                    pc_pilot_skill=pc_pilot_skill,
                    pc_attack_bonus=pc_attack_bonus,
                    weapon_lookup=build_dogfight_weapon_lookup(_dogfight_inventory),
                )

                sl_outcome = resolve_sealed_letter_lookup(
                    enc,
                    commits,
                    cdef.interaction_table,
                    geometry_modifiers=geo_mods,
                    shot_inputs=shot_inputs,
                    swn_cfg=pack.rules.swn,
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

                # Task 14: player shot deferred to Rapier throw; NPC shots held
                # server-side until player's die returns so all shots resolve
                # against pre-shot frame HP together. See _resolve_dogfight_shot_phase.
                outcome.pending_dogfight_shot = _resolve_dogfight_shot_phase(
                    snapshot=snapshot,
                    enc=enc,
                    sl_outcome=sl_outcome,
                )
                # Status-change processing further down still runs because
                # we only short-circuit the beat-selection block, not the
                # whole snapshot mutation phase.
                # Fall-through: skip beat loop by NOT defining beat_by_id
                # and gating the loop below.
                _legacy_beat_path = False
            elif cdef.resolution_mode == ResolutionMode.table_resolution:
                # ---- Free-for-all N-seat table branch (poker / auction) ----
                # EXCLUSIVE of apply_beat: table beats (fold/bet/cheat/...) are
                # NOT dial beats; falling through would double-apply. Each seat's
                # sealed action rides beat_selections (actor→seat, beat_id, amount,
                # target). Folded/out seats are dropped from the barrier elsewhere
                # (session_room). One decision point resolves per barrier turn.
                if enc.table_state is None:
                    raise ValueError(
                        f"confrontation {enc.encounter_type!r} declares "
                        "resolution_mode=table_resolution but encounter has no "
                        "table_state — cannot dispatch table resolution"
                    )
                if pack is None or pack.rules is None:
                    raise ValueError(
                        f"table_resolution {enc.encounter_type!r} requires a pack "
                        "(ruleset binding) but pack/rules is None"
                    )
                # Build the authored beat set once for validation + NPC policy.
                # Both the PC-commit validator (I1) and decide_npc_commit require
                # this set so the NPC policy can never emit an unauthored beat.
                authored_beats: set[str] = {b.id for b in cdef.beats}

                # Map each selection (actor name) → seat_id via the actor roster.
                # actor.role holds the seat_id for table encounters (set by
                # instantiate_table_encounter in encounter_lifecycle.py).
                # I1: validate that every PC commit names an authored beat BEFORE
                # building the TableCommit / before resolve_table mutates state.
                # Matching the legacy beat loop's fail-loud on unknown beat_id.
                table_commits: dict[str, TableCommit] = {}
                for sel in gated_selections:
                    actor = enc.find_actor(sel.actor)
                    if actor is None:
                        raise ValueError(
                            f"beat_selection actor {sel.actor!r} not found on "
                            f"table encounter {enc.encounter_type!r}"
                        )
                    if sel.beat_id not in authored_beats:
                        raise ValueError(
                            f"table beat {sel.beat_id!r} not authored for "
                            f"{enc.encounter_type!r} — authored: {sorted(authored_beats)}"
                        )
                    seat_id = actor.role  # role holds the seat_id for table encounters
                    table_commit = TableCommit(
                        seat_id=seat_id,
                        beat_id=sel.beat_id,
                        amount=int(sel.amount or 0),
                        target_seat=sel.target,
                    )
                    table_commits[seat_id] = table_commit
                    with table_commit_span(
                        seat=seat_id,
                        beat_id=sel.beat_id,
                        amount=table_commit.amount,
                        decision_point=enc.table_state.decision_point,
                    ):
                        pass

                # Auto-commit NPC seats with no PC selection this turn.
                # Only for active NPC seats not already committed.
                # Pass available_beats so the NPC policy is kind-general and
                # can never return a beat the confrontation did not author.
                from sidequest.game.table.npc_policy import decide_npc_commit  # noqa: PLC0415

                # Seeded from (interaction, decision_point) for reproducibility.
                # NOTE: full replay also requires replaying the NPC commit
                # sequence below — the shared Random advances once per NPC commit
                # (decide_npc_commit draws) before resolve_table consumes it, so
                # the seed alone is not a stable resolution key without the same
                # NPC iteration order.
                _table_rng = Random(
                    snapshot.turn_manager.interaction * 1000 + enc.table_state.decision_point
                )
                for _seat in enc.table_state.seats:
                    if _seat.is_pc or _seat.status != "active" or _seat.seat_id in table_commits:
                        continue
                    table_commits[_seat.seat_id] = decide_npc_commit(
                        enc.table_state, _seat, rng=_table_rng, available_beats=authored_beats
                    )

                module = get_ruleset_module(pack.rules.ruleset)
                # Snapshot seat statuses BEFORE resolve_table so the fold-mark
                # loop below can mark only seats that NEWLY transitioned to
                # "folded" this decision point — not every already-folded seat
                # (which would re-mark a seat that folded on an earlier DP of a
                # multi-decision-point hand).
                _pre_status = {s.seat_id: s.status for s in enc.table_state.seats}
                table_outcome = module.resolve_table(
                    enc.table_state, commits=table_commits, rng=_table_rng
                )
                # Part A (Task 15): fold-mark wiring.  After resolve_table mutates
                # seat statuses, call room.mark_table_folded for every PC seat that
                # JUST became "folded" this decision point so the barrier
                # denominator drops for the remaining decision points of this hand.
                # Build the reverse map (party_name → player_id) from
                # snapshot.player_seats once — this map is correct for any
                # multi-player snapshot.  NPC seats (is_pc=False) are skipped —
                # they never hold a barrier slot.  A folded PC whose party_name is
                # absent from the reverse map is a seating/data error; log it
                # loudly (No Silent Fallbacks) but do not crash the turn.
                if room is not None and enc.table_state is not None:
                    _pc_name_to_player_id: dict[str, str] = {
                        v: k for k, v in snapshot.player_seats.items() if v
                    }
                    for _tseat in enc.table_state.seats:
                        if (
                            _tseat.is_pc
                            and _tseat.status == "folded"
                            and _pre_status.get(_tseat.seat_id) != "folded"  # only NEWLY folded
                        ):
                            _pid = _pc_name_to_player_id.get(_tseat.party_name)
                            if _pid is not None:
                                room.mark_table_folded(_pid)
                            else:
                                logger.warning(
                                    "table.fold_mark_missing_player_id "
                                    "party_name=%r not in player_seats — "
                                    "solo/test path or seating mismatch; "
                                    "barrier denominator not adjusted for this seat",
                                    _tseat.party_name,
                                )
                if table_outcome.showdown:
                    enc.resolved = True
                    enc.outcome = f"table_winner:{table_outcome.resolved_winner}"
                    # Part A (Task 15): showdown teardown — clear the fold set so
                    # the room is ready for a fresh hand.  Must fire exactly once
                    # (on resolution), here and nowhere else in the table branch.
                    if room is not None:
                        room.clear_table_folds()
                    # Award the stake through the auditable state-patch path.
                    # Winner's party_name is the character/PC name.
                    # The engine always sets pot_awarded_to on showdown — assert
                    # narrows the type for pyright (invariant enforced by table engine).
                    assert table_outcome.pot_awarded_to is not None, (
                        "table showdown: engine must set pot_awarded_to when showdown=True"
                    )
                    winner_seat = enc.table_state.find_seat(table_outcome.pot_awarded_to)
                    if winner_seat is None:
                        # The engine always returns a valid pot_awarded_to among
                        # the seats — a miss here is an engine/state mismatch, not
                        # a recoverable state. Fail loud per No Silent Fallbacks.
                        raise ValueError(
                            f"table showdown: pot_awarded_to "
                            f"{table_outcome.pot_awarded_to!r} not found in "
                            "table_state.seats — engine/state mismatch"
                        )
                    # MVP proxy: award the abstract chip total as gold. The
                    # AUTHORITATIVE money prize is the content-declared stake
                    # value (Task 16: stake declaration in rules.yaml). Until
                    # then this approximates the prize from the abstract pot.
                    # TODO(task-16): source the money amount from the declared
                    # stake, not sum(contributions).
                    pot_total = sum(enc.table_state.pot.contributions.values())
                    outcome.table_pot_award = {
                        "recipient": winner_seat.party_name,
                        "stake_kind": table_outcome.stake_kind,
                        "stake_descriptor": table_outcome.stake_descriptor,
                        "amount": pot_total,
                    }
                    # Money stake: apply gold directly to the winner's character
                    # through the same auditable path used by gold_change above.
                    if table_outcome.stake_kind == "money":
                        winner_char = next(
                            (
                                c
                                for c in snapshot.characters
                                if c.core.name == winner_seat.party_name
                            ),
                            None,
                        )
                        if winner_char is None and winner_seat.is_pc:
                            # A PC won but no matching Character — a
                            # programming/seating error, not a valid state.
                            # Fail loud per No Silent Fallbacks.
                            raise ValueError(
                                f"table money award: PC winner "
                                f"{winner_seat.party_name!r} (seat "
                                f"{winner_seat.seat_id!r}) not found in "
                                "snapshot.characters"
                            )
                        # Apply the gold mutation only for a real PC Character
                        # with a positive pot. NPC winners (not in
                        # snapshot.characters) are an intentional gold-ledger
                        # no-op; a zero pot mutates nothing — but BOTH still emit
                        # the watcher event below so the GM panel never goes blind
                        # on a resolution.
                        before_gold: int | None = None
                        after_gold: int | None = None
                        if winner_char is not None:
                            before_gold = int(winner_char.core.inventory.gold)
                            after_gold = before_gold
                            if pot_total > 0:
                                after_gold = before_gold + pot_total
                                winner_char.core.inventory.gold = after_gold
                                logger.info(
                                    "economy.table_pot_award player=%s actor=%s "
                                    "turn=%d pot_total=%d before=%d after=%d",
                                    player_name,
                                    winner_seat.party_name,
                                    snapshot.turn_manager.interaction,
                                    pot_total,
                                    before_gold,
                                    after_gold,
                                )
                        _watcher_publish(
                            "state_transition",
                            {
                                "kind": "economy.table_pot_award",
                                "actor": winner_seat.party_name,
                                "pot_total": pot_total,
                                "stake_kind": table_outcome.stake_kind,
                                "stake_descriptor": table_outcome.stake_descriptor,
                                "before": before_gold,
                                "after": after_gold,
                                "npc_winner": not winner_seat.is_pc,
                                "turn_number": snapshot.turn_manager.interaction,
                                "player_name": player_name,
                            },
                            component="economy",
                        )
                    # Non-money stakes (item/information/favor): the award is
                    # recorded on outcome.table_pot_award (above) + the narrator
                    # hint (below). Full inventory transfer is deferred to
                    # content-driven handling (Task 16+).
                    snapshot.pending_resolution_signal = _build_resolution_signal(enc)
                if table_outcome.narration_hint:
                    enc.narrator_hints = [table_outcome.narration_hint]
                else:
                    enc.narrator_hints = []
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
                        # ADR-114 / Task 11 — damage roll injection on opposed path.
                        pack=pack,
                        room_broadcast=room.broadcast if room is not None else None,
                        rolling_player_id=acting_character_name or player_name,
                        session_round=snapshot.turn_manager.round,
                    )
                    if outcome_obj.encounter_resolved:
                        snapshot.pending_resolution_signal = _build_resolution_signal(enc)
                _legacy_beat_path = False
            elif cdef.resolution_mode == ResolutionMode.contest:
                # ---- Fate Contest branch (Westley major M1, spec 2026-06-17 §2) ----
                # A Contest resolves EXCLUSIVELY through FATE_ACTION (the 4dF
                # exchange engine in fate_contest.run_fate_contest_exchange). The
                # legacy apply_beat dial engine MUST NEVER resolve a contest, or it
                # would run in PARALLEL to the Contest engine — layering the dial on
                # top of the SRD binding, the exact failure ADR-144 forbids (REPLACE,
                # not layer). Content fix (b) strips the dial-beat scaffolding from
                # the converted contest defs so the narrator can't legitimately
                # select a contest beat — but the narrator is an LLM and could still
                # hallucinate a stray ``beat_selection`` against a live contest.
                # Drop those selections and surface the block LOUDLY on the GM panel
                # (No Silent Fallbacks: the dial engine was actively prevented from
                # resolving a contest, and we record that decision). The player's
                # turn does not error — the Contest still resolves via FATE_ACTION.
                if gated_selections:
                    for sel in gated_selections:
                        _watcher_publish(
                            "state_transition",
                            {
                                "field": "encounter",
                                "op": "contest_beat_dropped_dial_blocked",
                                "actor": sel.actor,
                                "beat_id": sel.beat_id,
                                "encounter_type": enc.encounter_type,
                                "reason": "contest resolves only via FATE_ACTION (4dF)",
                            },
                            component="confrontation",
                            severity="warning",
                        )
                    logger.warning(
                        "encounter.contest_beat_dropped_dial_blocked "
                        "encounter=%r dropped %d stray beat selection(s) — a Fate "
                        "Contest resolves only via FATE_ACTION; the legacy dial "
                        "apply_beat engine was blocked (ADR-144 REPLACE)",
                        enc.encounter_type,
                        len(gated_selections),
                    )
                _legacy_beat_path = False
            else:
                _legacy_beat_path = True
                beat_by_id = {b.id: b for b in cdef.beats}
        else:
            _legacy_beat_path = False
            # RW-2 loud-stash guard (playtest 2026-06-05, the_circuit chase):
            # the player rolled (DICE_THROW stashed a pending d20 for the
            # opposed resolver) but ZERO beat selections survived this
            # narration turn — the resolver above never ran, and the session
            # handler clears the stash right after this apply returns. That
            # discard was SILENT, which is how the entire opposed_check
            # engine died unnoticed on the SDK path (beat_selections zeroed
            # every turn → every player roll vanished). Surface it loudly on
            # the GM panel before the stash is lost (No Silent Fallbacks).
            if enc is not None and not enc.resolved and opposed_player_d20 is not None:
                _pending_cdef = find_confrontation_def(
                    pack.rules.confrontations if pack is not None and pack.rules else [],
                    enc.encounter_type,
                )
                if (
                    _pending_cdef is not None
                    and _pending_cdef.resolution_mode == ResolutionMode.opposed_check
                ):
                    logger.warning(
                        "encounter.opposed_check_pending_roll_unconsumed "
                        "encounter=%r player_d20=%d player_beat_id=%r — no beat "
                        "selections survived the narration turn; the stashed "
                        "player roll will be cleared unconsumed",
                        enc.encounter_type,
                        opposed_player_d20,
                        opposed_player_beat_id,
                    )
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "encounter",
                            "op": "opposed_check_pending_roll_unconsumed",
                            "encounter_type": enc.encounter_type,
                            "player_d20": opposed_player_d20,
                            "player_beat_id": opposed_player_beat_id or "",
                            "player_actor": opposed_player_actor or "",
                            "raw_selection_count": len(result.beat_selections),
                        },
                        component="encounter",
                        severity="warning",
                    )

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
                    if pack and pack.rules and pack.rules.ruleset == "wwn":
                        # WWN Content Plan 3 Task 7 — route to the WWN cast spine
                        # (damage + downed seam). cdef/enc are in local scope here
                        # (the legacy beat loop binds them above).
                        _resolve_wwn_cast_for_beat(
                            sel=sel,
                            actor=actor,
                            snapshot=snapshot,
                            pack=pack,
                            encounter=enc,
                            cdef=cdef,
                        )
                    else:
                        _resolve_innate_cast_for_beat(
                            sel=sel,
                            actor=actor,
                            snapshot=snapshot,
                        )

                # ─── Story 102-7: AWN mutation resolution (Plan 2 §6.3) ────
                # A beat carrying the mutation_resolution marker routes
                # through the mutation engine (Strain, usage limits, save-vs)
                # via the BeatSelection.mutation_id sidecar — the cast_spell
                # pattern retold for the pack's marquee mechanic. The marker
                # is the route (a stray mutation_id on an unmarked beat is
                # ignored); every miss inside is a loud awn.mutation.refused.
                if getattr(beat, "mutation_resolution", False):
                    _resolve_mutation_for_beat(
                        sel=sel,
                        actor=actor,
                        snapshot=snapshot,
                        pack=pack,
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
                # Story 84-2 (WI-5): the promotion turn's narration is the epithet
                # source for alias accretion when this status promotes a pool member.
                narration_text=result.narration or "",
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

    # ADR-116 §4 — end-on-no-Other. A confrontation ends when its last live
    # opponent leaves, not only when a dial hits threshold.
    _resolve_if_no_opponent_remains(snapshot)

    # Playtest 2026-06-01 (the_real_mccoy standoff): post-turn dial-threshold
    # sweep. ``advance_confrontation`` moves a dial without touching
    # ``beat``/``structured_phase``/resolution (only ``advance_encounter_beat``→
    # ``apply_beat`` does that), so a confrontation the narrator drove with the
    # dial tool alone heats past threshold while frozen in ``Setup`` and never
    # resolves. This sweep runs every turn after ALL tool calls, so it catches
    # the final dial regardless of which tool moved it.
    _resolve_dial_threshold_and_phase(snapshot)

    return outcome


# Dramatic-arc order for forward-only phase advancement (never regress).
_PHASE_ORDER: tuple[str, ...] = (
    "Setup",
    "Opening",
    "Escalation",
    "Climax",
    "Resolution",
)


def _phase_for_dial_progress(enc: object) -> EncounterPhase:
    """Map the hottest dial's progress toward threshold onto the dramatic arc.

    The encounter is as advanced as its hottest dial. Progress is
    ``(current - starting) / (threshold - starting)`` per metric, clamped at
    zero (negative "regroup" deltas don't push the phase below Setup). At/above
    1.0 the resolution branch owns the transition; this helper only covers the
    sub-threshold band.
    """
    from sidequest.game.encounter import EncounterPhase

    def _ratio(metric: object) -> float:
        span = metric.threshold - metric.starting  # type: ignore[attr-defined]
        if span <= 0:
            return 0.0
        return max(0.0, (metric.current - metric.starting) / span)  # type: ignore[attr-defined]

    ratio = max(_ratio(enc.player_metric), _ratio(enc.opponent_metric))  # type: ignore[attr-defined]
    if ratio <= 0.0:
        return EncounterPhase.Setup
    if ratio < 0.34:
        return EncounterPhase.Opening
    if ratio < 0.67:
        return EncounterPhase.Escalation
    if ratio < 1.0:
        return EncounterPhase.Climax
    return EncounterPhase.Resolution


def _resolve_dial_threshold_and_phase(snapshot: GameSnapshot) -> None:
    """Post-turn resolution + phase sweep for ``dial_threshold`` confrontations.

    Mirrors the ``current >= threshold`` resolution branch already in
    ``apply_beat`` (beat_kinds.py), but runs from the narration-apply pipeline
    so it fires no matter which tool moved the dial — closing the wedge where a
    standoff driven purely by ``advance_confrontation`` heats past threshold and
    never resolves.

    * At/over threshold → ``resolved`` + ``player_victory``/``opponent_victory``
      (player first, matching apply_beat's tie-break) + ``Resolution``. Emits
      ``encounter.resolved`` (source=``dial_threshold_sweep``) and stamps the
      resolution signal so the narrator's next frame sees the close.
    * Below threshold → advance ``structured_phase`` FORWARD to track dial heat
      (never regress past a beat-set phase), so the GM panel and mechanics-first
      players see the standoff progressing instead of frozen in Setup. Emits a
      ``state_transition`` watcher event on a real transition.

    Gated to ``win_condition == "dial_threshold"``: ``hp_depletion`` dials are
    synthesized inert (threshold ~1e6) and resolve on the HP path only;
    ``table_showdown`` reads ``table_state``, not the metrics.
    """
    from sidequest.game.encounter import EncounterPhase
    from sidequest.telemetry.spans import encounter_resolved_span

    enc = getattr(snapshot, "encounter", None)
    if enc is None or enc.resolved:
        return
    if enc.win_condition != "dial_threshold" or enc.table_state is not None:
        return

    # Resolution at threshold — player crossing wins the tie-break, matching
    # apply_beat (sealed-letter order places player beats first).
    if enc.player_metric.current >= enc.player_metric.threshold:
        outcome = "player_victory"
    elif enc.opponent_metric.current >= enc.opponent_metric.threshold:
        outcome = "opponent_victory"
    else:
        outcome = None

    if outcome is not None:
        enc.resolved = True
        enc.outcome = outcome
        enc.structured_phase = EncounterPhase.Resolution
        snapshot.pending_resolution_signal = _build_resolution_signal(enc)
        with encounter_resolved_span(
            encounter_type=enc.encounter_type,
            outcome=outcome,
            source="dial_threshold_sweep",
            player_metric=enc.player_metric.current,
            opponent_metric=enc.opponent_metric.current,
        ):
            pass
        return

    # Below threshold: advance the phase forward to reflect dial heat.
    derived = _phase_for_dial_progress(enc)
    current = enc.structured_phase or EncounterPhase.Setup
    if _PHASE_ORDER.index(str(derived)) > _PHASE_ORDER.index(str(current)):
        enc.structured_phase = derived
        _watcher_publish(
            "state_transition",
            {
                "field": "encounter.structured_phase",
                "op": "advanced",
                "from": str(current),
                "to": str(derived),
                "player_metric": enc.player_metric.current,
                "opponent_metric": enc.opponent_metric.current,
                "rationale": (
                    "dial-threshold confrontation phase derived from dial heat "
                    "(advance_confrontation moved the dial without a beat); "
                    "phase tracks progress toward threshold"
                ),
            },
            component="encounter",
            severity="info",
        )


def _resolve_opponent_yield(
    snapshot: GameSnapshot,
    enc: StructuredEncounter,
    *,
    trigger: str,
    turn_number: int,
) -> None:
    """Resolve a confrontation as a player VICTORY because the OPPONENT yielded
    (Story 59-31). Shared by the post-turn sweep and the location-change
    boundary so both record the same engine-checked outcome.

    Records ``enc.outcome = "opponent_yielded"`` — the narrative-precise label,
    sibling to the player-side ``yielded`` (a loss) — which RESOLVES AS
    ``player_victory`` for reward/credit (the OTEL ``outcome`` attr). Stamps
    ``pending_resolution_signal`` (cheap, correct — matches the dial-threshold
    sweep) so a future 49-5 [ENCOUNTER RESOLVED] revival narrates the close for
    free. Emits a ``component="confrontation"`` watcher event so Keith can
    confirm on the GM panel that the ENGINE — not the narrator's prose —
    recorded the victory (CLAUDE.md OTEL principle).
    """
    from sidequest.game.encounter import EncounterPhase
    from sidequest.game.resolution_signal import ResolutionSignal

    opponents = [a for a in enc.actors if a.side == "opponent"]
    withdrawn_names = [a.name for a in opponents if a.withdrawn]
    # Prefer the actually-withdrawn opponents; fall back to all opponents when
    # the yield came via opponents_disposition (surrender/rout) without
    # per-actor withdrawn flags.
    yielded_opponents = withdrawn_names or [a.name for a in opponents]

    from sidequest.telemetry.spans import encounter_resolution_signal_emitted_span

    enc.resolved = True
    enc.outcome = "opponent_yielded"
    enc.structured_phase = EncounterPhase.Resolution
    # Story 59-33: derive yield_side from the outcome (never hand-set) →
    # "opponent" for an opponent yield.
    yield_side = yield_side_for(enc.outcome)
    snapshot.pending_resolution_signal = ResolutionSignal(
        encounter_type=enc.encounter_type,
        outcome="opponent_yielded",
        final_player_metric=enc.player_metric.current,
        final_opponent_metric=enc.opponent_metric.current,
        yielded_actors=tuple(yielded_opponents),
        edge_refreshed=0,
        yield_side=yield_side,
    )
    # Story 59-33: emit the resolution-signal span (the live GM-panel consumer)
    # carrying yield_side — this inline opponent-yield builder does not route
    # through _build_resolution_signal, so it emits its own span.
    with encounter_resolution_signal_emitted_span(
        outcome="opponent_yielded",
        final_player_metric=enc.player_metric.current,
        final_opponent_metric=enc.opponent_metric.current,
        yield_side=yield_side,
    ):
        pass
    # Story 59-32: the credit-victory ``outcome`` attr is DERIVED through the
    # shared classifier from the mechanical-truth label the engine just set
    # (``enc.outcome == "opponent_yielded"``), not hardcoded. The classifier is
    # the single source of truth for "does this outcome credit a victory?" — so a
    # future relabel is mapped once, in one place. Module-level ``is_player_victory``
    # name (not module-qualified) keeps the monkeypatch seam the wiring test uses.
    credit_outcome = "player_victory" if is_player_victory(enc.outcome) else enc.outcome
    _watcher_publish(
        "confrontation_resolved_on_opponent_yield",
        {
            "encounter_type": enc.encounter_type,
            "outcome": credit_outcome,
            "resolution_label": "opponent_yielded",
            "trigger": trigger,
            "yielded_opponents": yielded_opponents,
            "opponents_disposition": enc.opponents_disposition,
            "turn_number": turn_number,
        },
        component="confrontation",
    )


def _resolve_if_no_opponent_remains(snapshot: GameSnapshot) -> None:
    """ADR-116 §4 — end-on-no-Other, recorded as an opponent YIELD (Story 59-31).

    If an active confrontation has seated opponents who have yielded — every
    opponent ``withdrawn``, OR ``opponents_disposition`` is a yield disposition
    (``surrendered``/``routed``) — resolve it as a player victory. A
    confrontation ends when there is no longer a live Other, not only when a
    dial reaches threshold. Emits ``participant.left`` per actor carrying the
    ``withdrawn`` flag; when the yield came ONLY via ``opponents_disposition``
    (surrender/rout with no per-actor ``withdrawn``) the loop produces no spans
    — in that case the WHY is conveyed entirely by the downstream
    ``confrontation_resolved_on_opponent_yield`` event. Either way it routes
    through ``_resolve_opponent_yield`` for the ``opponent_yielded``/
    player_victory outcome, the resolution-signal stamp, and that OTEL event.

    Supersedes the prior ``opponent_withdrew`` label (Story 59-31): a yielded
    opponent is a player VICTORY, never the player-side ``yielded`` (loss) nor a
    bare withdrawal with no credit.
    """
    from sidequest.telemetry.spans import participant_left_span

    enc = getattr(snapshot, "encounter", None)
    if enc is None or enc.resolved:
        return
    # No Other → nothing to resolve here (ADR-116). Cheap early-out before the
    # yield check so a confrontation with no seated opponent never reaches the
    # victory path.
    opponents = [a for a in enc.actors if a.side == "opponent"]
    if not opponents:
        return
    if enc.opponent_yield_outcome() is None:
        return
    for opp in (a for a in opponents if a.withdrawn):
        with participant_left_span(
            encounter_type=enc.encounter_type,
            name=opp.name,
            side="opponent",
            reason="withdrawn",
        ):
            pass
    _resolve_opponent_yield(
        snapshot,
        enc,
        trigger="opponent_yield_sweep",
        turn_number=snapshot.turn_manager.interaction,
    )


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
    from sidequest.telemetry.spans import encounter_resolution_signal_emitted_span

    outcome = enc.outcome or ""
    # Story 59-33: yield_side is DERIVED from the outcome (never hand-set) so the
    # factory cannot mislabel a surrender/rout/opponent_yielded resolution. None
    # for non-yield resolutions (dial wins, abandonment, etc.).
    yield_side = yield_side_for(outcome)
    # spec 2026-06-17 §2/§5 (Westley major M3): on the Fate Contest path the dial
    # metrics are seeded then NEVER advanced — the contest engine only touches
    # ``enc.contest.player_victories``/``opponent_victories``. Reading the frozen
    # ``*_metric.current`` would report the start value to the narrator + GM panel
    # (the lie detector would lie). Source the final metrics from the live victory
    # tally on the contest path; dial/opposed encounters keep reading the metric.
    contest = getattr(enc, "contest", None)
    if contest is not None:
        final_player_metric = contest.player_victories
        final_opponent_metric = contest.opponent_victories
    else:
        final_player_metric = enc.player_metric.current
        final_opponent_metric = enc.opponent_metric.current
    signal = ResolutionSignal(
        encounter_type=enc.encounter_type,
        outcome=outcome,
        final_player_metric=final_player_metric,
        final_opponent_metric=final_opponent_metric,
        yielded_actors=tuple(),
        edge_refreshed=0,
        yield_side=yield_side,
    )
    # Story 59-33: the live GM-panel consumer (49-5 narrator consume is dormant).
    # Emit the resolution-signal span carrying yield_side so the lie-detector can
    # confirm which side yielded at every factory-built (dial/threshold) resolution.
    with encounter_resolution_signal_emitted_span(
        outcome=outcome,
        final_player_metric=final_player_metric,
        final_opponent_metric=final_opponent_metric,
        yield_side=yield_side,
    ):
        pass
    return signal


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


def _resolve_dogfight_shot_phase(
    *,
    snapshot: Any,
    enc: Any,
    sl_outcome: Any,
) -> PendingDogfightShot | None:
    """Resolve or stash dogfight shots from a sealed-letter cell outcome.

    Called after ``resolve_sealed_letter_lookup`` when the cell yields gun
    solutions. Splits the solutions into player vs NPC:

    - NPC shots are server-rolled immediately and held.
    - If the player has a gun solution, a ``PendingDogfightShot`` is returned
      — the caller stashes it on ``_SessionData`` and emits a DiceRequest so
      the client throws the real Rapier die.
    - If only the NPC has a gun solution, it resolves inline (Task 13 path)
      and returns None.
    - If there are no gun solutions at all, returns None immediately.

    Invariant: ALL gun solutions in the cell are stored in the returned
    PendingDogfightShot so the DICE_THROW handler can resolve all shots
    (player + NPC) against the same pre-shot frame HP in one pass.
    """
    if not sl_outcome.gun_solutions:
        return None

    # Determine the player actor's role from the encounter's actor side map.
    pc_role: str | None = next(
        (a.role for a in enc.actors if a.side == "player"),
        None,
    )
    if pc_role is None:
        # No player actor seated — unreachable in a real dogfight (the PC is
        # always seated at instantiation). Fail soft: no player shot to defer.
        return None

    npc_solos: list[GunSolution] = [
        gs for gs in sl_outcome.gun_solutions if gs.shooter_role != pc_role
    ]
    player_solos: list[GunSolution] = [
        gs for gs in sl_outcome.gun_solutions if gs.shooter_role == pc_role
    ]

    # Server-roll and hold NPC d20s immediately — they're revealed together
    # with the player's roll so all shots resolve against the same frame HP.
    npc_d20s: dict[str, int] = {gs.shooter_role: _roll_d20_server_side() for gs in npc_solos}

    if player_solos:
        # Player has a gun solution — stash and wait for the Rapier throw.
        pc_gs = player_solos[0]
        pc_actor_name = next(
            (a.name for a in enc.actors if a.role == pc_role),
            pc_gs.shooter_name,
        )
        return PendingDogfightShot(
            gun_solutions=list(sl_outcome.gun_solutions),
            held_npc_d20s=npc_d20s,
            player_shooter_role=pc_role,
            player_modifier=pc_gs.attack.modifier,
            player_target_number=pc_gs.attack.target_number,
            player_actor_name=pc_actor_name,
        )

    # NPC-only: resolve inline (Task 13 path, no stash needed).
    shot_res = resolve_dogfight_shots(
        encounter=enc,
        gun_solutions=npc_solos,
        d20_by_shooter=npc_d20s,
        edge_resolver=frame_hp_resolver(enc),
    )
    if shot_res.depletion is not None:
        snapshot.pending_resolution_signal = _build_resolution_signal(enc)
    return None


def _opposed_dc(beat: Any) -> int:
    """Per-side DC derived from beat ``base`` magnitude, clamped 10..=30.

    Mirrors ``sidequest.game.ruleset.dial.DialRulesetModule.compute_dc``
    so a player using the dispatch path and an opponent using this resolver
    land on the same DC for the same beat.
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
    pack: GenrePack | None = None,
    room_broadcast: Any = None,
    rolling_player_id: str = "server",
    session_round: int = 0,
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

    ADR-114 / Task 11 — ``pack``, ``room_broadcast``, ``rolling_player_id``,
    and ``session_round`` enable strike-beat HP damage on the opposed path.
    When ``pack`` is provided and a strike beat lands (tier not Fail/CritFail),
    the damage spec is resolved, server-side faces are rolled, and a
    DICE_REQUEST + DICE_RESULT pair is broadcast before ``apply_beat`` so the
    player overlay shows the weapon dice (Sebastien's "show me the math").
    If no DamageSpec resolves, we log + skip (no phantom damage).
    """
    import uuid

    from sidequest.game.beat_kinds import apply_beat
    from sidequest.game.dice import generate_dice_seed, resolve_dice_with_faces
    from sidequest.game.opposed_check import resolve_opponent_modifier, resolve_opposed_check
    from sidequest.protocol.dice import DiceResultPayload as _DiceResultPayload
    from sidequest.protocol.dice import RollOutcome as _DmgRollOutcome
    from sidequest.protocol.dice import ThrowParams as _DmgThrowParams
    from sidequest.server.dispatch.damage_roll import (
        damage_request_from_spec as _damage_request_from_spec,
    )
    from sidequest.server.dispatch.damage_roll import (
        generate_server_faces as _gen_server_faces,
    )
    from sidequest.server.dispatch.damage_roll import (
        parity_damage_total as _parity_damage_total,
    )
    from sidequest.server.dispatch.damage_roll import (
        resolve_damage_spec_from_beat_and_actor as _resolve_dmg_spec,
    )
    from sidequest.telemetry.spans import (
        encounter_beat_skipped_span,
        encounter_opposed_roll_resolved_span,
        encounter_resolved_span,
    )

    _FAIL_TIERS = frozenset([RollOutcome.Fail, RollOutcome.CritFail])

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
        # ADR-114 / Task 11 — strike-beat HP damage on the opposed path.
        # Mirror the exact logic from dice.py's non-opposed branch: for each
        # actor whose beat has damage_channel=strike AND whose tier is not
        # Fail/CritFail, resolve the DamageSpec, roll server-side faces,
        # broadcast DICE_REQUEST + DICE_RESULT, and pass damage_resolver to
        # apply_beat so apply_beat_hp_channel fires. No phantom damage —
        # if no DamageSpec resolves, log + skip (same behaviour as dice.py).
        damage_resolver_fn = None
        damage_channel = str(getattr(sel_beat, "damage_channel", "none") or "none")
        if pack is not None and damage_channel == "strike" and sel_tier not in _FAIL_TIERS:
            actor_core = snapshot.find_creature_core(sel_actor.name)
            dmg_spec = _resolve_dmg_spec(
                beat=sel_beat,
                actor_core=actor_core,
                pack=pack,
                world_slug=snapshot.world_slug,
            )
            if dmg_spec is None:
                logger.warning(
                    "opposed_check.damage_spec_missing beat=%r actor=%r encounter=%r "
                    "— strike beat has no resolvable weapon, damage_override, or "
                    "unarmed default; HP damage skipped (CLAUDE.md no-fabricate)",
                    beat_id,
                    sel_actor.name,
                    encounter.encounter_type,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "encounter",
                        "op": "damage_spec_missing",
                        "beat_id": beat_id,
                        "actor": sel_actor.name,
                        "source": sel_source,
                        "rationale": (
                            "opposed_check strike beat has no damage_override, "
                            "no weapon with a damage spec in inventory, and no "
                            "unarmed_damage default on the genre rules — HP path skipped"
                        ),
                    },
                    component="encounter",
                    severity="warning",
                )
            else:
                # Lie-detector: span when the strike fell back to the genre
                # unarmed floor (no weapon/override/catalog). Identity match —
                # the resolver returns the exact pack.rules.unarmed_damage object.
                _unarmed_floor = pack.rules.unarmed_damage if pack.rules else None
                if _unarmed_floor is not None and dmg_spec is _unarmed_floor:
                    _watcher_publish(
                        "state_transition",
                        {
                            "field": "encounter",
                            "op": "unarmed_strike_floor",
                            "beat_id": beat_id,
                            "actor": sel_actor.name,
                            "source": sel_source,
                            "dice": dmg_spec.dice,
                            "rationale": (
                                "opposed_check strike resolved no weapon/override/"
                                "catalog damage; fell back to pack.rules.unarmed_damage"
                            ),
                        },
                        component="encounter",
                        severity="info",
                    )
                dmg_request_id = str(uuid.uuid4())
                dmg_request_payload = _damage_request_from_spec(
                    dmg_spec,
                    request_id=dmg_request_id,
                    rolling_player_id=rolling_player_id,
                    character_name=sel_actor.name,
                )
                dmg_faces = _gen_server_faces(dmg_request_payload.dice)
                dmg_resolved = resolve_dice_with_faces(
                    dmg_request_payload.dice,
                    dmg_faces,
                    dmg_request_payload.modifier,
                    dmg_request_payload.difficulty,
                )
                # Parity (d2) spec threw a backing d6; map faces to d2 values for
                # the HP total (see dice.py for the rationale). rolls keep the d6.
                if dmg_spec.is_parity_die:
                    dmg_total = _parity_damage_total(dmg_faces, dmg_request_payload.modifier)
                else:
                    dmg_total = dmg_resolved.total
                dmg_seed = generate_dice_seed(
                    f"{encounter.encounter_type}-{sel_actor.name}", session_round
                )
                dmg_result_payload = _DiceResultPayload(
                    request_id=dmg_request_id,
                    rolling_player_id=rolling_player_id,
                    character_name=sel_actor.name,
                    rolls=dmg_resolved.rolls,
                    modifier=dmg_request_payload.modifier,
                    total=dmg_total,
                    difficulty=dmg_request_payload.difficulty,
                    outcome=_DmgRollOutcome.Success,  # damage rolls have no outcome tier
                    seed=dmg_seed,
                    throw_params=_DmgThrowParams(
                        velocity=(0.0, 4.0, -1.0),
                        angular=(0.5, 0.5, 0.5),
                        position=(0.5, 0.5),
                    ),
                    # Follow-on weapon roll — tag it so the UI overlay treats it
                    # as a damage readout, not the primary check (the request
                    # from _damage_request_from_spec is already roll_role=damage).
                    roll_role="damage",
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "encounter",
                        "op": "damage_roll_resolved",
                        "beat_id": beat_id,
                        "actor": sel_actor.name,
                        "actor_side": sel_actor.side,
                        "damage_spec": dmg_spec.dice,
                        "bonus": dmg_spec.bonus,
                        "faces": dmg_faces,
                        "total": dmg_total,
                        "source": sel_source,
                    },
                    component="encounter",
                )
                if room_broadcast is not None:
                    room_broadcast(
                        DiceRequestMessage(payload=dmg_request_payload, player_id="server")
                    )
                    room_broadcast(
                        DiceResultMessage(payload=dmg_result_payload, player_id="server")
                    )
                damage_resolver_fn = lambda _t=dmg_total: _t  # noqa: E731

        applied = apply_beat(
            encounter,
            sel_actor,
            sel_beat,
            sel_tier,
            turn=turn,
            edge_resolver=snapshot.find_creature_core,
            damage_resolver=damage_resolver_fn,
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

    # Story 72-12: presence stamp — the opposed_check opponent was a SEATED
    # participant this turn (it rolled its own d20 against the player), even
    # when the narrator never name-dropped it in ``npcs_present`` prose. 72-8
    # closed this gap for the COMBAT seams only (gated behind
    # ``cdef.category == "combat"`` in encounter_lifecycle); a social duellist
    # seated via ``opposed_check`` went un-stamped and 72-6's last-seen prune
    # would read an actively-duelling NPC as stale. Reuse 72-8's shared stamp
    # primitive + the ``npc.edge_published`` span family — NOT the whole
    # ``_publish_combat_edge_to_npcs``, which would overwrite the social
    # opponent's ``core.hp`` from the dial (a combat-only side effect).
    from sidequest.server.dispatch.encounter_lifecycle import _stamp_encounter_presence
    from sidequest.telemetry.spans import npc_edge_published_span

    opp_npc = next((n for n in snapshot.npcs if n.core.name == opponent_actor.name), None)
    if opp_npc is not None:
        # Same location accessor the prose path and the combat seams use; ``None``
        # when the seat has no resolved location, in which case the primitive
        # stamps the turn only and freezes the location (No Silent Fallbacks).
        actor_loc = snapshot.party_location(perspective=player_actor.name)
        _stamp_encounter_presence(opp_npc, turn=turn, location=actor_loc)
        # GM-panel pool view: invert the (ascending) opponent dial into a
        # descending "current > 0 = still in the duel" read, the same convention
        # the dial combat seam uses. Fall back to the NPC's own pool only when
        # the encounter carries no opponent metric.
        _om = encounter.opponent_metric
        _thresh = int(getattr(_om, "threshold", 0) or 0) if _om is not None else 0
        if _thresh > 0:
            _span_max = _thresh
            _span_current = max(1, _thresh - int(getattr(_om, "current", 0) or 0))
        else:
            _span_max = opp_npc.core.hp.max
            _span_current = opp_npc.core.hp.current
        with npc_edge_published_span(
            npc_name=opp_npc.core.name,
            current=_span_current,
            max=_span_max,
            source="opposed_check",
            turn_number=turn,
            last_seen_turn=opp_npc.last_seen_turn,
            last_seen_location=opp_npc.last_seen_location or "",
        ):
            pass

    return _OpposedBranchOutcome(encounter_resolved=encounter_resolved)


# ---------------------------------------------------------------------------
# Story 45-20 — trope resolution handshake.
# ---------------------------------------------------------------------------

# ``_ACTIVE_STAKES_GUARDRAIL`` (the ~1024-char soft cap for ``active_stakes``)
# moved to sidequest.game.session in Story 77-2 to break an import cycle; it is
# imported at the top of this module and re-exported here for callers/tests.


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
                # Story 77-2: trope-resolution status-only entry under the
                # widened QuestEntry type.
                upsert_quest_status(snapshot.quest_log, key, status_text)
            logger.info(
                "trope.resolution_handshake fresh_writes=%d player=%s turn=%d",
                len(fresh_writes),
                player_name,
                interaction,
            )
