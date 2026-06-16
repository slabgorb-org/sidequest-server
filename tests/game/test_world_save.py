"""Tests for sidequest.game.world_save.

Hub-world persistence — survives SqliteStore.init_session() reinit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sidequest.game.persistence import SaveSchemaIncompatibleError, SqliteStore
from sidequest.game.world_save import Hireling, WallEntry, WorldSave


def test_hireling_defaults_active_zero_stress():
    h = Hireling(id="vol_1", name="Volga Stein", archetype="prig")
    assert h.stress == 0
    assert h.status == "active"
    assert h.recruited_at_delve == 0
    assert h.notes == ""


def test_hireling_status_validates_literal():
    with pytest.raises(ValidationError):
        Hireling(id="vol_1", name="x", archetype="x", status="ghost")  # type: ignore[arg-type]


def test_hireling_id_pattern_enforced():
    """Item 4a's recruit generator and items 5/6/7's narrator-zone
    consumers share this contract — locked at model boundary."""
    # Valid shapes
    Hireling(id="vol_1", name="x", archetype="x")
    Hireling(id="prig_a3f", name="x", archetype="x")
    # Invalid shapes — must fail loud, no silent normalization
    for bad in ("Vol_1", "1vol", "vol-1", "vol 1", "", "vol!"):
        with pytest.raises(ValidationError):
            Hireling(id=bad, name="x", archetype="x")


def test_wall_entry_required_fields():
    e = WallEntry(
        delve_number=1,
        sin="pride",
        dungeon="grimvault",
        party_hireling_ids=["a", "b"],
        outcome="victory",
        timestamp=datetime.now(tz=UTC),
    )
    assert e.delve_number == 1
    assert e.party_hireling_ids == ["a", "b"]
    assert e.wounded_boss is False  # default


def test_wall_entry_outcome_validates_literal():
    """Outcome is the party-fate literal — wounded_dungeon is NOT here.
    Wound status lives on the orthogonal ``wounded_boss`` bool so that
    e.g. a TPK-after-wound is recordable as ``outcome=defeat``,
    ``wounded_boss=True``."""
    with pytest.raises(ValidationError):
        WallEntry(
            delve_number=1,
            sin="pride",
            dungeon="grimvault",
            party_hireling_ids=[],
            outcome="wounded_dungeon",  # rejected — not a party-fate
            timestamp=datetime.now(tz=UTC),
        )


def test_wall_entry_wounded_boss_is_orthogonal_to_outcome():
    """All four (outcome, wounded_boss) combinations must construct."""
    for outcome in ("victory", "defeat", "retreat"):
        for wounded in (True, False):
            e = WallEntry(
                delve_number=1,
                sin="pride",
                dungeon="grimvault",
                party_hireling_ids=[],
                outcome=outcome,
                wounded_boss=wounded,
                timestamp=datetime.now(tz=UTC),
            )
            assert e.outcome == outcome
            assert e.wounded_boss is wounded


def test_world_save_defaults_empty():
    ws = WorldSave()
    assert ws.roster == []
    assert ws.currency == 0
    assert ws.wall == []
    assert ws.dungeon_wounds == {}
    assert ws.latest_delve_sin is None
    assert ws.delve_count == 0
    assert ws.last_saved_at is None


def test_world_save_round_trip_json():
    ws = WorldSave(
        roster=[Hireling(id="vol_1", name="Volga", archetype="prig")],
        currency=42,
        dungeon_wounds={"grimvault": True},
        latest_delve_sin="pride",
        delve_count=3,
    )
    raw = ws.model_dump_json()
    ws2 = WorldSave.model_validate_json(raw)
    assert ws2.roster[0].name == "Volga"
    assert ws2.currency == 42
    assert ws2.dungeon_wounds == {"grimvault": True}
    assert ws2.latest_delve_sin == "pride"
    assert ws2.delve_count == 3


def test_load_world_save_empty_returns_default():
    store = SqliteStore.open_in_memory()
    ws = store.load_world_save()
    assert ws.roster == []
    assert ws.currency == 0
    assert ws.delve_count == 0


def test_load_world_save_invalid_json_raises():
    store = SqliteStore.open_in_memory()
    store._conn.execute(
        "INSERT INTO world_save (id, payload_json, saved_at) VALUES (1, ?, ?)",
        ("not json", "2026-05-05T00:00:00+00:00"),
    )
    store._conn.commit()
    with pytest.raises(SaveSchemaIncompatibleError):
        store.load_world_save()


def test_save_world_save_round_trip():
    store = SqliteStore.open_in_memory()
    ws = WorldSave(
        roster=[Hireling(id="x_1", name="X", archetype="x")],
        currency=10,
        delve_count=1,
    )
    store.save_world_save(ws)
    reloaded = store.load_world_save()
    assert reloaded.currency == 10
    assert reloaded.delve_count == 1
    assert len(reloaded.roster) == 1
    assert reloaded.roster[0].name == "X"
    assert reloaded.last_saved_at is not None  # save_world_save stamps it


def test_save_world_save_overwrites_singleton():
    store = SqliteStore.open_in_memory()
    store.save_world_save(WorldSave(currency=1))
    store.save_world_save(WorldSave(currency=2))
    rows = store._conn.execute("SELECT COUNT(*) FROM world_save").fetchone()
    assert rows[0] == 1, "INSERT OR REPLACE must keep singleton invariant"
    assert store.load_world_save().currency == 2


def test_init_session_preserves_world_save_across_reinit():
    """Hub-state persistence guarantee — Sünden engine plan item 2.

    A delve-end / fresh-delve flow calls ``init_session()`` to clear
    per-slot tables. The roster, currency, Wall, wound flags, and
    drift flag MUST survive that reinit; otherwise a hireling roster
    is emptied between delves and the entire DD-shaped loop falls
    apart.
    """
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_three_sins")

    ws = WorldSave(
        roster=[Hireling(id="vol_1", name="Volga", archetype="prig", stress=15)],
        currency=42,
        wall=[
            WallEntry(
                delve_number=1,
                sin="pride",
                dungeon="grimvault",
                party_hireling_ids=["vol_1"],
                outcome="victory",
                wounded_boss=True,
                timestamp=datetime.now(tz=UTC),
            )
        ],
        dungeon_wounds={"grimvault": True},
        latest_delve_sin="pride",
        delve_count=1,
    )
    store.save_world_save(ws)

    # Simulate the next delve starting — slot reinit clears per-slot tables.
    store.init_session("caverns_and_claudes", "caverns_three_sins")

    reloaded = store.load_world_save()
    assert reloaded.currency == 42
    assert reloaded.delve_count == 1
    assert reloaded.roster[0].name == "Volga"
    assert reloaded.roster[0].stress == 15
    assert reloaded.wall[0].sin == "pride"
    assert reloaded.wall[0].dungeon == "grimvault"
    assert reloaded.wall[0].wounded_boss is True
    assert reloaded.dungeon_wounds == {"grimvault": True}
    assert reloaded.latest_delve_sin == "pride"


def test_init_session_clears_game_state_but_keeps_world_save():
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_three_sins")
    # populate game_state and world_save
    store._conn.execute(
        "INSERT INTO game_state (id, snapshot_json, saved_at) VALUES (1, '{}', ?)",
        (datetime.now(tz=UTC).isoformat(),),
    )
    store._conn.commit()
    store.save_world_save(WorldSave(currency=99))

    store.init_session("caverns_and_claudes", "caverns_three_sins")

    game_state_rows = store._conn.execute("SELECT COUNT(*) FROM game_state").fetchone()[0]
    world_save_rows = store._conn.execute("SELECT COUNT(*) FROM world_save").fetchone()[0]
    assert game_state_rows == 0, "game_state must be cleared by init_session"
    assert world_save_rows == 1, "world_save must NOT be cleared by init_session"
    assert store.load_world_save().currency == 99


def test_world_save_round_trip_file_backed(tmp_path):
    """File-backed round-trip across reopen — documents SQLite persistence."""
    db_path = tmp_path / "save.db"
    store = SqliteStore(db_path)
    store.init_session("caverns_and_claudes", "caverns_three_sins")
    store.save_world_save(WorldSave(currency=7, delve_count=1))
    store.close()

    # Re-open
    store2 = SqliteStore(db_path)
    ws = store2.load_world_save()
    assert ws.currency == 7
    assert ws.delve_count == 1
