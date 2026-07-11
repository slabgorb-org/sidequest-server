"""MM injection routes through GreenRoom.admit — one identity per creature
regardless of which builders propose it (ADR-156 feeders 1-4, Green Room
Task 2).

Step 1 findings (read before extending):

* **Application seam** — pre-Task-2, ``inject()`` (monster_manual_inject.py
  :886-1000 as of task-1) combined ``human_patches + creature_patches`` into
  ``all_patches`` (emitting ``SPAN_MONSTER_MANUAL_INJECTED`` off THOSE two
  builders only), then — when ``room_id`` + ``combat_encounters`` — appended
  the room-binding patches, then appended region-population patches AFTER
  filtering them against the room-binding set via a hand-rolled
  ``_patch_identity_key``/``normalize_name`` pre-filter (the ONLY place
  identity-key dedup ran pre-Task-2). The combined ``all_patches`` list was
  finally wrapped in one ``WorldStatePatch(npcs_present=...)`` and applied via
  ``snapshot.apply_world_patch`` (session.py:1853-1859), which merges/appends
  purely by **NAME** (``next((n for n in self.npcs if n.core.name ==
  npc_patch.name), None)``) through ``_merge_npc_patch``/``_npc_from_patch``.
  Task 2 replaces that whole tail: every builder's patches now become
  :class:`~sidequest.game.green_room.MaterializationCandidate` objects and
  land through ONE ``green_room.admit()`` call; the hand-rolled pre-filter is
  deleted (admit()'s ladder supersedes it).

* **Authored-marker logic** — within :351-490 (``_npc_patches_for_available_
  humans`` / ``_human_patch``) there was, pre-Task-2, NO read of
  ``ManualNpc.authored`` at all: every human patch (Active-anchored or
  Available, placed or unplaced, backfilled-authored or namegen-pregen) was
  stamped identically via ``_human_patch(...)`` with
  ``origin=Origin(kind=OriginKind.MANUAL_POOL)`` unconditionally. The
  authored/pregen distinction lives ONLY on ``ManualNpc.authored: bool``
  (monster_manual.py:168-176, set by ``pregen._seed_authored_npcs``) and was
  never consulted at this seam. There is also no ``authored_id`` anywhere in
  the Manual pipeline itself — ``AuthoredNpc.id`` (the real id) only exists on
  ``pack.worlds[world].authored_npcs``, resolved today by
  ``world_materialization.preload_authored_npcs`` (a *different* feeder, Task
  3's ``preload_authored``). Task 2 adds a name-keyed
  ``_authored_npc_ids(pack, world_slug)`` lookup so ``_human_patch`` can stamp
  AUTHORED + ``authored_id`` on a backfilled row and MANUAL_POOL on a pregen
  one.

**Load-bearing implementation finding (not in the task brief — discovered
while making the existing MM suite pass):** the brief's kind mapping says
"encounters -> MANUAL_POOL + creature_id" and "region-population ->
REGION_POPULATION + creature_id". Routing either builder's ``creature_id``
into the ``MaterializationCandidate`` Origin is WRONG and breaks production
identity, not just this task's tests:

* Every creature-type encounter enemy in the ENTIRE system shares one
  fallback creature_id. ``pregen.py`` documents that
  ``encountergen.generate_enemy_from_bestiary`` "carries no creature_id";
  ``_creature_patch_from_enemy`` falls back to the raw enemy's ``class``,
  which BOTH content pipelines (``creature_to_enemy_block`` and
  ``generate_enemy_from_bestiary``, encountergen.py) hardcode to the literal
  ``"creature"`` for every creature-type enemy. Confirmed directly by this
  repo's OWN test fixtures: ``tests/server/dispatch/test_monster_manual_
  inject.py::_creature_encounter`` always sets ``"class": "salt_burrower"``
  regardless of ``enemy_name``, and
  ``test_inject_in_combat_materializes_all_available_encounters`` asserts 5
  DISTINCT enemies materialize from 5 encounters that would all resolve to
  the same fallback creature_id.
* ``RegionCreature.creature_type`` is a SPECIES tag, not a per-instance id —
  ``test_inject_region_population_ooc_cap``'s own fixture has five
  distinctly-named ``Mob0..Mob4`` rows all typed ``"mob"`` and asserts they
  materialize as distinct NPCs.

Stamping either as an identity ``creature_id`` would key ``identity_key`` on
that shared tag and collapse every same-species enemy in one scene onto ONE
seat — breaking real encounters (multiple enemies of one type) and the
existing test suite. ``monster_manual_inject._candidate`` therefore passes
``creature_id`` through ONLY for room-binding (a genuine, per-room-authored
bestiary id); encounters and region-population key on normalized display
name instead — exactly their pre-Green-Room dedup behavior, just routed
through ``admit()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.game.monster_manual import EntryState, ManualEncounter, ManualNpc, MonsterManual
from sidequest.game.origin import Origin, OriginKind
from sidequest.game.session import GameSnapshot, NpcPatch
from sidequest.game.turn import TurnManager
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.server.dispatch import monster_manual_inject


def _human(name: str, *, authored: bool = False) -> ManualNpc:
    return ManualNpc(
        data={
            "name": name,
            "role": "wanderer",
            "culture": "Sunden",
            "ocean_summary": "watchful",
        },
        name=name,
        role="wanderer",
        culture="Sunden",
        state=EntryState.AVAILABLE,
        authored=authored,
    )


def _encounter(enemy_name: str, *, tier: int = 1, hp: int = 6) -> ManualEncounter:
    return ManualEncounter(
        data={
            "enemies": [
                {
                    "name": enemy_name,
                    "class": "salt_burrower",
                    "hp": hp,
                    "role": "ambusher",
                }
            ]
        },
        label=f"1x {enemy_name} (tier {tier})",
        tier=tier,
        state=EntryState.AVAILABLE,
    )


@dataclass
class _MMSessionFixture:
    sd: Any
    snapshot: GameSnapshot


@pytest.fixture
def mm_session_fixture(monkeypatch: pytest.MonkeyPatch) -> _MMSessionFixture:
    """A ``_SessionData`` + snapshot with a Manual holding one pregen human and
    one encounter creature; the room-binding and region-population builders
    are staged via module-seam monkeypatches the way
    ``tests/server/test_162_7_all_sources_one_scene_identity.py`` and
    ``tests/server/test_162_2_identity_fork_seating.py`` stage them (their own
    content plumbing — resolve_room_creatures/load_region_population — is
    covered by 107-2/153-x tests elsewhere; this fixture only needs real
    ``NpcPatch``es reaching the injector so feeders 1-4 all fire in one
    ``inject()`` call)."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=3),
    )
    snap.character_locations["Kirk"] = "Salt Camp"

    manual = MonsterManual(
        genre="caverns_and_claudes",
        world="beneath_sunden",
        npcs=[_human("Pregen Wanderer")],
        encounters=[_encounter("Salt Burrower", tier=1, hp=6)],
    )
    sd = SimpleNamespace(
        monster_manual=manual,
        genre_pack=None,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )

    monkeypatch.setattr(
        monster_manual_inject,
        "_npc_patches_for_room_binding",
        lambda *a, **k: [
            NpcPatch(
                name="Gnaw-Swarm",
                creature_id="gnaw_swarm",
                threat_level=1,
                hp=10,
                manual_origin=True,
                origin=Origin(kind=OriginKind.ROOM_BOUND, creature_id="gnaw_swarm"),
            )
        ],
    )
    monkeypatch.setattr(
        monster_manual_inject,
        "_npc_patches_for_region_population",
        lambda *a, **k: [
            NpcPatch(
                name="Pale Lurker",
                creature_id="pale_lurker",
                threat_level=2,
                hp=9,
                region="toods_dome",
                manual_origin=True,
                origin=Origin(kind=OriginKind.REGION_POPULATION, creature_id="pale_lurker"),
            )
        ],
    )

    return _MMSessionFixture(sd=sd, snapshot=snap)


