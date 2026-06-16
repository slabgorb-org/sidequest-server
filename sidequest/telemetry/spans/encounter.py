"""Encounter spans — phase transitions, beats, dual-track momentum, yield."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute
from .span import Span

# Routed encounter spans
SPAN_ENCOUNTER_PHASE_TRANSITION = "encounter.phase_transition"
SPAN_ROUTES[SPAN_ENCOUNTER_PHASE_TRANSITION] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.phase_transition",
        # Emission site uses keys "from" and "to".
        "from_phase": (span.attributes or {}).get("from", ""),
        "to_phase": (span.attributes or {}).get("to", ""),
    },
)
SPAN_ENCOUNTER_RESOLVED = "encounter.resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.resolved",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "outcome": (span.attributes or {}).get("outcome", ""),
        "source": (span.attributes or {}).get("source", ""),
    },
)
# EH-2 burning_peace playtest (2026-06-05): the lie-detector for "did anything
# handle the 0-HP exit". When a confrontation resolves against the PC
# (opponent_victory / mutual_destruction) with the PC at 0 HP, the post-resolution
# lethality seam applies the genre policy's mechanical consequence and emits this
# span. ``decision`` is non_lethal_recover | lethal_down | no_policy; the GM panel
# reads hp_before/hp_after to confirm the PC was not left parked at 0/10.
SPAN_POST_RESOLUTION_LETHALITY = "encounter.post_resolution_lethality"
SPAN_ROUTES[SPAN_POST_RESOLUTION_LETHALITY] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.post_resolution_lethality",
        "decision": (span.attributes or {}).get("decision", ""),
        "outcome": (span.attributes or {}).get("outcome", ""),
        "verdict": (span.attributes or {}).get("verdict", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "hp_before": (span.attributes or {}).get("hp_before", -1),
        "hp_after": (span.attributes or {}).get("hp_after", -1),
    },
)
# sq-playtest 2026-06-07 (barsoom-3, blocking): the turn-intake gate refused a
# downed PC's action (the PC carries an ``incapacitating`` status). Missing span
# where a dead PC kept submitting → the gate isn't wired and the dead-man-walking
# is back. ``character`` is the downed PC; ``verdict`` is the lethality verdict.
SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED = "session.player_action_blocked_incapacitated"
SPAN_ROUTES[SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "session.player_action_blocked_incapacitated",
        "character": (span.attributes or {}).get("character", ""),
        "verdict": (span.attributes or {}).get("verdict", ""),
        "status_text": (span.attributes or {}).get("status_text", ""),
    },
)
# SWN P4 initiative spine: the engine rolls initiative and seats the turn
# order at instantiation. This span is the GM-panel polygraph proving the
# order is engine-rolled (not narrator improv). ``initiative_order`` is the
# human-readable order string (e.g. ``"Rux(9), Raider(5)"``); ``source``
# distinguishes the emission site (e.g. ``"instantiate"``).
SPAN_ENCOUNTER_INITIATIVE_ROLLED = "encounter.initiative_rolled"
SPAN_ROUTES[SPAN_ENCOUNTER_INITIATIVE_ROLLED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.initiative_rolled",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "initiative_order": (span.attributes or {}).get("initiative_order", ""),
        "source": (span.attributes or {}).get("source", ""),
    },
)
SPAN_ENCOUNTER_BEAT_APPLIED = "encounter.beat_applied"
SPAN_ROUTES[SPAN_ENCOUNTER_BEAT_APPLIED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.beat_applied",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "metric_delta": (span.attributes or {}).get("metric_delta", 0),
    },
)
# Story 71-28: encounter-beat advance lie-detector. The advance_encounter_beat
# WRITE tool sets tool.encounter.* attributes on its dispatch span
# (tool.write.advance_encounter_beat), but that span name is *constructed
# dynamically* by tool_dispatch_span (tool.{cat}.{name}), so it never appeared
# as a SPAN_* constant and escaped the routing-completeness lint — the watcher
# emitted only agent_span_close and the GM panel's state_transition tab was
# half-blind to whether beats were advancing or stuck. Binding it to a SPAN_*
# constant here both routes it to a typed state_transition AND brings it under
# the lint going forward (test_routing_completeness). Mirrors the beat-related
# encounter routes above (state_transition / component=encounter).
SPAN_ENCOUNTER_BEAT_ADVANCE = "tool.write.advance_encounter_beat"
SPAN_ROUTES[SPAN_ENCOUNTER_BEAT_ADVANCE] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.beat_advance",
        "beat_from": (span.attributes or {}).get("tool.encounter.beat_from", -1),
        "beat_to": (span.attributes or {}).get("tool.encounter.beat_to", -1),
        "reason": (span.attributes or {}).get("tool.encounter.reason", ""),
        "encounter_type": (span.attributes or {}).get("tool.encounter.encounter_type", ""),
    },
)
SPAN_ENCOUNTER_CONFRONTATION_INITIATED = "encounter.confrontation_initiated"
SPAN_ROUTES[SPAN_ENCOUNTER_CONFRONTATION_INITIATED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.confrontation_initiated",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
    },
)
# ADR-116 ("A Confrontation Requires an Other"): confrontation participant
# membership is observable. ``participant.joined`` fires when the engine seats
# an actor (esp. an opponent sourced from the location roster for a chase);
# ``participant.left`` fires when an opponent withdraws and the encounter
# resolves because no Other remains. ``source`` distinguishes router-named
# from location-fallback seating so the GM panel can answer "why is this
# pursuer here?".
SPAN_PARTICIPANT_JOINED = "participant.joined"
SPAN_ROUTES[SPAN_PARTICIPANT_JOINED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "participant.joined",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "name": (span.attributes or {}).get("name", ""),
        "side": (span.attributes or {}).get("side", ""),
        "source": (span.attributes or {}).get("source", ""),
    },
)
SPAN_PARTICIPANT_LEFT = "participant.left"
SPAN_ROUTES[SPAN_PARTICIPANT_LEFT] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "participant.left",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "name": (span.attributes or {}).get("name", ""),
        "side": (span.attributes or {}).get("side", ""),
        "reason": (span.attributes or {}).get("reason", ""),
    },
)
SPAN_CONFRONTATION_OPPONENT_DISENGAGED = "confrontation.opponent_disengaged"
SPAN_ROUTES[SPAN_CONFRONTATION_OPPONENT_DISENGAGED] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.opponent_disengaged",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "name": (span.attributes or {}).get("name", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)
SPAN_ENCOUNTER_EMPTY_ACTOR_LIST = "encounter.empty_actor_list"
SPAN_ROUTES[SPAN_ENCOUNTER_EMPTY_ACTOR_LIST] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.empty_actor_list",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "player_name": (span.attributes or {}).get("player_name", ""),
    },
)
# Story 45-33: a combat encounter that resolves to zero opponents post-fallback
# is the original Playtest 3 (Orin) bug shape — narrator emits ``confrontation=combat``
# but neither the explicit ``npcs_present`` nor the location-scoped registry
# fallback yields an opponent. CLAUDE.md "No Silent Fallbacks" requires the
# pipeline to refuse this state and surface the lie-detector signal here so
# the GM panel can show Sebastien the guard engaged rather than the narrator
# improvising around an empty encounter.
SPAN_ENCOUNTER_NO_OPPONENT_AVAILABLE = "encounter.no_opponent_available"
SPAN_ROUTES[SPAN_ENCOUNTER_NO_OPPONENT_AVAILABLE] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.no_opponent_available",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "category": (span.attributes or {}).get("category", ""),
        # Story 45-52 silent-failure detector: ``False`` means the
        # location-scoped fallback returned [] because the player had no
        # resolved location, not because no NPCs were at that location.
        # Discriminates a genuine empty-scene from an unresolved-perspective
        # bug shape that would otherwise look identical on the GM panel.
        "location_available": (span.attributes or {}).get("location_available", False),
    },
)
# Playtest 2026-05-08: narrator staged a multi-NPC scene (drift gang pack) and
# triggered the ``dogfight`` sealed-letter encounter, whose 1v1 red/blue
# contract refused 3 npcs_present and crashed the turn (auto-save + reconnect
# = sticky crash loop). Guard now declines the instantiation gracefully and
# fires this span so the GM panel can see "narrator picked sealed-letter for
# pack scene; engine declined" rather than the narrator's prose proceeding
# without any signal that the mechanic disengaged.
SPAN_ENCOUNTER_SEALED_LETTER_ARITY_REJECTED = "encounter.sealed_letter_arity_rejected"
SPAN_ROUTES[SPAN_ENCOUNTER_SEALED_LETTER_ARITY_REJECTED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.sealed_letter_arity_rejected",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "npc_count": (span.attributes or {}).get("npc_count", 0),
    },
)
# ADR-139 Invariant 1 (win-condition liveness): emitted by check_hp_depletion
# whenever a seated actor crosses (or sits past) the 0-HP threshold, whether or
# not the encounter resolves. sq-playtest 2026-06-07 (perseus_cloud MP): a
# PRE-EXISTING 0-HP party-mate's first beat commit auto-resolved
# ``opponent_victory`` while a live PC fought on — the GM panel could not see
# the (wrong) decision being made. ``terminal_reached=False`` with
# ``down_actors`` non-empty is the "one downed PC ≠ party defeat" branch.
SPAN_CONFRONTATION_WIN_CONDITION_EVALUATED = "confrontation.win_condition_evaluated"
SPAN_ROUTES[SPAN_CONFRONTATION_WIN_CONDITION_EVALUATED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "confrontation.win_condition_evaluated",
        "win_condition": (span.attributes or {}).get("win_condition", ""),
        "terminal_reached": (span.attributes or {}).get("terminal_reached", False),
        "outcome": (span.attributes or {}).get("outcome", ""),
        "down_actors": (span.attributes or {}).get("down_actors", ""),
        "standing_actors": (span.attributes or {}).get("standing_actors", ""),
    },
)
SPAN_ENCOUNTER_BEAT_FAILURE_BRANCH = "encounter.beat_failure_branch"
SPAN_ROUTES[SPAN_ENCOUNTER_BEAT_FAILURE_BRANCH] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.beat_failure_branch",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "base_delta": (span.attributes or {}).get("base_delta", 0),
        "failure_delta": (span.attributes or {}).get("failure_delta", 0),
    },
)
SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED = "encounter.opposed_roll_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.opposed_roll_resolved",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "player_roll": (span.attributes or {}).get("player_roll", 0),
        "player_mod": (span.attributes or {}).get("player_mod", 0),
        "opponent_roll": (span.attributes or {}).get("opponent_roll", 0),
        "opponent_mod": (span.attributes or {}).get("opponent_mod", 0),
        "player_num_advantage": (span.attributes or {}).get("player_num_advantage", 0),
        "opponent_num_advantage": (span.attributes or {}).get("opponent_num_advantage", 0),
        "shift": (span.attributes or {}).get("shift", 0),
        "tier": (span.attributes or {}).get("tier", ""),
    },
)

# Story 71-21: server-driven opponent attack (SWN hp_depletion enemy turn).
# Fires on EVERY opponent reprisal — hit or miss — carrying the full to-hit math
# so the GM panel can tell a real, mechanically-backed enemy attack from narrator
# improv. The playtest bug: perseus_cloud hp_depletion combat had no enemy turn,
# so the player could never lose; this span is the lie-detector for the fix.
SPAN_ENCOUNTER_OPPONENT_ATTACK = "encounter.opponent_attack_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_OPPONENT_ATTACK] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.opponent_attack_resolved",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "attacker": (span.attributes or {}).get("attacker", ""),
        "target": (span.attributes or {}).get("target", ""),
        "d20": (span.attributes or {}).get("d20", 0),
        "modifier": (span.attributes or {}).get("modifier", 0),
        "attack_total": (span.attributes or {}).get("attack_total", 0),
        "target_ac": (span.attributes or {}).get("target_ac", 0),
        "hit": (span.attributes or {}).get("hit", False),
    },
)

# BUG 1 (eh-opp-damage): a seated hp_depletion combat opponent has NO resolvable
# reprisal damage source (no cdef.opponent_damage, no strike beat damage_override,
# weaponless opponent core). Fires AT INSTANTIATION (the seating seam) so the GM
# panel flags the toothless Other the moment it is seated — not once per reprisal,
# six rounds deep. Authoring opponent_damage is the content fix; this is the
# engine's at-seat detector for the gap.
SPAN_ENCOUNTER_OPPONENT_TOOTHLESS = "encounter.opponent_toothless"
SPAN_ROUTES[SPAN_ENCOUNTER_OPPONENT_TOOTHLESS] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.opponent_toothless",
        "encounter_type": (span.attributes or {}).get("confrontation_type", ""),
        "opponent": (span.attributes or {}).get("opponent", ""),
        "rationale": (span.attributes or {}).get("rationale", ""),
    },
)

# 108-2: the seater reconciled a router-named free-string opponent to a BOUND,
# statted adversary in the scene (ADR-059 Monster-Manual / ADR-116 the Other).
# Fires when the intent router invented an adversary name that matched no roster
# entry (the "Hold-Dead"/"Arena Opponent" stubs) and the seater re-pointed it to
# a co-located, statted, hostile ``creature_id`` creature instead of fabricating
# one. The GM panel reads this to confirm the bound roster — not an improvised
# HP-10 placeholder — reached the fight.
SPAN_ENCOUNTER_OPPONENT_RESOLVED_FROM_ROSTER = "encounter.opponent_resolved_from_roster"
SPAN_ROUTES[SPAN_ENCOUNTER_OPPONENT_RESOLVED_FROM_ROSTER] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.opponent_resolved_from_roster",
        "router_name": (span.attributes or {}).get("router_name", ""),
        "bound_name": (span.attributes or {}).get("bound_name", ""),
        "creature_id": (span.attributes or {}).get("creature_id", ""),
        "match_scope": (span.attributes or {}).get("match_scope", ""),
    },
)

# 108-2 (MINTING-MAJOR): the seater had to FABRICATE an opponent — a router-named
# free-string adversary with no backing roster/bestiary entry AND no co-located
# bound creature to resolve to. Loud lie-detector (No Silent Fallbacks): the GM
# panel sees the engine invented a stub (and the content gap — author the
# encounter's adversary). The stub is marked ``ephemeral`` and reaped with its
# encounter so it never persists as durable canon.
SPAN_ENCOUNTER_OPPONENT_MINTED_STUB = "encounter.opponent_minted_stub"
SPAN_ROUTES[SPAN_ENCOUNTER_OPPONENT_MINTED_STUB] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.opponent_minted_stub",
        "encounter_type": (span.attributes or {}).get("confrontation_type", ""),
        "opponent": (span.attributes or {}).get("opponent", ""),
        "hp": (span.attributes or {}).get("hp", 0),
        "armor_class": (span.attributes or {}).get("armor_class", 0),
        "reason": (span.attributes or {}).get("reason", ""),
    },
)

# Story 45-3: Mid-turn momentum broadcast lie-detector. Fires whenever the
# server emits a CONFRONTATION frame carrying post-mutation momentum, so
# the GM panel can audit "the dial moved on screen because the engine
# moved a metric, not because the narrator improvised matching prose."
# ``source`` distinguishes the dice-throw site from the post-narration
# site; ``beat_id`` is non-null on dice_throw and may be null on
# narration_apply emits.
SPAN_ENCOUNTER_MOMENTUM_BROADCAST = "encounter.momentum_broadcast"
SPAN_ROUTES[SPAN_ENCOUNTER_MOMENTUM_BROADCAST] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.momentum_broadcast",
        "encounter_type": (span.attributes or {}).get("encounter_type", ""),
        "player_metric_after": (span.attributes or {}).get("player_metric_after", 0),
        "opponent_metric_after": (span.attributes or {}).get("opponent_metric_after", 0),
        "source": (span.attributes or {}).get("source", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
    },
)

# Story 106-4 Part C — inventory item-use beat lie-detector. Fires when a
# player commits a "Drink <potion>" beat mid-confrontation: the engine consumed
# the item and applied its heal effect. The GM panel reads this to confirm the
# heal was mechanically backed (item gone, HP pool moved) rather than narrator
# improvisation. ``healed`` is the post-clamp HP restored; ``hp_after`` the new
# pool current.
SPAN_CONFRONTATION_ITEM_USED = "confrontation.item_used"
SPAN_ROUTES[SPAN_CONFRONTATION_ITEM_USED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "confrontation.item_used",
        "actor": (span.attributes or {}).get("actor", ""),
        "item": (span.attributes or {}).get("item", ""),
        "healed": (span.attributes or {}).get("healed", 0),
        "hp_after": (span.attributes or {}).get("hp_after", 0),
    },
)

# Dual-track momentum constants — flat-only baseline; routes land with the
# GM-panel encounter timeline rollout.
SPAN_ENCOUNTER_BEAT_SKIPPED = "encounter.beat_skipped"
# C&C B/X class beats Task 7 — fires every time the narrator prompt is built
# with a class-filtered per-PC beat menu. Lets the GM panel verify the filter
# is wired, not just defined (CLAUDE.md OTEL-on-every-subsystem discipline).
SPAN_CONFRONTATION_BEAT_FILTER = "confrontation.beat_filter"
SPAN_ENCOUNTER_INVALID_SIDE = "encounter.invalid_side"
SPAN_ENCOUNTER_INVALID_OUTCOME_TIER = "encounter.invalid_outcome_tier"
SPAN_ENCOUNTER_METRIC_ADVANCE = "encounter.metric_advance"
SPAN_ENCOUNTER_TAG_CREATED = "encounter.tag_created"
SPAN_ENCOUNTER_TAG_BACKFIRE = "encounter.tag_backfire"
SPAN_ENCOUNTER_STATUS_ADDED = "encounter.status_added"
SPAN_ENCOUNTER_STATUS_CLEARED = "encounter.status_cleared"
SPAN_ENCOUNTER_YIELD_RECEIVED = "encounter.yield_received"
SPAN_ENCOUNTER_YIELD_RESOLVED = "encounter.yield_resolved"
SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED = "encounter.resolution_signal_emitted"
SPAN_ENCOUNTER_RESOLUTION_SIGNAL_CONSUMED = "encounter.resolution_signal_consumed"
# ADR-078 §4 — composure-break resolution + per-beat edge debits.
SPAN_ENCOUNTER_EDGE_DEBIT = "encounter.edge_debit"
SPAN_ENCOUNTER_COMPOSURE_BREAK = "encounter.composure_break"

FLAT_ONLY_SPANS.update(
    {
        SPAN_ENCOUNTER_BEAT_SKIPPED,
        SPAN_ENCOUNTER_INVALID_SIDE,
        SPAN_ENCOUNTER_INVALID_OUTCOME_TIER,
        SPAN_ENCOUNTER_METRIC_ADVANCE,
        SPAN_ENCOUNTER_TAG_CREATED,
        SPAN_ENCOUNTER_TAG_BACKFIRE,
        SPAN_ENCOUNTER_STATUS_ADDED,
        SPAN_ENCOUNTER_STATUS_CLEARED,
        SPAN_ENCOUNTER_YIELD_RECEIVED,
        SPAN_ENCOUNTER_YIELD_RESOLVED,
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED,
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_CONSUMED,
    }
)
# Promoted from flat-only — combat resolution lie-detector (sprint 3 cold-subsystem
# audit). Without typed events the GM panel's state_transition tab can't show damage
# application; the events sat in the firehose as agent_span_close only.
SPAN_ROUTES[SPAN_ENCOUNTER_EDGE_DEBIT] = SpanRoute(
    event_type="state_transition",
    component="combat",
    extract=lambda span: {
        "field": "encounter.edge_debit",
        "source_actor": (span.attributes or {}).get("source_actor", ""),
        "target_actor": (span.attributes or {}).get("target_actor", ""),
        "debit_kind": (span.attributes or {}).get("debit_kind", ""),
        "delta": (span.attributes or {}).get("delta", 0),
        "before": (span.attributes or {}).get("before", 0),
        "after": (span.attributes or {}).get("after", 0),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
        "target_select": (span.attributes or {}).get("target_select", ""),
        "taunt_redirected": (span.attributes or {}).get("taunt_redirected", False),
    },
)
SPAN_ROUTES[SPAN_ENCOUNTER_COMPOSURE_BREAK] = SpanRoute(
    event_type="state_transition",
    component="combat",
    extract=lambda span: {
        "field": "encounter.composure_break",
        "char_name": (span.attributes or {}).get("char_name", ""),
        "side": (span.attributes or {}).get("side", ""),
        "beat_id": (span.attributes or {}).get("beat_id", ""),
    },
)
# C&C B/X Task 12 — promote beat_filter from flat-only to routed so the GM
# panel's state_transition tab shows filter decisions (lie-detector discipline).
SPAN_ROUTES[SPAN_CONFRONTATION_BEAT_FILTER] = SpanRoute(
    event_type="state_transition",
    component="combat",
    extract=lambda span: {
        "field": "beat_filter",
        "actor": (span.attributes or {}).get("actor", ""),
        "character_class": (span.attributes or {}).get("class_name", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
        "pool_size": (span.attributes or {}).get("pool_size", 0),
        "filtered_size": (span.attributes or {}).get("filtered_size", 0),
        "beat_ids": (span.attributes or {}).get("available_beat_ids", ""),
        "spell_slots_remaining": (span.attributes or {}).get("spell_slots_remaining", 0.0),
    },
)
# Story 59-16 — fail-loud when a SEATED, connected PC cannot be resolved to a
# class for the single filtered CONFRONTATION delivery. The encounter layer
# must NEVER fall back to the unfiltered union for that socket; it fires this
# ERROR span and sends nothing (a lobby/unseated socket is a different, silent
# case). The GM panel surfaces it as a delivery failure, not a clean turn.
SPAN_CONFRONTATION_RECIPIENT_UNRESOLVED = "confrontation.recipient_unresolved"
SPAN_ROUTES[SPAN_CONFRONTATION_RECIPIENT_UNRESOLVED] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.recipient_unresolved",
        "player_id": (span.attributes or {}).get("player_id", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
    },
)

# sq-playtest 2026-06-02 (wry_whimsy/oz): a SEATED PC in a classes.yaml-less pack
# (``genre_pack.classes`` empty) receives the unfiltered beat UNION because there
# is nothing to class-filter. This span is the GM-panel lie-detector for that
# path: its PRESENCE proves the confrontation was delivered (not suppressed) for
# a no-classes pack; its ABSENCE on a no-classes confrontation turn means the
# frame was dropped. Distinct from confrontation.beat_filter (fires only when a
# class IS resolved) and confrontation.recipient_unresolved (the ERROR span for a
# pack that HAS classes but cannot resolve this PC's class).
SPAN_CONFRONTATION_UNFILTERED_DELIVERY = "confrontation.unfiltered_delivery"
SPAN_ROUTES[SPAN_CONFRONTATION_UNFILTERED_DELIVERY] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.unfiltered_delivery",
        "player_id": (span.attributes or {}).get("player_id", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
    },
)

# Story 85-3 — fires once per build_confrontation_payload, confirming the
# session's active_stakes was attached to the CONFRONTATION channel (Tier B
# dockview panel renders a stakes banner from it). The GM-panel lie-detector for
# the stakes wiring: has_stakes=False means a confrontation ran with no active
# stakes (legitimate), distinct from the span being ABSENT (emit dropped).
SPAN_CONFRONTATION_STAKES_ATTACHED = "confrontation.stakes_attached"
SPAN_ROUTES[SPAN_CONFRONTATION_STAKES_ATTACHED] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.stakes_attached",
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
        "has_stakes": (span.attributes or {}).get("has_stakes", False),
        "stakes_len": (span.attributes or {}).get("stakes_len", 0),
    },
)

# Story 97-3 — fires when build_confrontation_payload authors the per-beat
# pre-roll difficulty onto the beat offer (the TARGET banner source). The
# GM-panel lie-detector for the single-DC-author fix: the offered numbers here
# must match the difficulty dice resolution later reports; absence of this
# span on a confrontation frame means the offer went out without
# server-authored DCs and the client has nothing legitimate to display.
SPAN_CONFRONTATION_BEAT_DC_AUTHORED = "confrontation.beat_dc_authored"
SPAN_ROUTES[SPAN_CONFRONTATION_BEAT_DC_AUTHORED] = SpanRoute(
    event_type="state_transition",
    component="confrontation",
    extract=lambda span: {
        "field": "confrontation.beat_dc_authored",
        "genre_slug": (span.attributes or {}).get("genre_slug", ""),
        "confrontation_type": (span.attributes or {}).get("confrontation_type", ""),
        "ruleset": (span.attributes or {}).get("ruleset", ""),
        "target_name": (span.attributes or {}).get("target_name", ""),
        "beat_difficulties": (span.attributes or {}).get("beat_difficulties", ""),
    },
)


@contextmanager
def confrontation_recipient_unresolved_span(
    *,
    player_id: str,
    actor: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``confrontation.recipient_unresolved`` ERROR span for a
    seated, connected PC whose class could not be resolved. Marks the span
    status ERROR so the GM panel surfaces the delivery failure — never a
    silent fallback to the unfiltered union (Story 59-16, CLAUDE.md No
    Silent Fallbacks)."""
    with Span.open(
        SPAN_CONFRONTATION_RECIPIENT_UNRESOLVED,
        {"player_id": player_id, "actor": actor, "reason": reason, **attrs},
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason))
        yield span


