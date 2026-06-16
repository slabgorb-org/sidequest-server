"""Failing tests for Wave 2A — NPC Pool / NPC State Split (story 45-47, Task 1).

These tests assert the type contract the plan introduces:
- ``NpcPoolMember`` (new) — identity-only pool member, lives in
  ``sidequest.game.npc_pool``.
- ``Npc`` (existing, gains 3 fields) — ``pool_origin``, ``last_seen_location``,
  ``last_seen_turn``.
- ``GameSnapshot`` (existing, swaps fields) — ``npc_pool: list[NpcPoolMember]``
  replaces the (dropped, story 45-52) ``npc_registry``.

Per plan (docs/superpowers/plans/2026-05-04-snapshot-split-brain-wave-2a.md):
Task 1 is the type-scaffolding pass. No behavior change — tests assert types
compile, defaults round-trip, and required fields are enforced.

RED phase: every test in this file should fail today. The ``NpcPoolMember``
import will ImportError (module doesn't exist); the ``Npc`` field tests will
fail with pydantic ``extra='forbid'`` ValidationError; the ``GameSnapshot``
test will AttributeError on ``npc_pool``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc

# ---------------------------------------------------------------------------
# NpcPoolMember model contract
# ---------------------------------------------------------------------------


def test_npc_pool_member_constructs_with_minimal_required_fields() -> None:
    """``name`` and ``drawn_from`` are the only required fields; everything
    else defaults to ``None`` (or empty)."""
    member = NpcPoolMember(name="Marya", drawn_from="legacy_registry")
    assert member.name == "Marya"
    assert member.drawn_from == "legacy_registry"
    assert member.role is None
    assert member.pronouns is None
    assert member.appearance is None
    assert member.archetype_id is None


def test_npc_pool_member_constructs_with_all_fields() -> None:
    member = NpcPoolMember(
        name="Boris",
        role="bartender",
        pronouns="he/him",
        appearance="weathered hands, brass earring",
        archetype_id="genre.barkeep",
        drawn_from="world_authored",
    )
    assert member.name == "Boris"
    assert member.role == "bartender"
    assert member.pronouns == "he/him"
    assert member.appearance == "weathered hands, brass earring"
    assert member.archetype_id == "genre.barkeep"
    assert member.drawn_from == "world_authored"


def test_npc_pool_member_json_round_trip_preserves_all_fields() -> None:
    """Pool members serialize to JSON and back with no field drift —
    they live in save snapshots."""
    original = NpcPoolMember(
        name="Wren",
        role="hedge witch",
        pronouns="they/them",
        appearance="silver braids, one milky eye",
        archetype_id="genre.cunning_folk",
        drawn_from="name_generator",
    )
    payload = original.model_dump()
    restored = NpcPoolMember.model_validate(payload)
    assert restored == original


def test_npc_pool_member_json_round_trip_handles_optional_nulls() -> None:
    """Round-trip a minimally-populated member; optional ``None`` values
    must not get stringified or dropped."""
    original = NpcPoolMember(name="Fen", drawn_from="narrator_invented")
    payload = original.model_dump()
    restored = NpcPoolMember.model_validate(payload)
    assert restored == original
    assert restored.role is None
    assert restored.archetype_id is None


def test_npc_pool_member_drawn_from_is_required() -> None:
    """The plan's contract: every member must declare provenance.
    ``drawn_from`` has no default — omitting it must raise."""
    with pytest.raises(ValidationError) as exc:
        NpcPoolMember(name="Anonymous")  # type: ignore[call-arg]
    assert "drawn_from" in str(exc.value)


def test_npc_pool_member_name_is_required() -> None:
    """A pool member without a name is meaningless — narrator can't cite it."""
    with pytest.raises(ValidationError) as exc:
        NpcPoolMember(drawn_from="legacy_registry")  # type: ignore[call-arg]
    assert "name" in str(exc.value)


def test_npc_pool_member_rejects_extra_fields() -> None:
    """Schema discipline: ``extra='forbid'`` so pool members can't grow
    silent state. Stateful tracking belongs on ``Npc``, not the pool."""
    with pytest.raises(ValidationError) as exc:
        NpcPoolMember(
            name="X",
            drawn_from="legacy_registry",
            last_seen_location="Tavern",  # type: ignore[call-arg]
        )
    assert "extra" in str(exc.value).lower() or "last_seen_location" in str(exc.value)


# ---------------------------------------------------------------------------
# Npc gains pool_origin + last_seen_* fields
# ---------------------------------------------------------------------------


def _minimal_creature_core(name: str = "Boris") -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A weathered figure.",
        personality="Stoic.",
    )


def test_npc_defaults_pool_origin_to_none() -> None:
    """Newly-constructed ``Npc`` has ``pool_origin = None`` — the
    narrator-invented signal. Pool-promoted NPCs set this explicitly."""
    npc = Npc(core=_minimal_creature_core())
    assert npc.pool_origin is None


