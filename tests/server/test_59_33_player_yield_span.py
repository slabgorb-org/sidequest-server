"""RED test — Story 59-33: player-yield ``yield_side='player'`` on the emitted span.

The player-yield path (``dispatch/yield_action.py::handle_yield``) ALREADY emits
the ``encounter.resolution_signal_emitted`` OTEL span (yield_action.py:210) — this
story adds ``yield_side='player'`` to it (the live GM-panel consumer per Keith's
Option-C ruling) and to the ResolutionSignal it stamps on the snapshot.

Separate file from the opponent/dial wiring because ``handle_yield`` →
``room.session.end_scene`` persists to Postgres, so it needs the PG isolation
harness (mirrors ``tests/server/test_yield_dispatch.py``).

RED today: the span fires but carries NO ``yield_side`` attr, and the stamped
ResolutionSignal has no ``yield_side`` field.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest

from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.server.dispatch.yield_action import handle_yield
from sidequest.server.session_room import SessionRoom

_SIGNAL_EMITTED_SPAN = "encounter.resolution_signal_emitted"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Per-worker throwaway PG db (handle_yield → end_scene persists ENCOUNTER
    rows). Mirrors tests/server/test_yield_dispatch.py::_pg_isolation."""
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _room_for(snap) -> SessionRoom:
    room = SessionRoom(slug="test_world", mode=GameMode.SOLO)
    room.bind_world(snapshot=snap, store=MagicMock(spec=SaveRepository))
    return room


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=4, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=7, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )


def test_player_yield_emits_signal_span_with_yield_side_player(
    snapshot_with_pack, character_named_sam, otel_capture
) -> None:
    """A player yield stamps yield_side='player' on the snapshot signal AND emits
    it on the resolution OTEL span (the live consumer)."""
    snap, _ = snapshot_with_pack
    snap.encounter = _enc()
    snap.characters.append(character_named_sam)
    room = _room_for(snap)

    handle_yield(snap, room=room, player_id="p1", player_name="Sam")

    sig = snap.pending_resolution_signal
    assert sig is not None
    assert sig.outcome == "yielded"
    assert sig.yield_side == "player", (
        f"a player yield must record yield_side='player'; got {sig.yield_side!r}"
    )

    spans = [s for s in otel_capture.get_finished_spans() if s.name == _SIGNAL_EMITTED_SPAN]
    assert len(spans) == 1, (
        f"player yield must emit one {_SIGNAL_EMITTED_SPAN} span carrying yield_side; "
        f"got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert dict(spans[0].attributes or {}).get("yield_side") == "player"
