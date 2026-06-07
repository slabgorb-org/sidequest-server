from unittest.mock import MagicMock

import pytest

from sidequest.game.creature_core import HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.status import Status, StatusSeverity
from sidequest.server.dispatch.yield_action import handle_yield
from sidequest.server.session_room import SessionRoom


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: the watcher-events test persists/reads ENCOUNTER rows via PG)."""
    import psycopg

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


def _room_for(snap, tmp_path):
    """Bind the snapshot to a fresh SessionRoom for handle_yield wiring."""
    room = SessionRoom(slug="test_world", mode=GameMode.SOLO)
    room.bind_world(snapshot=snap, store=MagicMock(spec=SaveRepository))
    return room


def _enc(*, p_metric=4, o_metric=7):
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=p_metric, starting=0, threshold=10),
        opponent_metric=EncounterMetric(
            name="momentum", current=o_metric, starting=0, threshold=10
        ),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )


def test_yield_solo_pc_resolves_encounter_immediately(
    snapshot_with_pack, character_named_sam, tmp_path
):
    snap, _ = snapshot_with_pack
    snap.encounter = _enc()
    snap.characters.append(character_named_sam)
    room = _room_for(snap, tmp_path)
    handle_yield(snap, room=room, player_id="p1", player_name="Sam")
    assert snap.encounter.resolved is True
    assert snap.encounter.outcome == "yielded"
    assert snap.pending_resolution_signal.outcome == "yielded"
    assert snap.pending_resolution_signal.yielded_actors == ("Sam",)


def test_yield_refunds_edge_one_plus_status_count(
    snapshot_with_pack, character_named_sam, tmp_path
):
    snap, _ = snapshot_with_pack
    snap.encounter = _enc()
    sam = character_named_sam
    sam.core.statuses.extend(
        [
            Status(
                text="Bruised Ribs",
                severity=StatusSeverity.Wound,
                absorbed_shifts=0,
                created_turn=2,
                created_in_encounter="combat",
            ),
            Status(
                text="Mocked",
                severity=StatusSeverity.Scratch,
                absorbed_shifts=0,
                created_turn=3,
                created_in_encounter="combat",
            ),
        ]
    )
    sam.core.hp.current = 0
    sam.core.hp.max = 5
    snap.characters.append(sam)
    room = _room_for(snap, tmp_path)
    handle_yield(snap, room=room, player_id="p1", player_name="Sam")
    # Both statuses created in this encounter → refund 1 + 2 = 3
    assert sam.core.hp.current == 3
    assert snap.pending_resolution_signal.edge_refreshed == 3


def test_yield_does_not_count_pre_existing_statuses(
    snapshot_with_pack, character_named_sam, tmp_path
):
    snap, _ = snapshot_with_pack
    snap.encounter = _enc()
    sam = character_named_sam
    sam.core.statuses.append(
        Status(
            text="Old Scar",
            severity=StatusSeverity.Scar,
            absorbed_shifts=0,
            created_turn=0,
            created_in_encounter=None,
        )
    )
    sam.core.hp.current = 0
    sam.core.hp.max = 5
    snap.characters.append(sam)
    room = _room_for(snap, tmp_path)
    handle_yield(snap, room=room, player_id="p1", player_name="Sam")
    # Pre-existing status not in this encounter → refund 1 + 0 = 1
    assert sam.core.hp.current == 1


def test_yield_caps_at_edge_max(snapshot_with_pack, character_named_sam, tmp_path):
    snap, _ = snapshot_with_pack
    snap.encounter = _enc()
    sam = character_named_sam
    sam.core.hp.current = 4
    sam.core.hp.max = 5
    snap.characters.append(sam)
    room = _room_for(snap, tmp_path)
    handle_yield(snap, room=room, player_id="p1", player_name="Sam")
    assert sam.core.hp.current == 5  # capped at max


def test_yield_with_no_active_encounter_raises(snapshot_with_pack, character_named_sam, tmp_path):
    snap, _ = snapshot_with_pack
    snap.encounter = None
    snap.characters.append(character_named_sam)
    room = _room_for(snap, tmp_path)
    with pytest.raises(ValueError, match="no active encounter"):
        handle_yield(snap, room=room, player_id="p1", player_name="Sam")


def test_yield_with_two_pcs_first_yield_keeps_encounter_active(snapshot_with_pack, tmp_path):
    snap, _ = snapshot_with_pack
    enc = _enc()
    enc.actors.append(EncounterActor(name="Alex", role="combatant", side="player"))
    snap.encounter = enc
    # Each PC needs a Character entry
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore

    snap.characters.append(
        Character(
            core=CreatureCore(
                name="Sam", description="x", personality="x", hp=HpPool(current=10, max=10, base_max=10)
            ),
            backstory="x",
            char_class="Rogue",
            race="Human",
        )
    )
    snap.characters.append(
        Character(
            core=CreatureCore(
                name="Alex", description="x", personality="x", hp=HpPool(current=10, max=10, base_max=10)
            ),
            backstory="x",
            char_class="Warrior",
            race="Elf",
        )
    )
    room = _room_for(snap, tmp_path)
    handle_yield(snap, room=room, player_id="p1", player_name="Sam")
    # Sam withdrawn; Alex still active → encounter not resolved
    assert snap.encounter.resolved is False
    assert next(a for a in snap.encounter.actors if a.name == "Sam").withdrawn is True
    assert next(a for a in snap.encounter.actors if a.name == "Alex").withdrawn is False

    # Alex yields too → resolves
    handle_yield(snap, room=room, player_id="p2", player_name="Alex")
    assert snap.encounter.resolved is True
    assert snap.encounter.outcome == "yielded"
    assert set(snap.pending_resolution_signal.yielded_actors) == {"Sam", "Alex"}


def test_yield_emits_watcher_events_with_resolved_last(
    snapshot_with_pack, character_named_sam, tmp_path
):
    """Watcher events fire in row order: yield_received → yield_resolved → resolved.
    The kinds[-1] == ENCOUNTER_RESOLVED invariant must hold for solo yield."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug
    from sidequest.telemetry.watcher_hub import bind_event_store

    snap, _ = snapshot_with_pack
    snap.encounter = _enc()
    snap.characters.append(character_named_sam)

    # ADR-115 F1: encounter rows persist to Postgres via the bound
    # PgTelemetrySink; read them back through the repository.
    repo, _dungeon, sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug="yield-watcher-events",
        mode="solo",
        genre_slug="test_pack",
        world_slug="test_world",
    )
    bind_event_store(sink)
    try:
        room = _room_for(snap, tmp_path)
        handle_yield(snap, room=room, player_id="p1", player_name="Sam")
        kinds = [
            r.kind for r in repo.read_events_since(since_seq=0) if r.kind.startswith("ENCOUNTER_")
        ]
        assert "ENCOUNTER_YIELD" in kinds, f"missing ENCOUNTER_YIELD; got {kinds}"
        assert kinds[-1] == "ENCOUNTER_RESOLVED", (
            f"last row must be ENCOUNTER_RESOLVED; got {kinds}"
        )
    finally:
        bind_event_store(None)


