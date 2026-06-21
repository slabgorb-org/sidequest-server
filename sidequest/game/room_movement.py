"""Room-graph movement — Slice E provides the chargen-time init surface only.

This module exposes :func:`init_room_graph_location`. The runtime
movement surface (``validate_room_transition``, ``apply_validated_move``,
``build_room_graph_explored``) belongs to the per-turn dispatch pipeline
and lands with the narrator runtime in a later story.

No silent fallback: a room graph declared ``room_graph`` mode but
carrying no room tagged ``entrance`` is an authoring bug. We raise
:class:`RoomGraphInitError`; the dispatch caller decides whether to
log-and-continue (chargen must not hard-fail) or propagate.
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot, RoomState
from sidequest.genre.models.world import RoomDef
from sidequest.magic.confrontations import find_eligible_room_autofire
from sidequest.magic.outputs import apply_mandatory_outputs
from sidequest.telemetry.spans.movement import room_discovered_span
from sidequest.telemetry.spans.rig import (
    emit_room_entry_evaluated,
    emit_room_entry_skipped,
)


class RoomGraphInitError(Exception):
    """Raised when :func:`init_room_graph_location` cannot find an entrance."""


def init_room_graph_location(snap: GameSnapshot, rooms: list[RoomDef]) -> str:
    """Set ``snap.character_locations`` to the graph's entrance room.

    Mutates ``snap`` in place:

    - ``snap.character_locations[character_name]`` = the first room with
      ``room_type == "entrance"`` for each character in ``snap.characters``
    - ``snap.discovered_rooms`` gains that room id (dedup-append on a
      ``list[str]`` for JSON stability with existing saves).

    Returns the chosen entrance id so the caller can emit OTEL without
    re-scanning ``snap.character_locations``.

    Raises :class:`RoomGraphInitError` when no room is tagged
    ``entrance`` — a pack authoring error rather than a runtime state.
    """
    entrance = next((r for r in rooms if r.room_type == "entrance"), None)
    if entrance is None:
        raise RoomGraphInitError(f"room graph has no entrance room — {len(rooms)} rooms checked")

    # Populate character_locations for all characters (Wave 2B).
    for character in snap.characters:
        snap.character_locations[character.core.name] = entrance.id

    if entrance.id not in snap.discovered_rooms:
        snap.discovered_rooms.append(entrance.id)
    return entrance.id


def record_room_discovery(
    snap: GameSnapshot,
    *,
    character_id: str,
    from_room: str,
    to_room: str,
) -> None:
    """Persist the room-graph axis on a dungeon transition (Story 153-24, ADR-055).

    Runtime counterpart to :func:`init_room_graph_location`: as the party
    walks the dungeon graph the *region* axis advances on its own, but the
    *room* axis (``discovered_rooms`` / ``room_states`` /
    ``Character.current_room``) was never written — so forensics and cold
    reload (ADR-133 / ADR-115) saw an empty dungeon. This closes that gap by
    wiring the existing transition seam to the existing fields. Mutates
    ``snap`` in place:

    - Both endpoints are dedup-appended to ``discovered_rooms``: the entered
      room (``to_room``) AND the room crossed from (``from_room``). The
      playtest finding was an empty ``discovered_rooms`` while standing
      mid-crawl, so the room the actor came from must backfill too (AC1:
      "must now show both the entrance and exp002.r2").
    - ``room_states[to_room]`` is seeded with a fresh :class:`RoomState` on
      first entry, and PRESERVED on re-entry (never clobber the Story 45-43
      container/prop lifecycle written by the container-retrieval path).
    - The acting character's ``current_room`` is set to ``to_room``.

    Emits one ``room.discovered`` span carrying ``room_id``,
    ``newly_discovered`` (first-entry vs backtracking), ``discovered_count``,
    and ``character`` so the GM panel can confirm the room axis moved
    (CLAUDE.md OTEL mandate; the region axis' ``dungeon.map_emitted`` does not
    prove the room axis was written).
    """
    newly_discovered = to_room not in snap.discovered_rooms
    if from_room not in snap.discovered_rooms:
        snap.discovered_rooms.append(from_room)
    # Re-test membership: when ``from_room == to_room`` the append above already
    # recorded ``to_room``, so guard against a double-append (Reviewer LOW). The
    # sole caller gates ``result.location != old_loc``, but the helper is public.
    if newly_discovered and to_room not in snap.discovered_rooms:
        snap.discovered_rooms.append(to_room)

    # Seed mechanical state for the entered room on first entry only — never
    # overwrite the container/prop state the Story 45-43 path already wrote.
    if to_room not in snap.room_states:
        snap.room_states[to_room] = RoomState(room_id=to_room)

    character = next((c for c in snap.characters if c.core.name == character_id), None)
    if character is not None:
        character.current_room = to_room

    with room_discovered_span(
        room_id=to_room,
        newly_discovered=newly_discovered,
        discovered_count=len(snap.discovered_rooms),
        character=character_id,
    ):
        pass


def process_room_entry(
    snap: GameSnapshot,
    *,
    character_id: str,
    room_id: str,
    current_turn: int,
) -> None:
    """Post-room-entry hook: dispatch any rig-coupled auto-fire
    confrontations that match the entered room AND pass the cooldown gate.

    ``room_id`` accepts three forms:

    1. Chassis-prefixed ``"<chassis_id>:<room_local>"`` — explicit form
       used by tests and callers that already know the chassis context.
    2. Chassis-qualified narrator form ``"<Chassis Name> — <Display>"``
       (em-dash with surrounding spaces) — what the narrator actually
       emits as ``location``. Stripped via ``rsplit`` and resolved by
       falling through to (3).
    3. Bare world-name like ``"Galley"`` — case-insensitive match
       against ``chassis.interior_rooms`` across all registered chassis.

    Inputs that match none of these resolve to no chassis and emit
    ``room.entry_skipped`` (reason ``not_chassis_room`` or
    ``chassis_not_found``); map-graph rooms are handled by the legacy
    room-graph machinery upstream of this hook.

    Story 47-4 wires Galley entry → ``the_tea_brew``. Story 47-6 adds the
    em-dash matcher, OTEL on every silent-return path, and splits the
    cooldown gate out of ``find_eligible_room_autofire`` so the
    ``room.entry_evaluated`` span can distinguish ``eligible_count``
    (matched room+bond) from ``fired_count`` (matched AND off cooldown).

    Story 47-6 + post-merge with 45-43: confrontations are read from
    ``snap.magic_state.confrontations`` (canonical store after the
    snapshot split-brain cleanup) filtered to ``register == "intimate"``
    so world-scoped bar-DSL entries don't appear here.

    OTEL: every return path emits either ``room.entry_skipped`` (with
    ``reason``) or ``room.entry_evaluated`` (with ``eligible_count`` and
    ``fired_count``); per-firing ``rig.confrontation_outcome`` and
    ``rig.bond_event`` come from inside ``apply_mandatory_outputs``.
    """
    if ":" in room_id:
        chassis_id, room_local_id = room_id.split(":", 1)
        chassis = snap.chassis_registry.get(chassis_id)
        if chassis is None:
            emit_room_entry_skipped(
                reason="chassis_not_found",
                room_id=room_id,
                actor_id=character_id,
            )
            return
    else:
        # Strip the chassis-qualified prefix if present. The narrator's
        # ``location`` field is "<chassis_name> — <room_display>" — splitting
        # on " — " and taking the trailing segment yields the room piece.
        # Bare names without the separator pass through unchanged.
        room_segment = room_id.rsplit(" — ", 1)[-1]
        room_local_id = room_segment.strip().lower().replace(" ", "_")
        chassis = None
        for c in snap.chassis_registry.values():
            normalized = {r.lower() for r in c.interior_rooms}
            if room_local_id in normalized:
                chassis = c
                break
        if chassis is None:
            emit_room_entry_skipped(
                reason="not_chassis_room",
                room_id=room_id,
                actor_id=character_id,
            )
            return

    bond = chassis.bond_for(character_id)
    if bond is None:
        emit_room_entry_skipped(
            reason="no_bond_for_actor",
            room_id=room_id,
            actor_id=character_id,
        )
        return

    # 45-43 (snapshot split-brain): magic_state.confrontations is the
    # canonical store. World-scoped (bar-DSL) entries are driven by the
    # threshold evaluator, not this room-entry path; pre-filter to
    # chassis-coupled (``register == "intimate"``) entries.
    if snap.magic_state is None:
        emit_room_entry_skipped(
            reason="no_magic_state",
            room_id=room_id,
            actor_id=character_id,
        )
        return
    chassis_coupled = [c for c in snap.magic_state.confrontations if c.register == "intimate"]
    eligible = find_eligible_room_autofire(
        confrontations=chassis_coupled,
        room_local_id=room_local_id,
        bond_tier_chassis=bond.bond_tier_chassis,
    )

    fired_count = 0
    for cdef in eligible:
        # Per-confrontation cooldown gate. Eligible entries that fail
        # the cooldown still count toward ``eligible_count`` so the GM
        # panel sees "matched but on cooldown" vs "no match".
        cooldown_key = f"{chassis.id}:{cdef.id}"
        last_fired = snap.chassis_autofire_cooldowns.get(cooldown_key)
        cooldown_turns = cdef.fire_conditions.cooldown_turns if cdef.fire_conditions else 0
        if last_fired is not None and (current_turn - last_fired) < cooldown_turns:
            continue

        outputs = cdef.outcomes["clear_win"].mandatory_outputs
        apply_mandatory_outputs(
            snapshot=snap,
            outputs=outputs,
            actor=character_id,
            chassis_id=chassis.id,
            confrontation_id=cdef.id,
            register=cdef.register or "",
            branch="clear_win",
            turn_id=current_turn,
            narrative_seed=f"auto_fire:{room_local_id}",
        )
        snap.chassis_autofire_cooldowns[cooldown_key] = current_turn
        fired_count += 1

    emit_room_entry_evaluated(
        chassis_id=chassis.id,
        room_local_id=room_local_id,
        eligible_count=len(eligible),
        fired_count=fired_count,
    )


def process_session_open(
    snap: GameSnapshot,
    *,
    character_id: str,
    current_turn: int,
) -> None:
    """Run room-entry eligibility against the character's current location
    at session-start time.

    Story 47-6 (Bug 3): ``sidequest/server/dispatch/opening.py`` sets
    the starting interior room without going through
    ``process_room_entry``. So the FIRST eligible moment — turn 1, cold
    start in galley with bond ``trusted`` — silently skipped. This hook
    closes the gap. Idempotent because ``process_room_entry`` records a
    cooldown stamp on fire, so a second call within the cooldown window
    is observable (eligible_count >= 1, fired_count == 0) but harmless.

    No-op when the PC has no per-character location entry — the opening
    pipeline will populate it before this hook runs in production. Wave
    2B (story 45-48) uses ``snap.party_location(perspective=character_id)``
    instead of the removed party-level ``snap.location``.
    """
    location = snap.party_location(perspective=character_id)
    if not location:
        return
    process_room_entry(
        snap,
        character_id=character_id,
        room_id=location,
        current_turn=current_turn,
    )


__all__ = [
    "RoomGraphInitError",
    "init_room_graph_location",
    "process_room_entry",
    "process_session_open",
    "record_room_discovery",
]
