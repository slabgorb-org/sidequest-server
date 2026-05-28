from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.system_strain import SystemStrainPool


def _core(**kw) -> CreatureCore:
    return CreatureCore(name="Jax", description="runner", personality="cool", **kw)


def test_creature_core_strain_defaults_none():
    core = _core()
    assert core.system_strain is None


def test_creature_core_accepts_strain_pool():
    core = _core(system_strain=SystemStrainPool(current=0, max=14, permanent=0))
    assert core.system_strain is not None
    assert core.system_strain.max == 14
    assert core.system_strain.current == 0
