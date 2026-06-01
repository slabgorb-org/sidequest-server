from sidequest.game.disposition import (
    DISPOSITION_LOG_CAP,
    DispositionBeat,
)


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