@contextmanager
def confrontation_unfiltered_delivery_span(
    *,
    player_id: str,
    actor: str,
    confrontation_type: str,
    reason: str = "pack_declares_no_classes",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``confrontation.unfiltered_delivery`` span for a seated PC who
    receives the unfiltered beat UNION because the pack declares no classes
    (``genre_pack.classes`` empty) — class-filtering is a no-op. NOT an error
    (unlike ``confrontation.recipient_unresolved``): a classes.yaml-less pack
    legitimately has no per-class beat restriction, so the union IS the correct
    projection. The span exists so the GM panel can confirm the no-classes
    delivery path engaged (sq-playtest 2026-06-02 wry_whimsy/oz)."""
    with Span.open(
        SPAN_CONFRONTATION_UNFILTERED_DELIVERY,
        {
            "player_id": player_id,
            "actor": actor,
            "reason": reason,
            "confrontation_type": confrontation_type,
            **attrs,
        },
    ) as span:
        yield span


@contextmanager
def confrontation_beat_dc_authored_span(
    *,
    genre_slug: str,
    confrontation_type: str,
    ruleset: str,
    target_name: str,
    beat_difficulties: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``confrontation.beat_dc_authored`` span (Story 97-3). Fires once
    per ``build_confrontation_payload`` call that authors per-beat pre-roll
    difficulties onto the beat offer. ``beat_difficulties`` is a compact
    ``"beat_id=dc,..."`` string so the GM panel can compare the offered numbers
    against the resolution-time ``dice.request_sent`` difficulty."""
    with Span.open(
        SPAN_CONFRONTATION_BEAT_DC_AUTHORED,
        {
            "genre_slug": genre_slug,
            "confrontation_type": confrontation_type,
            "ruleset": ruleset,
            "target_name": target_name,
            "beat_difficulties": beat_difficulties,
            **attrs,
        },
    ) as span:
        yield span


@contextmanager
def confrontation_stakes_attached_span(
    *,
    genre_slug: str,
    confrontation_type: str,
    has_stakes: bool,
    stakes_len: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``confrontation.stakes_attached`` span (Story 85-3). Fires once
    per ``build_confrontation_payload`` so the GM panel can confirm the active
    stakes were attached to the CONFRONTATION channel — ``has_stakes`` tells a
    no-stakes confrontation apart from a dropped emit."""
    with Span.open(
        SPAN_CONFRONTATION_STAKES_ATTACHED,
        {
            "genre_slug": genre_slug,
            "confrontation_type": confrontation_type,
            "has_stakes": has_stakes,
            "stakes_len": stakes_len,
            **attrs,
        },
    ) as span:
        yield span


@contextmanager
def encounter_phase_transition_span(
    *,
    from_phase: str,
    to_phase: str,
    encounter_type: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_PHASE_TRANSITION,
        {"from": from_phase, "to": to_phase, "encounter_type": encounter_type, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_resolved_span(
    *,
    encounter_type: str,
    outcome: str | None,
    source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    span_attrs = {"encounter_type": encounter_type, "source": source}
    if outcome is not None:
        span_attrs["outcome"] = outcome
    span_attrs.update(attrs)
    with Span.open(SPAN_ENCOUNTER_RESOLVED, span_attrs, tracer_override=_tracer) as span:
        yield span


@contextmanager
def win_condition_evaluated_span(
    *,
    win_condition: str,
    terminal_reached: bool,
    outcome: str,
    down_actors: str,
    standing_actors: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-139 Invariant 1: the win-condition evaluation decision itself.

    Fired by ``check_hp_depletion`` whenever any seated actor sits at or past
    the 0-HP threshold — resolve and no-resolve branches both emit, so the GM
    panel can distinguish "fight correctly continued past a downed party-mate"
    (``terminal_reached=False``) from "side fully down, resolved"
    (``terminal_reached=True``).
    """
    span_attrs = {
        "win_condition": win_condition,
        "terminal_reached": terminal_reached,
        "outcome": outcome,
        "down_actors": down_actors,
        "standing_actors": standing_actors,
    }
    span_attrs.update(attrs)
    with Span.open(
        SPAN_CONFRONTATION_WIN_CONDITION_EVALUATED, span_attrs, tracer_override=_tracer
    ) as span:
        yield span


@contextmanager
def post_resolution_lethality_span(
    *,
    decision: str,
    outcome: str,
    verdict: str,
    actor: str,
    hp_before: int,
    hp_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """EH-2 lie-detector: the post-resolution PC-down decision (recover vs hold).

    Missing span on an ``opponent_victory`` where the PC hit 0 HP → nothing
    handled the 0-HP exit (the PC is parked at 0/10 with full agency, the bug).
    ``decision`` is ``non_lethal_recover`` | ``lethal_down`` | ``no_policy``."""
    with Span.open(
        SPAN_POST_RESOLUTION_LETHALITY,
        {
            "decision": decision,
            "outcome": outcome,
            "verdict": verdict,
            "actor": actor,
            "hp_before": hp_before,
            "hp_after": hp_after,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def player_action_blocked_incapacitated_span(
    *,
    character: str,
    verdict: str,
    status_text: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """barsoom-3 lie-detector: the turn-intake gate refused a downed PC's action.

    Missing span where a dead PC keeps submitting actions → the gate isn't wired
    and the dead-man-walking regression is back. ``character`` is the downed PC;
    ``verdict`` is the lethality verdict (``dead`` | ``dying``)."""
    with Span.open(
        SPAN_PLAYER_ACTION_BLOCKED_INCAPACITATED,
        {
            "character": character,
            "verdict": verdict,
            "status_text": status_text,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_initiative_rolled_span(
    *,
    encounter_type: str,
    initiative_order: str,
    source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """SWN P4 polygraph: the engine rolled initiative and seated the turn
    order. ``initiative_order`` is the human-readable order string (e.g.
    ``"Rux(9), Raider(5)"``); ``source`` names the emission site. Missing
    span on a SWN encounter → initiative wasn't engine-rolled → the narrator
    is improvising the order."""
    with Span.open(
        SPAN_ENCOUNTER_INITIATIVE_ROLLED,
        {
            "encounter_type": encounter_type,
            "initiative_order": initiative_order,
            "source": source,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_beat_applied_span(
    *,
    encounter_type: str,
    actor: str,
    beat_id: str,
    metric_delta: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_BEAT_APPLIED,
        {
            "encounter_type": encounter_type,
            "actor": actor,
            "beat_id": beat_id,
            "metric_delta": metric_delta,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_confrontation_initiated_span(
    *,
    encounter_type: str,
    genre_slug: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_CONFRONTATION_INITIATED,
        {"encounter_type": encounter_type, "genre_slug": genre_slug, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def participant_joined_span(
    *,
    encounter_type: str,
    name: str,
    side: str,
    source: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-116: an actor was seated into a confrontation. ``source`` is
    ``router_named`` or ``location_fallback`` so the GM panel can audit
    where a (esp. opponent-side) participant came from."""
    with Span.open(
        SPAN_PARTICIPANT_JOINED,
        {
            "encounter_type": encounter_type,
            "name": name,
            "side": side,
            "source": source,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def participant_left_span(
    *,
    encounter_type: str,
    name: str,
    side: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-116: an opponent withdrew. Emitted alongside end-on-no-Other
    resolution so the GM panel sees the confrontation ended because the
    Other left, not because a dial hit threshold."""
    with Span.open(
        SPAN_PARTICIPANT_LEFT,
        {
            "encounter_type": encounter_type,
            "name": name,
            "side": side,
            "reason": reason,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def confrontation_opponent_disengaged_span(
    *,
    encounter_type: str,
    name: str,
    turn_number: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """ADR-116 §4 (social path) — the narrator signalled that a seated opponent
    LEFT the confrontation (``npcs_present`` mention ``disengaged=True``). Emitted
    where the engine flips that opponent ``withdrawn``, so the GM panel sees the
    confrontation is ending because the Other departed — not because a dial hit
    threshold and not because the narrator improvised. The end-on-no-Other sweep
    then resolves the encounter (sq-playtest 2026-06-10 long_foundry zombie
    negotiation)."""
    with Span.open(
        SPAN_CONFRONTATION_OPPONENT_DISENGAGED,
        {
            "encounter_type": encounter_type,
            "name": name,
            "turn_number": int(turn_number),
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_opposed_roll_resolved_span(
    *,
    encounter_type: str,
    player_roll: int,
    player_mod: int,
    opponent_roll: int,
    opponent_mod: int,
    shift: int,
    tier: str,
    player_num_advantage: int = 0,
    opponent_num_advantage: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Lie-detector for the opposed-check path. Emit BEFORE apply_beat so the
    GM panel can correlate the engine-derived tier with the metric_advance.

    ``player_num_advantage`` / ``opponent_num_advantage`` carry the side-
    aggregate numerical-advantage modifiers from
    ``numerical_advantage_for``. Default to 0 for back-compat with legacy
    callers; production sites pass the values from ``OpposedRollResult``.
    """
    with Span.open(
        SPAN_ENCOUNTER_OPPOSED_ROLL_RESOLVED,
        {
            "encounter_type": encounter_type,
            "player_roll": int(player_roll),
            "player_mod": int(player_mod),
            "opponent_roll": int(opponent_roll),
            "opponent_mod": int(opponent_mod),
            "player_num_advantage": int(player_num_advantage),
            "opponent_num_advantage": int(opponent_num_advantage),
            "shift": int(shift),
            "tier": tier,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_opponent_attack_resolved_span(
    *,
    encounter_type: str,
    attacker: str,
    target: str,
    d20: int,
    modifier: int,
    attack_total: int,
    target_ac: int,
    hit: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Lie-detector for the server-driven opponent attack turn (story 71-21,
    SWN hp_depletion combat). Emit on every reprisal so the GM panel can audit
    that the enemy's "shot" was a real d20-vs-AC decision, not narrator prose.
    ``hit`` is the verdict; ``attack_total`` == ``d20`` + ``modifier``."""
    with Span.open(
        SPAN_ENCOUNTER_OPPONENT_ATTACK,
        {
            "encounter_type": encounter_type,
            "attacker": attacker,
            "target": target,
            "d20": int(d20),
            "modifier": int(modifier),
            "attack_total": int(attack_total),
            "target_ac": int(target_ac),
            "hit": bool(hit),
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_opponent_resolved_from_roster_span(
    *,
    router_name: str,
    bound_name: str,
    creature_id: str,
    match_scope: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """108-2: a router-named free-string opponent was reconciled to a bound,
    statted adversary in the scene instead of fabricating a stub. ``match_scope``
    records HOW it was found (``room``); ``creature_id`` proves a real bestiary
    creature reached the fight (ADR-059 Monster-Manual doctrine)."""
    with Span.open(
        SPAN_ENCOUNTER_OPPONENT_RESOLVED_FROM_ROSTER,
        {
            "router_name": router_name,
            "bound_name": bound_name,
            "creature_id": creature_id,
            "match_scope": match_scope,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_opponent_minted_stub_span(
    *,
    confrontation_type: str,
    opponent: str,
    hp: int,
    armor_class: int,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """108-2 (MINTING-MAJOR): the seater fabricated an opponent with no backing
    roster/bestiary entry and no co-located bound creature to resolve to. Loud
    lie-detector for the content gap; the stub is marked ephemeral + reaped with
    its encounter (No Silent Fallbacks)."""
    with Span.open(
        SPAN_ENCOUNTER_OPPONENT_MINTED_STUB,
        {
            "confrontation_type": confrontation_type,
            "opponent": opponent,
            "hp": hp,
            "armor_class": armor_class,
            "reason": reason,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_opponent_toothless_span(
    *,
    confrontation_type: str,
    opponent: str,
    rationale: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """BUG 1 (eh-opp-damage) instantiation-time lie-detector: an hp_depletion
    combat seated an opponent with NO resolvable reprisal damage source, so every
    enemy reprisal will land for 0 HP (the player is invulnerable). Emitted at
    SEATING by ``_seed_combat_hp_depletion_to_npcs`` so the GM panel flags the
    toothless Other immediately, rather than only via the per-turn
    ``dice.opponent_reprisal_damage_spec_missing`` warning. ``rationale`` records
    which sources were checked and came up empty."""
    with Span.open(
        SPAN_ENCOUNTER_OPPONENT_TOOTHLESS,
        {
            "confrontation_type": confrontation_type,
            "opponent": opponent,
            "rationale": rationale,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_empty_actor_list_span(
    *,
    encounter_type: str,
    genre_slug: str,
    player_name: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Narrator named adversaries in prose but the JSON game_patch omitted them."""
    with Span.open(
        SPAN_ENCOUNTER_EMPTY_ACTOR_LIST,
        {
            "encounter_type": encounter_type,
            "genre_slug": genre_slug,
            "player_name": player_name,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_no_opponent_available_span(
    *,
    encounter_type: str,
    genre_slug: str,
    player_name: str,
    category: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 45-33: combat encounter has no opponent post-fallback — guard fired.

    Distinct from ``encounter_empty_actor_list_span`` which fires when the
    narrator named adversaries in prose but the JSON extraction dropped
    them. This span fires the layer above: the entire instantiation path
    (explicit ``npcs_present`` AND the location-scoped registry fallback)
    produced zero opponents for a category=combat encounter, and the
    pipeline is refusing to advance.
    """
    with Span.open(
        SPAN_ENCOUNTER_NO_OPPONENT_AVAILABLE,
        {
            "encounter_type": encounter_type,
            "genre_slug": genre_slug,
            "player_name": player_name,
            "category": category,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_sealed_letter_arity_rejected_span(
    *,
    encounter_type: str,
    genre_slug: str,
    player_name: str,
    npc_count: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Playtest 2026-05-08: sealed-letter encounter (1v1 red/blue) declined
    because the narrator staged a multi-NPC scene. The encounter does NOT
    instantiate; the narration turn continues without a structured mechanic.
    GM panel reads this span as the lie-detector signal that the engine saw
    the inappropriate selection and refused, rather than improvising a 1v1
    against a pack.
    """
    with Span.open(
        SPAN_ENCOUNTER_SEALED_LETTER_ARITY_REJECTED,
        {
            "encounter_type": encounter_type,
            "genre_slug": genre_slug,
            "player_name": player_name,
            "npc_count": npc_count,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def encounter_beat_skipped_span(
    *,
    reason: str,
    actor: str,
    actor_side: str,
    beat_id: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_BEAT_SKIPPED,
        {"reason": reason, "actor": actor, "actor_side": actor_side, "beat_id": beat_id, **attrs},
    ) as s:
        yield s


def confrontation_item_used_span(
    *,
    actor: str,
    item: str,
    healed: int,
    hp_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a confrontation.item_used span (Story 106-4 Part C lie-detector).

    Point mutation, not a span of work — opens and immediately closes so the
    WatcherSpanProcessor routes it to the GM panel's state_transition feed.
    """
    with Span.open(
        SPAN_CONFRONTATION_ITEM_USED,
        {
            "actor": actor,
            "item": item,
            "healed": healed,
            "hp_after": hp_after,
            **attrs,
        },
        tracer_override=_tracer,
    ):
        pass


@contextmanager
def confrontation_beat_filter_span(
    *,
    actor: str,
    class_name: str,
    confrontation_type: str,
    available_beat_ids: str,
    spell_slots_remaining: float,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Emitted once per PC actor when the narrator prompt renders a
    class-filtered beat menu. Lets the GM panel verify class-distinct beat
    menus are actually shipped to the LLM (§5.6 criterion 1)."""
    with Span.open(
        SPAN_CONFRONTATION_BEAT_FILTER,
        {
            "actor": actor,
            "class_name": class_name,
            "confrontation_type": confrontation_type,
            "available_beat_ids": available_beat_ids,
            "spell_slots_remaining": spell_slots_remaining,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_invalid_side_span(
    *,
    actor_name: str,
    declared_side: str,
    valid_set: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_INVALID_SIDE,
        {"actor_name": actor_name, "declared_side": declared_side, "valid_set": valid_set, **attrs},
    ) as s:
        yield s


@contextmanager
def encounter_invalid_outcome_tier_span(
    *,
    beat_id: str,
    actor: str,
    declared_tier: str,
    valid_set: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_INVALID_OUTCOME_TIER,
        {
            "beat_id": beat_id,
            "actor": actor,
            "declared_tier": declared_tier,
            "valid_set": valid_set,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_metric_advance_span(
    *,
    side: str,
    delta_kind: str,
    delta: int,
    before: int,
    after: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_METRIC_ADVANCE,
        {
            "side": side,
            "delta_kind": delta_kind,
            "delta": delta,
            "before": before,
            "after": after,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_momentum_broadcast_span(
    *,
    encounter_type: str,
    player_metric_after: int,
    opponent_metric_after: int,
    source: str,
    beat_id: str | None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 45-3: Lie-detector for the mid-turn CONFRONTATION emit.

    Wraps every server site that broadcasts a CONFRONTATION frame
    carrying post-mutation momentum. ``source`` is ``"dice_throw"`` from
    the dice-dispatch site and ``"narration_apply"`` from the post-
    narration site. ``beat_id`` is the resolved beat on the dice path
    and may be ``None`` for narrator-driven emits.
    """
    with Span.open(
        SPAN_ENCOUNTER_MOMENTUM_BROADCAST,
        {
            "encounter_type": encounter_type,
            "player_metric_after": player_metric_after,
            "opponent_metric_after": opponent_metric_after,
            "source": source,
            "beat_id": beat_id or "",
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_tag_created_span(
    *,
    tag_text: str,
    created_by: str,
    target: str | None,
    leverage: int,
    fleeting: bool,
    created_via: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_TAG_CREATED,
        {
            "tag_text": tag_text,
            "created_by": created_by,
            "target": target or "",
            "leverage": leverage,
            "fleeting": fleeting,
            "created_via": created_via,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_tag_backfire_span(
    *,
    tag_text: str,
    created_by: str,
    target: str,
    triggering_beat: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_TAG_BACKFIRE,
        {
            "tag_text": tag_text,
            "created_by": created_by,
            "target": target,
            "triggering_beat": triggering_beat,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_status_added_span(
    *,
    actor: str,
    text: str,
    severity: str,
    source: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_STATUS_ADDED,
        {"actor": actor, "text": text, "severity": severity, "source": source, **attrs},
    ) as s:
        yield s


@contextmanager
def encounter_status_cleared_span(
    *,
    actor: str,
    text: str,
    severity: str,
    reason: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """``reason``: ``scene_end`` | ``narrator_clear`` | ``location_change``."""
    with Span.open(
        SPAN_ENCOUNTER_STATUS_CLEARED,
        {"actor": actor, "text": text, "severity": severity, "reason": reason, **attrs},
    ) as s:
        yield s


@contextmanager
def encounter_yield_received_span(
    *,
    player_id: str,
    actor_name: str,
    prior_player_metric: int,
    prior_opponent_metric: int,
    statuses_taken_this_encounter: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_YIELD_RECEIVED,
        {
            "player_id": player_id,
            "actor_name": actor_name,
            "prior_player_metric": prior_player_metric,
            "prior_opponent_metric": prior_opponent_metric,
            "statuses_taken_this_encounter": statuses_taken_this_encounter,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_yield_resolved_span(
    *,
    outcome: str,
    yielded_actors: tuple[str, ...],
    edge_refreshed: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_YIELD_RESOLVED,
        {
            "outcome": outcome,
            "yielded_actors": ",".join(yielded_actors),
            "edge_refreshed": edge_refreshed,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_resolution_signal_emitted_span(
    *,
    outcome: str,
    final_player_metric: int,
    final_opponent_metric: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_EMITTED,
        {
            "outcome": outcome,
            "final_player_metric": final_player_metric,
            "final_opponent_metric": final_opponent_metric,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_resolution_signal_consumed_span(
    *,
    outcome: str,
    final_player_metric: int,
    final_opponent_metric: int,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ENCOUNTER_RESOLUTION_SIGNAL_CONSUMED,
        {
            "outcome": outcome,
            "final_player_metric": final_player_metric,
            "final_opponent_metric": final_opponent_metric,
            **attrs,
        },
    ) as s:
        yield s


@contextmanager
def encounter_edge_debit_span(
    *,
    source_actor: str,
    target_actor: str,
    debit_kind: str,
    delta: int,
    before: int,
    after: int,
    beat_id: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Per-beat edge debit (ADR-078 §3-4).

    ``debit_kind``: ``"self"`` (acting actor pays) | ``"target"`` (opposing
    actor takes the hit). ``before``/``after`` are clamped current values.
    Lie-detector for "did the engine actually debit edge or did the
    narrator just describe a wound." Always one span per beat per debit.
    """
    with Span.open(
        SPAN_ENCOUNTER_EDGE_DEBIT,
        {
            "source_actor": source_actor,
            "target_actor": target_actor,
            "debit_kind": debit_kind,
            "delta": delta,
            "before": before,
            "after": after,
            "beat_id": beat_id,
            **attrs,
        },
    ) as s:
        yield s


SPAN_ENCOUNTER_CHECK_RESOLVED = "encounter.check_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_CHECK_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.check_resolved",
        "kind": (span.attributes or {}).get("check.kind", ""),
        "actor": (span.attributes or {}).get("check.actor", ""),
        "label": (span.attributes or {}).get("check.label", ""),
        "total": (span.attributes or {}).get("check.total", 0),
        "difficulty": (span.attributes or {}).get("check.difficulty", 0),
        "outcome": (span.attributes or {}).get("check.outcome", ""),
    },
)
SPAN_ENCOUNTER_SAVING_THROW_RESOLVED = "encounter.saving_throw_resolved"
SPAN_ROUTES[SPAN_ENCOUNTER_SAVING_THROW_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.saving_throw",
        "defender_actor": (span.attributes or {}).get("defender_actor", ""),
        "defender_class": (span.attributes or {}).get("defender_class", ""),
        "category": (span.attributes or {}).get("category", ""),
        "threat_label": (span.attributes or {}).get("threat_label", ""),
        "target": (span.attributes or {}).get("target", 0),
        "roll": (span.attributes or {}).get("roll", 0),
        "total": (span.attributes or {}).get("total", 0),
        "shift": (span.attributes or {}).get("shift", 0),
        "tier": (span.attributes or {}).get("tier", ""),
        "mindless_gate": (span.attributes or {}).get("mindless_gate", False),
        "spell_id": (span.attributes or {}).get("spell_id", ""),
    },
)


@contextmanager
def encounter_saving_throw_resolved_span(
    *,
    defender_actor: str,
    defender_class: str,
    category: str,
    ability: str | None,
    threat_label: str,
    target: int,
    roll: int,
    mod: int,
    total: int,
    shift: int,
    tier: str,
    spell_id: str,
    encounter_type: str,
    mindless_gate: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Span emitted on every saving-throw resolution.

    The lie-detector for B/X save resolution: missing span → save
    subsystem isn't engaged → narrator is improvising the save outcome.

    ``mindless_gate=True`` indicates the save was SKIPPED because the
    target was mindless and the spell had ``requires_mind=True``;
    in that case ``roll/total/shift/tier`` are zero/empty and only
    the gate decision is logged.
    """
    span_attrs: dict[str, Any] = {
        "defender_actor": defender_actor,
        "defender_class": defender_class,
        "category": category,
        "threat_label": threat_label,
        "target": target,
        "roll": roll,
        "mod": mod,
        "total": total,
        "shift": shift,
        "tier": tier,
        "spell_id": spell_id,
        "encounter_type": encounter_type,
        "mindless_gate": mindless_gate,
        **attrs,
    }
    if ability is not None:
        span_attrs["ability"] = ability
    with Span.open(
        SPAN_ENCOUNTER_SAVING_THROW_RESOLVED,
        span_attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


# Story 2026-05-10: Taunt mechanic — force enemy attention.
# See docs/superpowers/specs/2026-05-10-class-mechanical-surface-design.md §8.
SPAN_ENCOUNTER_TAUNT_ACTIVATED = "encounter.taunt.activated"
SPAN_ROUTES[SPAN_ENCOUNTER_TAUNT_ACTIVATED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.taunt",
        "op": "activated",
        "actor_id": (span.attributes or {}).get("actor_id", ""),
        "round": (span.attributes or {}).get("round", 0),
    },
)
SPAN_ENCOUNTER_TAUNT_EXPIRED = "encounter.taunt.expired"
SPAN_ROUTES[SPAN_ENCOUNTER_TAUNT_EXPIRED] = SpanRoute(
    event_type="state_transition",
    component="encounter",
    extract=lambda span: {
        "field": "encounter.taunt",
        "op": "expired",
        "actor_id": (span.attributes or {}).get("actor_id", ""),
        "round": (span.attributes or {}).get("round", 0),
    },
)


@contextmanager
def encounter_composure_break_span(
    *,
    char_name: str,
    side: str,
    beat_id: str,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Composure break — edge dropped to 0 (ADR-078 §4).

    ``side``: ``"self"`` if the acting actor broke, ``"target"`` if the
    opposing actor broke. Triggers ``encounter.resolved=True`` in the
    caller; this span fires before the encounter-level resolution.
    """
    with Span.open(
        SPAN_ENCOUNTER_COMPOSURE_BREAK,
        {
            "char_name": char_name,
            "side": side,
            "beat_id": beat_id,
            **attrs,
        },
    ) as s:
        yield s


def check_resolved_span(
    *,
    kind: str,
    actor: str,
    label: str,
    total: int,
    difficulty: int,
    outcome: str,
    **attrs: Any,
) -> None:
    """Span for a non-beat SWN check/save — the GM-panel polygraph for free rolls.

    ``kind``: ``"skill_check"`` | ``"save"``. ``actor`` is the character name.
    ``outcome`` is the ``RollOutcome`` value string (e.g. ``"Success"``).
    Plain function, not a contextmanager — emits and closes immediately.
    """
    with Span.open(
        SPAN_ENCOUNTER_CHECK_RESOLVED,
        {
            "check.kind": kind,
            "check.actor": actor,
            "check.label": label,
            "check.total": total,
            "check.difficulty": difficulty,
            "check.outcome": outcome,
            **attrs,
        },
    ):
        pass
