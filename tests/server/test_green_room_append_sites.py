"""Every production append routes through admit() — proven by behavior,
not source text: each path fires green_room.materialized with its source
(Green Room Task 3, ADR-156).

Two representative sites, one per conversion shape:

* ``preload_authored_npcs`` (world_materialization.py) — a fresh, unstamped
  Npc that must be freshly stamped AUTHORED at the gate.
* the 162-3 generics last-resort seat (encounter_lifecycle.py) — an Npc that
  already carries a stamped ``Origin(kind=GENERIC, ...)`` and must pass
  through the gate verbatim, still emitting the materialized span.

Fixtures are lifted verbatim from the two files the task-3 brief cites —
``tests/game/test_world_materialization_authored_npcs.py`` (the actual home
of ``preload_authored_npcs`` coverage; ``test_world_materialization.py``
itself covers ``WorldBuilder``/chapter materialization, not the authored
preload) and ``tests/server/test_162_3_generics_last_resort_seating.py`` —
not reinvented.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.world_materialization import preload_authored_npcs
from sidequest.genre.models.authored_npc import AuthoredNpc
from tests.server.test_162_3_generics_last_resort_seating import (
    _ROUTER_NAME,
    _drive,
    _generics_pack,
    _snapshot,
)


def _make_authored_npc(npc_id: str, disposition: int = 0) -> AuthoredNpc:
    # Same shape as test_world_materialization_authored_npcs.py's _make_npc.
    return AuthoredNpc(
        id=npc_id,
        name=f"Authored-{npc_id}",
        pronouns="they/them",
        role="crew",
        appearance="brief description",
        initial_disposition=disposition,
    )


def test_authored_preload_admits(otel_capture) -> None:
    # Drive preload_authored_npcs with a 1-NPC authored world fixture — the
    # exact MagicMock-state / _make_npc shape
    # test_world_materialization_authored_npcs.py's test_fresh_session_
    # preloads_npcs uses.
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=1)

    authored = [_make_authored_npc("captain", disposition=60)]

    preload_authored_npcs(state, authored)

    assert len(state.npcs) == 1
    materialized = [
        s for s in otel_capture.get_finished_spans() if s.name == "green_room.materialized"
    ]
    assert any(
        dict(s.attributes or {}).get("canonical_source") == "preload_authored"
        and dict(s.attributes or {}).get("canonical_tier") == 1
        for s in materialized
    ), (
        f"expected a preload_authored green_room.materialized span (tier 1); got "
        f"{[dict(s.attributes or {}) for s in materialized]}"
    )


def test_generics_seat_admits_under_router_name(otel_capture) -> None:
    # Drive instantiate_encounter_from_trigger with a materialized_threat
    # naming an unknown person (_ROUTER_NAME) and a pack whose bestiary has
    # one generics row — staged exactly as
    # TestGenericsSeatTheLastResortOther.test_unbacked_opponent_seats_from_
    # generics_not_stub does.
    snap = _snapshot()
    pack = _generics_pack()

    enc = _drive(snap, pack)

    assert enc is not None
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "green_room.materialized"]
    assert any(
        dict(s.attributes or {}).get("canonical_source") == "seeder.generics" for s in spans
    ), (
        f"expected a seeder.generics green_room.materialized span; got "
        f"{[dict(s.attributes or {}) for s in spans]}"
    )
    opponent = next(a for a in enc.actors if a.side == "opponent")
    # The seat keeps the ROUTER-given name (narrator continuity) — the
    # bestiary generic row donates stats/creature_id only, not identity.
    assert opponent.name == _ROUTER_NAME
