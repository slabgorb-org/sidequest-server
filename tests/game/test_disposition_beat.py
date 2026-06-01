from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import (
    DISPOSITION_LOG_CAP,
    PATCH_BEAT_REASON,
    DispositionBeat,
)
from sidequest.game.session import GameSnapshot, Npc, WorldStatePatch


def test_disposition_beat_fields():
    beat = DispositionBeat(turn=4, delta=3, reason="warmed by your candor", location="parlor")
    assert beat.turn == 4
    assert beat.delta == 3
    assert beat.reason == "warmed by your candor"
    assert beat.location == "parlor"


def test_disposition_beat_location_optional():
    beat = DispositionBeat(turn=1, delta=-2, reason="snubbed")
    assert beat.location is None


def test_disposition_log_cap_is_ten():
    assert DISPOSITION_LOG_CAP == 10


def _npc(name: str = "Tabitha") -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="A guest.",
            personality="Wry.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
            acquired_advancements=[],
        )
    )


def test_record_beat_appends():
    npc = _npc()
    npc.record_disposition_beat(turn=2, delta=3, reason="candor", location="parlor")
    assert len(npc.disposition_log) == 1
    assert npc.disposition_log[0].delta == 3


def test_record_beat_skips_zero_delta():
    npc = _npc()
    npc.record_disposition_beat(turn=2, delta=0, reason="no change", location=None)
    assert npc.disposition_log == []


def test_record_beat_trims_to_cap():
    npc = _npc()
    for i in range(1, 16):
        npc.record_disposition_beat(turn=i, delta=1, reason=f"r{i}", location=None)
    assert len(npc.disposition_log) == DISPOSITION_LOG_CAP
    # oldest trimmed, newest kept
    assert npc.disposition_log[0].turn == 6
    assert npc.disposition_log[-1].turn == 15


def test_record_beat_emits_span():
    from sidequest.telemetry.spans import SPAN_RELATIONSHIP_BEAT_RECORDED

    assert SPAN_RELATIONSHIP_BEAT_RECORDED == "relationship.beat_recorded"


def test_patch_beat_reason_constant():
    assert isinstance(PATCH_BEAT_REASON, str) and PATCH_BEAT_REASON


# ---------------------------------------------------------------------------
# Site 3/4 (ADR-136): apply_world_patch npc_attitudes records a beat.
#
# These drive the REAL production path: a GameSnapshot carrying one Npc, fed
# a WorldStatePatch with an npc_attitudes delta, must append a DispositionBeat
# with the EFFECTIVE (post-clamp) delta and the neutral PATCH_BEAT_REASON
# label. Construction mirrors tests/integration/test_disposition_threshold_crossing.py.
# ---------------------------------------------------------------------------


def _snapshot_with_npc(npc: Npc) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[],
        npcs=[npc],
    )


def test_apply_world_patch_records_disposition_beat():
    npc = _npc("Bartender")
    snapshot = _snapshot_with_npc(npc)
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Bartender": 5}))

    assert len(npc.disposition_log) == 1
    beat = npc.disposition_log[-1]
    assert beat.delta == 5
    assert beat.reason == PATCH_BEAT_REASON
    # turn comes from the snapshot's turn_manager.interaction (default 1).
    assert beat.turn == snapshot.turn_manager.interaction
    # No PCs seated → no party consensus → location is None (not a stale global).
    assert beat.location is None


def test_apply_world_patch_records_effective_clamped_delta():
    # Start near the +100 ceiling so a large patch delta clamps; the beat must
    # record the EFFECTIVE delta (after - before), not the raw patch delta.
    npc = _npc("Ally")
    npc.disposition = type(npc.disposition)(95)
    snapshot = _snapshot_with_npc(npc)
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Ally": 50}))

    assert int(npc.disposition) == 100
    assert len(npc.disposition_log) == 1
    beat = npc.disposition_log[-1]
    # raw delta was 50, but clamp limited the real move to +5.
    assert beat.delta == 5
    assert beat.reason == PATCH_BEAT_REASON


def test_apply_world_patch_clamped_noop_records_nothing():
    # Already at the ceiling; a positive patch is a no-op → no beat.
    npc = _npc("Maxed")
    npc.disposition = type(npc.disposition)(100)
    snapshot = _snapshot_with_npc(npc)
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Maxed": 20}))

    assert int(npc.disposition) == 100
    assert npc.disposition_log == []
