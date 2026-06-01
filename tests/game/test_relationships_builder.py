from sidequest.game.disposition import DispositionBeat
from sidequest.game.projection.relationships import band_for, trend_for


def test_band_thresholds():
    assert band_for(80) == "Devoted"
    assert band_for(50) == "Devoted"
    assert band_for(49) == "Warm"
    assert band_for(10) == "Warm"
    assert band_for(9) == "Neutral"
    assert band_for(0) == "Neutral"
    assert band_for(-9) == "Neutral"
    assert band_for(-10) == "Cool"
    assert band_for(-49) == "Cool"
    assert band_for(-50) == "Hostile"
    assert band_for(-100) == "Hostile"


def test_trend_up_flat_down():
    up = [
        DispositionBeat(turn=1, delta=2, reason="a"),
        DispositionBeat(turn=2, delta=1, reason="b"),
    ]
    down = [DispositionBeat(turn=1, delta=-3, reason="a")]
    assert trend_for(up) == "up"
    assert trend_for(down) == "down"
    assert trend_for([]) == "flat"


def test_trend_windows_last_k():
    beats = [
        DispositionBeat(turn=1, delta=-10, reason="old"),  # outside window
        DispositionBeat(turn=2, delta=1, reason="x"),
        DispositionBeat(turn=3, delta=1, reason="y"),
        DispositionBeat(turn=4, delta=1, reason="z"),
    ]
    assert trend_for(beats, k=3) == "up"  # last 3 sum +3, ignores the -10