def test_yield_solo_pc_with_companion_resolves_encounter_immediately(
    snapshot_with_pack, character_named_sam, tmp_path
):
    """Playtest 2026-05-06 regression (sumpdrake fight, caverns_sunden):

    A solo PC with a recruited NPC companion on the player side must
    resolve the encounter on yield. Pre-fix the gate counted every
    player-side actor — including the companion — as 'still owing a
    beat', so yield logged remaining_player_actors=1 with no
    seated player to ever click for the missing actor. Encounter
    deadlocked: action surface re-presented, no narration advanced.

    Filter: only actors whose name appears in snapshot.player_seats
    block yield resolution. Companions follow their patron; they never
    block the gate.
    """
    snap, _ = snapshot_with_pack
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=2, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=4, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Donut", role="ally", side="player"),
            EncounterActor(name="Sumpdrake", role="combatant", side="opponent"),
        ],
    )
    snap.characters.append(character_named_sam)
    # Solo seat — only Sam is a seated PC. Donut is an NPC companion.
    snap.player_seats = {"p1": "Sam"}

    room = _room_for(snap, tmp_path)
    handle_yield(snap, room=room, player_id="p1", player_name="Sam")

    assert snap.encounter.resolved is True, (
        "solo yield with NPC companion on the player side must resolve — "
        "companions follow their patron"
    )
    assert snap.encounter.outcome == "yielded"
    # Only the explicit yielder ends up withdrawn; the companion is not
    # marked withdrawn (the gate excludes them, but doesn't auto-yield
    # them either). The manifest carries actors that ARE withdrawn.
    assert snap.pending_resolution_signal.yielded_actors == ("Sam",)


def test_yield_partial_blocked_by_other_seated_pc(
    snapshot_with_pack, character_named_sam, tmp_path
):
    """Multiplayer parity check: when there is more than one seated PC,
    one PC's yield must NOT resolve the encounter — the other seated PC
    still has to commit. The seat-aware gate from the solo fix must not
    weaken multiplayer semantics.
    """
    snap, _ = snapshot_with_pack
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Riley", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )
    snap.characters.append(character_named_sam)
    snap.player_seats = {"p1": "Sam", "p2": "Riley"}

    room = _room_for(snap, tmp_path)
    handle_yield(snap, room=room, player_id="p1", player_name="Sam")

    assert snap.encounter.resolved is False, (
        "encounter must persist while another seated PC has not yielded"
    )
    sam_actor = snap.encounter.find_actor("Sam")
    assert sam_actor is not None and sam_actor.withdrawn is True
    riley_actor = snap.encounter.find_actor("Riley")
    assert riley_actor is not None and riley_actor.withdrawn is False
