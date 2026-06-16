"""Story 103-2 RED — Sleeper implants as System-Strain item sources (AC3).

Story context (addendum, locked): "Sleeper → no mutations, implants
authored as System-Strain item sources (same pool slot as AWN
cyberware/stims — reuse system_strain.py hooks, NO parallel implant
economy)."

Contract pinned here: ``use_implant(core, item, *, module, cfg, actor,
session_id) -> StrainResult`` in ``sidequest.mutation.stocks``:

  - the implant is a ``WorldItem`` (world-tier items.yaml entry, tolerant
    shape) carrying a ``strain_cost`` field;
  - use charges strain THROUGH the existing
    ``CwnRulesetModule.apply_system_strain`` pool — the strain delta, the
    over-max refusal, and the pool arithmetic are the live machinery, not
    a reimplementation;
  - an item with no ``strain_cost`` (or a non-positive one) is NOT a
    System-Strain source: using it as one is a configuration error,
    refused loudly naming the item (No Silent Fallbacks).
"""

from __future__ import annotations

import pytest

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.awn import AwnRulesetModule
from sidequest.game.system_strain import SystemStrainPool
from sidequest.genre.models.items import WorldItem
from sidequest.genre.models.rules import AwnConfig
from sidequest.mutation.stocks import use_implant

_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}
_CFG = AwnConfig(attribute_map=_AMAP)
_MOD = AwnRulesetModule()


def _core(current: int = 0, max: int = 12) -> CreatureCore:
    return CreatureCore(
        name="Winter Cho",
        description="a Sleeper out of the cold racks",
        personality="precise",
        system_strain=SystemStrainPool(current=current, max=max, permanent=0),
    )


def _implant(**overrides) -> WorldItem:
    base: dict = dict(id="cortex_booster", name="Cortex Booster", strain_cost=2)
    base.update(overrides)
    return WorldItem(**base)


def _use(core: CreatureCore, item: WorldItem):
    return use_implant(
        core,
        item,
        module=_MOD,
        cfg=_CFG,
        actor="Winter Cho",
        session_id="implant-strain-test",
    )


class TestImplantStrain:
    def test_use_charges_strain_through_existing_pool(self) -> None:
        """AC3: the strain delta assertion. Use costs exactly the item's
        strain_cost, applied to the SAME SystemStrainPool cyberware/stims
        use — current rises, the result reports the delta."""
        core = _core(current=0, max=12)
        result = _use(core, _implant())
        assert result.applied is True
        assert result.delta == 2
        assert core.system_strain is not None
        assert core.system_strain.current == 2

    def test_over_max_refused_by_existing_machinery(self) -> None:
        """No parallel implant economy: the over-max refusal is the live
        CwnRulesetModule rule. If this passes with current mutated, someone
        rebuilt the pool instead of reusing it."""
        core = _core(current=11, max=12)
        result = _use(core, _implant())
        assert result.applied is False
        assert result.delta == 0
        assert core.system_strain is not None
        assert core.system_strain.current == 11

    def test_item_without_strain_cost_refused_naming_item(self) -> None:
        """A bare item used as an implant is a configuration error — the
        loud refusal names the item so the author can fix items.yaml."""
        core = _core()
        bare = WorldItem(id="rusty_sextant", name="Rusty Sextant")
        with pytest.raises(ValueError) as exc_info:
            _use(core, bare)
        assert "rusty_sextant" in str(exc_info.value)

    def test_non_positive_strain_cost_refused(self) -> None:
        core = _core()
        with pytest.raises(ValueError) as exc_info:
            _use(core, _implant(strain_cost=0))
        assert "cortex_booster" in str(exc_info.value)

    def test_no_strain_pool_fails_loud(self) -> None:
        """A Sleeper without a seeded pool is a chargen bug upstream — the
        existing apply_system_strain contract already fails loud; using an
        implant must surface that, never swallow it."""
        core = CreatureCore(
            name="Winter Cho",
            description="a Sleeper out of the cold racks",
            personality="precise",
            system_strain=None,
        )
        with pytest.raises(ValueError):
            _use(core, _implant())
