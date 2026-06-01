from sidequest.game.npc_development import ENGAGEMENT_BEAT_REASON, develop_npc_on_engagement
from tests.game.test_disposition_beat import _npc


def test_engagement_reason_constant():
    assert isinstance(ENGAGEMENT_BEAT_REASON, str) and ENGAGEMENT_BEAT_REASON


def test_engagement_then_beat_records_with_reason():
    # Mirror the narration_apply call site: tick, then record the beat.
    npc = _npc()
    before = int(npc.disposition)
    tick = develop_npc_on_engagement(npc)
    if tick.disposition_delta != 0:
        npc.record_disposition_beat(
            turn=7,
            delta=tick.disposition_delta,
            reason=ENGAGEMENT_BEAT_REASON,
            location="parlor",
        )
    assert int(npc.disposition) == before + 2  # DISPOSITION_DRIFT_PER_ENGAGEMENT
    assert npc.disposition_log[-1].reason == ENGAGEMENT_BEAT_REASON
    assert npc.disposition_log[-1].turn == 7