def test_inject_routes_through_admit_spans(
    mm_session_fixture: _MMSessionFixture, otel_capture
) -> None:
    count = monster_manual_inject.inject(
        mm_session_fixture.sd,
        mm_session_fixture.snapshot,
        current_location="Salt Camp",
        in_combat=False,
        room_id="toods_dome",
    )
    assert count >= 4

    materialized = [
        s for s in otel_capture.get_finished_spans() if s.name == "green_room.materialized"
    ]
    assert len(materialized) >= 4, (
        f"expected >=4 green_room.materialized spans (one per feeder's identity); "
        f"got {len(materialized)}"
    )
    sources = {dict(s.attributes or {}).get("canonical_source") for s in materialized}
    assert {
        "mm.available_humans",
        "mm.encounters",
        "mm.room_binding",
        "mm.region_population",
    } <= sources, f"missing feeder source labels in materialized spans: {sources!r}"


def test_reinject_preserves_wounded_hp(mm_session_fixture: _MMSessionFixture) -> None:
    """The ADR-139 Inv-2 carve-out is now structural: re-injection on a later
    turn must not heal a wounded creature — ``green_room.admit()`` never
    touches ``core.hp`` on a merge (only the fixed ``_MERGE_FILL_FIELDS``
    allowlist, which excludes hp)."""
    snap = mm_session_fixture.snapshot
    monster_manual_inject.inject(
        mm_session_fixture.sd, snap, current_location="Salt Camp", in_combat=True
    )
    creature = next(
        n
        for n in snap.npcs
        if n.manual_origin or (n.origin and n.origin.kind.value == "manual_pool")
    )
    creature.core.apply_hp_delta(-3)
    hp_after_wound = creature.core.hp.current

    monster_manual_inject.inject(
        mm_session_fixture.sd, snap, current_location="Salt Camp", in_combat=True
    )
    assert creature.core.hp.current == hp_after_wound
    assert len([n for n in snap.npcs if n.core.name == creature.core.name]) == 1


