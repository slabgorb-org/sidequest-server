"""Tests for WWN magic data models (pure-data layer, no ruleset logic)."""


def test_effort_pool_available_math():
    from sidequest.game.wwn_magic import EffortCommitment, EffortPool

    p = EffortPool(
        source="vowed", max=3, commitments=[EffortCommitment(points=1, duration="scene")]
    )
    assert p.committed == 1 and p.available == 2
