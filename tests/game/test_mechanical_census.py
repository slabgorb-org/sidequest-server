"""Per-subsystem accuracy: each census field is read from the CANONICAL
game model for a seeded character (anti-log-absence — every gap closed
against real state, not a hoped-for emitter)."""

from sidequest.game.mechanical_census import (
    build_pc_census,
    build_trope_census,
    emit_mechanical_census,
    inv_hash,
    inventory_digest,
    seat_index,
)
from sidequest.game.persistence import SqliteStore
from sidequest.telemetry.watcher_hub import bind_event_store


# --- inventory_digest: aggregate by name, sum quantity, singleton-safe ---
def test_inventory_digest_aggregates_singletons_by_name():
    items = [
        {"name": "torch", "quantity": 1},
        {"name": "torch", "quantity": 1},  # narrator singleton dup (R7)
        {"name": "brass key"},  # no quantity -> 1
        {"name": "rations", "quantity": 3},
    ]
    assert inventory_digest(items) == [
        {"item": "brass key", "qty": 1},
        {"item": "rations", "qty": 3},
        {"item": "torch", "qty": 2},
    ]


def test_inventory_digest_skips_nameless_entries_loudly(caplog):
    with caplog.at_level("WARNING"):
        d = inventory_digest([{"quantity": 2}, {"name": "rope", "quantity": 1}])
    assert d == [{"item": "rope", "qty": 1}]
    assert "mechanical_census.inventory_unnamed_entry" in caplog.text


def test_inv_hash_is_stable_and_order_independent():
    a = inv_hash([{"name": "b"}, {"name": "a", "quantity": 2}])
    b = inv_hash([{"name": "a", "quantity": 2}, {"name": "b"}])
    assert a == b and isinstance(a, str) and len(a) == 16


# --- seat_index: positional in playing_player_ids, never raises (R9) ---
class _Room:
    def __init__(self, ids):
        self._ids = ids

    def playing_player_ids(self):
        return list(self._ids)


def test_seat_index_is_positional_and_defensive():
    room = _Room(["p2", "p1", "p3"])
    assert seat_index(room, "p1") == 1
    assert seat_index(room, "ghost") == -1  # absent -> -1, never raises
    assert seat_index(None, "p1") == -1  # no room -> -1, never raises


# --- build_pc_census: canonical reads (R3/R4/R6/R7) ---
class _Edge:
    current, max, base_max = 7, 12, 12


class _Inv:
    items = [{"name": "torch", "quantity": 1}, {"name": "torch", "quantity": 1}]
    gold = 9


class _Core:
    name = "Rux"
    xp = 150
    level = 3
    acquired_advancements = ["adv.iron_grip"]
    statuses = [type("S", (), {"text": "Wound: ribs", "severity": "wound"})()]
    hp = _Edge()
    inventory = _Inv()


class _Char:
    core = _Core()
    current_room = "antechamber"
    abilities = [object(), object()]

    def is_broken(self):
        return self.core.hp.current <= 0


def test_build_pc_census_reads_every_gap_subsystem():
    c = build_pc_census(
        character=_Char(),
        player_id="p1",
        character_name="Rux",
        seat=0,
        round_number=4,
        location="The Kept Fire",
    )
    assert c["player_id"] == "p1"
    assert c["character_name"] == "Rux"
    assert c["seat"] == 0
    assert c["round"] == 4
    assert c["interaction"] == 4
    assert c["location"] == "The Kept Fire"
    assert c["chassis_room"] == "antechamber"
    assert c["edge"] == {"current": 7, "max": 12, "base_max": 12}
    assert c["down"] is False
    assert c["statuses"] == [{"text": "Wound: ribs", "severity": "wound"}]
    assert c["inventory"] == [{"item": "torch", "qty": 2}]
    assert c["inv_hash"] == inv_hash(_Inv.items)
    assert c["gold"] == 9
    assert c["xp"] == 150
    assert c["level"] == 3
    assert c["acquired_advancements"] == ["adv.iron_grip"]
    assert c["ability_count"] == 2
    # R4: no fabricated tier / pending_advancements
    assert "tier" not in c and "pending_advancements" not in c
    # R3: no separate composure field
    assert "composure" not in c


