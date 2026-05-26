from sidequest.game.creature_core import CreatureCore, HpPool


def _core(*, name="Mara", ac=10, **kw):
    return CreatureCore(
        name=name, description="d", personality="p",
        hp=HpPool(current=8, max=8, base_max=8), armor_class=ac, **kw,
    )


def test_creature_core_has_armor_class_default_10():
    core = CreatureCore(name="x", description="d", personality="p")
    assert core.armor_class == 10


def test_creature_core_armor_class_settable():
    assert _core(ac=15).armor_class == 15
