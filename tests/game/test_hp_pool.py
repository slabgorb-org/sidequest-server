from sidequest.game.creature_core import HpPool, hp_pool_from_hp


def test_hp_pool_clamps_to_zero_and_max():
    pool = HpPool(current=8, max=8, base_max=8)
    assert pool.apply_delta(-3) == 5
    assert pool.apply_delta(-100) == 0          # floored at 0
    assert pool.apply_delta(50) == 8            # capped at max
    assert pool.current == 8


def test_hp_pool_from_hp_seeds_full_floored_at_one():
    pool = hp_pool_from_hp(30)
    assert (pool.current, pool.max, pool.base_max) == (30, 30, 30)
    assert hp_pool_from_hp(0).max == 1