def test_build_pc_census_none_location_is_honest_none():
    c = build_pc_census(
        character=_Char(),
        player_id="p1",
        character_name="Rux",
        seat=0,
        round_number=1,
        location=None,
    )
    assert c["location"] is None  # absent scene -> None, not "" or fabricated


# --- build_trope_census: session-level, NOT per-PC (R5) ---
class _Trope:
    def __init__(self, tid, status, prog, beats):
        self.id, self.status, self.progress = tid, status, prog
        self.beats_fired, self.last_fired_turn = beats, 3
        self.fire_cooldown_until = None


class _Snap:
    active_tropes = [_Trope("vengeance", "active", 0.4, 2)]
    turns_since_meaningful = 1
    total_beats_fired = 5


def test_build_trope_census_is_session_scoped():
    t = build_trope_census(_Snap(), round_number=4)
    assert t["round"] == 4
    assert t["turns_since_meaningful"] == 1
    assert t["total_beats_fired"] == 5
    assert t["active_tropes"] == [
        {
            "id": "vengeance",
            "status": "active",
            "progress": 0.4,
            "beats_fired": 2,
            "last_fired_turn": 3,
        }
    ]


def test_one_bad_pc_is_isolated_others_and_trope_still_emit(tmp_path, caplog):
    """A PC whose build raises must loud-log mechanical_census.build_failed
    and NOT drop the healthy PC or the session trope row."""
    store = SqliteStore.open(str(tmp_path / "s.db"))
    try:
        bind_event_store(store)

        class _GoodCore:
            name = "Rux"
            xp = 0
            level = 1
            acquired_advancements: list = []
            statuses: list = []
            hp = type("E", (), {"current": 5, "max": 5, "base_max": 5})()
            inventory = type("I", (), {"items": [], "gold": 0})()

        good = type(
            "G",
            (),
            {
                "core": _GoodCore(),
                "current_room": None,
                "abilities": [],
                "is_broken": lambda self: False,
            },
        )()

        class _BadCore:
            name = "Vex"

            @property
            def hp(self):
                raise RuntimeError("corrupt hp pool")

        bad = type(
            "B",
            (),
            {
                "core": _BadCore(),
                "current_room": None,
                "abilities": [],
                "is_broken": lambda self: False,
            },
        )()

        class _Snap:
            active_tropes: list = []
            turns_since_meaningful = 0
            total_beats_fired = 0
            character_locations = {"Rux": "Cave", "Vex": "Cave"}
            characters = [good, bad]
            player_seats = {"p1": "Rux", "p2": "Vex"}

            class turn_manager:  # noqa: N801
                interaction = 2

        class _Room:
            def playing_player_ids(self):
                return ["p1", "p2"]

        with caplog.at_level("WARNING"), store._conn:
            store._conn.execute(
                "INSERT INTO events (kind, payload_json, created_at) VALUES ('NARRATION','{}','t')"
            )
            emit_mechanical_census(_Room(), _Snap())  # must not raise
        rows = store._conn.execute(
            "SELECT event_type, payload_json FROM turn_telemetry ORDER BY seq"
        ).fetchall()
        types = [r[0] for r in rows]
        assert types == ["census", "trope_census"]  # good PC + trope kept
        assert "mechanical_census.build_failed pc=p2" in caplog.text
    finally:
        bind_event_store(None)
        store.close()


def test_census_payload_never_sets_encounter_field(tmp_path):
    """The adjacent _maybe_persist_encounter_row hazard only fires on
    fields['field']=='encounter'. A census must never carry that key
    (defends the open C2 txn from a premature commit)."""

    class _C:
        core = type(
            "C",
            (),
            {
                "name": "Rux",
                "xp": 0,
                "level": 1,
                "acquired_advancements": [],
                "statuses": [],
                "hp": type("E", (), {"current": 1, "max": 1, "base_max": 1})(),
                "inventory": type("I", (), {"items": [], "gold": 0})(),
            },
        )()
        current_room = None
        abilities: list = []

        def is_broken(self):
            return False

    c = build_pc_census(
        character=_C(),
        player_id="p1",
        character_name="Rux",
        seat=0,
        round_number=1,
        location=None,
    )
    assert "field" not in c or c.get("field") != "encounter"