def test_authored_backfill_human_lands_authored_pregen_lands_manual_pool(otel_capture) -> None:
    """Task-2 rework (reviewer Finding 1): the AUTHORED + authored_id branch.

    A Manual human backfilled from the world's authored ``npcs.yaml`` cast
    (``ManualNpc.authored=True``, name matching an ``AuthoredNpc`` on the
    pack's world so ``_authored_npc_ids`` resolves a real id) must land with
    ``origin.kind == AUTHORED`` carrying that ``authored_id``, and its
    ``green_room.materialized`` span must show tier 1 (the ladder's top rung)
    with source ``mm.available_humans``. A pregen walk-on
    (``authored=False``) in the SAME inject() still lands MANUAL_POOL /
    tier 4."""
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=3),
    )
    snap.character_locations["Kirk"] = "Salt Camp"

    manual = MonsterManual(
        genre="caverns_and_claudes",
        world="beneath_sunden",
        npcs=[
            _human("Brecca Half-Hand", authored=True),
            _human("Pregen Wanderer"),
        ],
    )
    pack = SimpleNamespace(
        worlds={
            "beneath_sunden": SimpleNamespace(
                authored_npcs=[
                    AuthoredNpc(id="brecca_half_hand", name="Brecca Half-Hand", role="camp keeper")
                ]
            )
        },
        rules=SimpleNamespace(combat_encounters=True),
    )
    sd: Any = SimpleNamespace(
        monster_manual=manual,
        genre_pack=pack,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )

    count = monster_manual_inject.inject(sd, snap, current_location="Salt Camp", in_combat=False)
    assert count == 2

    brecca = next(n for n in snap.npcs if n.core.name == "Brecca Half-Hand")
    assert brecca.origin is not None
    assert brecca.origin.kind == OriginKind.AUTHORED
    assert brecca.origin.authored_id == "brecca_half_hand"

    pregen = next(n for n in snap.npcs if n.core.name == "Pregen Wanderer")
    assert pregen.origin is not None
    assert pregen.origin.kind == OriginKind.MANUAL_POOL
    assert pregen.origin.authored_id is None

    materialized: dict[str, dict[str, Any]] = {}
    for s in otel_capture.get_finished_spans():
        if s.name != "green_room.materialized":
            continue
        attrs = dict(s.attributes or {})
        materialized[str(attrs.get("identity_key", ""))] = attrs
    authored_span = materialized.get("authored:brecca_half_hand")
    assert authored_span is not None, (
        f"no materialized span keyed authored:brecca_half_hand — got {sorted(materialized)}"
    )
    assert authored_span["canonical_tier"] == 1
    assert authored_span["canonical_source"] == "mm.available_humans"

    pregen_span = materialized.get("name:pregen wanderer")
    assert pregen_span is not None, (
        f"no materialized span keyed name:pregen wanderer — got {sorted(materialized)}"
    )
    assert pregen_span["canonical_tier"] == 4
    assert pregen_span["canonical_source"] == "mm.available_humans"


def test_spawn_disposition_fires_once_per_admitted_identity_only(
    mm_session_fixture: _MMSessionFixture, otel_capture
) -> None:
    """Task-2 rework (reviewer Finding 2): the story 72-5 spawn-disposition
    lie-detector fires once per identity ``admit()`` genuinely seated —
    NEVER for a candidate that merged onto an already-materialized entry.

    Pre-gate, ``apply_world_patch`` only built an Npc when no same-name entry
    existed, so re-injection never re-fired the span; routing every candidate
    through ``_npc_from_patch`` naively would fire it every turn for the
    whole bench. ``inject()`` therefore builds candidates with
    ``emit_spawn_span=False`` and emits once per ``result.admitted`` after
    the gate decides."""
    from sidequest.telemetry.spans import SPAN_NPC_SPAWN_DISPOSITION

    snap = mm_session_fixture.snapshot

    def spawn_spans() -> list[Any]:
        return [
            s for s in otel_capture.get_finished_spans() if s.name == SPAN_NPC_SPAWN_DISPOSITION
        ]

    count = monster_manual_inject.inject(
        mm_session_fixture.sd,
        snap,
        current_location="Salt Camp",
        in_combat=False,
        room_id="toods_dome",
    )
    first_pass = spawn_spans()
    assert len(first_pass) == count == 4, (
        f"first inject must fire spawn_disposition once per admitted NPC "
        f"(admitted={count}); got {len(first_pass)} spans"
    )

    # Second turn: same manual, same room — every candidate merges onto its
    # existing seat; ZERO additional spawn spans (exact pre-Task-2 parity).
    monster_manual_inject.inject(
        mm_session_fixture.sd,
        snap,
        current_location="Salt Camp",
        in_combat=False,
        room_id="toods_dome",
    )
    second_pass = spawn_spans()
    assert len(second_pass) == len(first_pass), (
        f"re-injection fired {len(second_pass) - len(first_pass)} extra "
        f"npc.spawn_disposition spans for already-materialized identities"
    )
