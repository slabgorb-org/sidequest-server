from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import (
    DISPOSITION_LOG_CAP,
    DispositionBeat,
)
from sidequest.game.session import Npc


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