def test_npc_defaults_last_seen_location_to_none() -> None:
    """Last-seen tracking is empty until the narrator first cites the NPC."""
    npc = Npc(core=_minimal_creature_core())
    assert npc.last_seen_location is None


def test_npc_defaults_last_seen_turn_to_zero() -> None:
    """Default ``0`` means 'never seen this session' — distinct from
    'seen on turn 0' which is impossible (turn counter starts at 1)."""
    npc = Npc(core=_minimal_creature_core())
    assert npc.last_seen_turn == 0


def test_npc_accepts_pool_origin_string() -> None:
    """Pool-promoted NPC carries the originating pool member's name."""
    npc = Npc(core=_minimal_creature_core(), pool_origin="Marya")
    assert npc.pool_origin == "Marya"


def test_npc_accepts_last_seen_fields() -> None:
    npc = Npc(
        core=_minimal_creature_core(),
        last_seen_location="TavernRow",
        last_seen_turn=7,
    )
    assert npc.last_seen_location == "TavernRow"
    assert npc.last_seen_turn == 7


def test_npc_json_round_trip_preserves_new_fields() -> None:
    """Round-trip: the three new fields survive ``model_dump`` /
    ``model_validate`` so they persist across save/load."""
    original = Npc(
        core=_minimal_creature_core(name="Wren"),
        pool_origin="Wren",
        last_seen_location="Crossroads",
        last_seen_turn=12,
    )
    payload = original.model_dump()
    restored = Npc.model_validate(payload)
    assert restored.pool_origin == "Wren"
    assert restored.last_seen_location == "Crossroads"
    assert restored.last_seen_turn == 12


def test_npc_pool_origin_distinct_from_location_field() -> None:
    """Sanity: ``pool_origin`` and ``location`` are separate fields with
    independent semantics. ``location`` is current scene; ``pool_origin``
    is provenance."""
    npc = Npc(
        core=_minimal_creature_core(),
        location="Tavern",
        pool_origin="Boris",
    )
    assert npc.location == "Tavern"
    assert npc.pool_origin == "Boris"


def test_npc_last_seen_location_distinct_from_location_and_current_room() -> None:
    """All three location-flavored fields can hold different values
    simultaneously (open question #2 in the session — spec keeps them
    distinct). Per docstring: ``location`` is current scene, ``current_room``
    is chassis interior, ``last_seen_location`` is most recent narration mention."""
    npc = Npc(
        core=_minimal_creature_core(),
        location="Engine Room",
        current_room="bridge_aft",
        last_seen_location="Bridge",
    )
    assert npc.location == "Engine Room"
    assert npc.current_room == "bridge_aft"
    assert npc.last_seen_location == "Bridge"


# ---------------------------------------------------------------------------
# GameSnapshot.npc_pool field
# ---------------------------------------------------------------------------


def test_game_snapshot_defaults_npc_pool_to_empty_list() -> None:
    """New sessions start with no pool — narrator-invented members
    populate reactively (``drawn_from='narrator_invented'``)."""
    snapshot = GameSnapshot()
    assert snapshot.npc_pool == []


def test_game_snapshot_accepts_npc_pool_members() -> None:
    """Pool members can be appended via construction or post-init append."""
    members = [
        NpcPoolMember(name="A", drawn_from="legacy_registry"),
        NpcPoolMember(name="B", drawn_from="world_authored"),
    ]
    snapshot = GameSnapshot(npc_pool=members)
    assert len(snapshot.npc_pool) == 2
    assert snapshot.npc_pool[0].name == "A"
    assert snapshot.npc_pool[1].drawn_from == "world_authored"


def test_game_snapshot_npc_pool_round_trips_through_json() -> None:
    """Save/load: pool survives ``model_dump`` / ``model_validate``
    with member identity preserved."""
    original = GameSnapshot(
        npc_pool=[
            NpcPoolMember(
                name="Marya",
                role="barkeep",
                pronouns="she/her",
                appearance="weathered hands",
                archetype_id="genre.barkeep",
                drawn_from="legacy_registry",
            )
        ]
    )
    payload = original.model_dump()
    restored = GameSnapshot.model_validate(payload)
    assert len(restored.npc_pool) == 1
    member = restored.npc_pool[0]
    assert member.name == "Marya"
    assert member.role == "barkeep"
    assert member.archetype_id == "genre.barkeep"


def test_game_snapshot_carries_pool_and_npcs_independently() -> None:
    """Pool and ``npcs`` are separate stores. A snapshot can have
    both populated; they don't conflict."""
    pool_member = NpcPoolMember(name="Wren", drawn_from="world_authored")
    npc = Npc(core=_minimal_creature_core(name="Boris"), pool_origin="Boris")
    snapshot = GameSnapshot(npc_pool=[pool_member], npcs=[npc])
    assert len(snapshot.npc_pool) == 1
    assert len(snapshot.npcs) == 1
    assert snapshot.npc_pool[0].name == "Wren"
    assert snapshot.npcs[0].pool_origin == "Boris"
