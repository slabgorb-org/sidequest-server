"""emit_mechanical_census: one component='mechanical' census row per
SEATED PC + one session trope_census row, all inside the open C2 txn,
event_seq attributed, rolled back with the turn. Sealed-rounds: every
seated PC every round, no acting-player bias (ADR-036)."""

from sidequest.game.mechanical_census import emit_mechanical_census
from sidequest.game.persistence import SqliteStore
from sidequest.telemetry.watcher_hub import bind_event_store


def _store(tmp_path):
    return SqliteStore.open(str(tmp_path / "save.db"))


class _HpPool:
    current, max, base_max = 10, 10, 10


class _Inv:
    items = [{"name": "torch", "quantity": 1}]
    gold = 0


def _char(name):
    core = type(
        "C",
        (),
        {
            "name": name,
            "xp": 0,
            "level": 1,
            "acquired_advancements": [],
            "statuses": [],
            "hp": _HpPool(),
            "inventory": _Inv(),
        },
    )()
    return type(
        "Ch",
        (),
        {
            "core": core,
            "current_room": None,
            "abilities": [],
            "is_broken": lambda self: False,
        },
    )()


class _Snap:
    active_tropes = []
    turns_since_meaningful = 0
    total_beats_fired = 0
    character_locations = {"Rux": "Cave", "Vex": "Cave"}

    class turn_manager:  # noqa: N801
        interaction = 5

    characters = [_char("Rux"), _char("Vex")]
    player_seats = {"p1": "Rux", "p2": "Vex"}


class _Room:
    def playing_player_ids(self):
        return ["p1", "p2"]


def test_emits_one_census_per_seated_pc_plus_one_trope_in_txn(tmp_path):
    store = _store(tmp_path)
    try:
        bind_event_store(store)
        conn = store._conn
        with conn:  # simulate emit_event's C2 transaction
            conn.execute(
                "INSERT INTO events (kind, payload_json, created_at) "
                "VALUES ('NARRATION', '{}', 't')"
            )
            emit_mechanical_census(_Room(), _Snap())
            rows = conn.execute(
                "SELECT event_seq, round, component, event_type, payload_json "
                "FROM turn_telemetry ORDER BY seq"
            ).fetchall()
        # 2 seated PCs + 1 session trope row, all event_seq=1 (this turn)
        assert [(r[0], r[2], r[3]) for r in rows] == [
            (1, "mechanical", "census"),
            (1, "mechanical", "census"),
            (1, "mechanical", "trope_census"),
        ]
        assert all(r[1] == 5 for r in rows)  # round = turn_manager.interaction
        import json

        pcs = {json.loads(r[4])["player_id"] for r in rows if r[3] == "census"}
        assert pcs == {"p1", "p2"}  # no acting-player bias
    finally:
        bind_event_store(None)
        store.close()


def test_empty_roster_emits_nothing_and_does_not_raise(tmp_path):
    store = _store(tmp_path)
    try:
        bind_event_store(store)

        class _Empty:
            def playing_player_ids(self):
                return []

        class _S(_Snap):
            player_seats: dict = {}

        with store._conn:
            store._conn.execute(
                "INSERT INTO events (kind, payload_json, created_at) VALUES ('NARRATION','{}','t')"
            )
            emit_mechanical_census(_Empty(), _S())  # must not raise
        assert (
            store._conn.execute("SELECT COUNT(*) FROM turn_telemetry").fetchone()[0] == 0
        )  # honest no-rows, not an error
    finally:
        bind_event_store(None)
        store.close()
