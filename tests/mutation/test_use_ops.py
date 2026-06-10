from __future__ import annotations

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.awn import AwnRulesetModule
from sidequest.game.system_strain import SystemStrainPool
from sidequest.genre.models.rules import AwnConfig
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    SaveVs,
    StigmaTables,
)
from sidequest.mutation.state import CharacterMutationState, MutationState
from sidequest.mutation.use_ops import use_mutation

_SIX = {
    "STRENGTH": "STR", "DEXTERITY": "DEX", "CONSTITUTION": "CON",
    "INTELLIGENCE": "INT", "WISDOM": "WIS", "CHARISMA": "CHA",
}


def _cfg() -> AwnConfig:
    return AwnConfig(attribute_map=_SIX)


def _core(strain_max: int = 10, strain_current: int = 0) -> CreatureCore:
    core = CreatureCore(name="Rux", description="A mutant survivor", personality="Grim")
    core.system_strain = SystemStrainPool(current=strain_current, max=strain_max, permanent=0)
    return core


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(
            body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12,
        ),
        negatives=[NegativeMutationDef(id="negative/frail", name="Frail",
                                       roll_range=(1, 100), effect="frail")],
        positives=[
            PositiveMutationDef(id="exotic/acid_spit", name="Acid Spit", category="exotic",
                                effect="spit acid", strain_cost=2, usage="per_scene",
                                save=SaveVs(stat="evasion", effect="negates")),
            PositiveMutationDef(id="sense/dark_sight", name="Dark Sight", category="sense",
                                effect="see in dark", strain_cost=0, usage="at_will"),
        ],
    )


def _state_with(*positive_ids: str) -> MutationState:
    return MutationState(characters={
        "Rux": CharacterMutationState(mp_remaining=0, positive_ids=list(positive_ids)),
    })


def test_at_will_passive_use_applies() -> None:
    result = use_mutation(
        state=_state_with("sense/dark_sight"), catalog=_catalog(),
        module=AwnRulesetModule(), cfg=_cfg(), core=_core(),
        actor="Rux", mutation_id="sense/dark_sight",
    )
    assert result.applied
    assert result.strain is None  # zero-cost: strain seam not touched


def test_strain_cost_flows_through_pool() -> None:
    core = _core()
    state = _state_with("exotic/acid_spit")
    result = use_mutation(
        state=state, catalog=_catalog(), module=AwnRulesetModule(), cfg=_cfg(), core=core,
        actor="Rux", mutation_id="exotic/acid_spit",
        save_resolver=lambda stat, target: "fail",
    )
    assert result.applied
    assert core.system_strain.current == 2
    assert state.characters["Rux"].usage["exotic/acid_spit"].used == 1


def test_strain_over_max_refused() -> None:
    core = _core(strain_max=2, strain_current=1)
    result = use_mutation(
        state=_state_with("exotic/acid_spit"), catalog=_catalog(),
        module=AwnRulesetModule(), cfg=_cfg(), core=core,
        actor="Rux", mutation_id="exotic/acid_spit",
        save_resolver=lambda stat, target: "fail",
    )
    assert not result.applied
    assert "strain" in result.reason
    assert core.system_strain.current == 1  # unchanged


def test_usage_limit_refused() -> None:
    state = _state_with("exotic/acid_spit")
    core = _core()
    kwargs = dict(
        state=state, catalog=_catalog(), module=AwnRulesetModule(), cfg=_cfg(), core=core,
        actor="Rux", mutation_id="exotic/acid_spit",
        save_resolver=lambda stat, target: "fail",
    )
    assert use_mutation(**kwargs).applied
    second = use_mutation(**kwargs)
    assert not second.applied
    assert "limit_exhausted" in second.reason


def test_not_owned_refused() -> None:
    result = use_mutation(
        state=_state_with(), catalog=_catalog(),
        module=AwnRulesetModule(), cfg=_cfg(), core=_core(),
        actor="Rux", mutation_id="exotic/acid_spit",
    )
    assert not result.applied
    assert "not_owned" in result.reason


def test_save_resolver_required_when_save_stat_set() -> None:
    import pytest

    with pytest.raises(ValueError, match="save_resolver"):
        use_mutation(
            state=_state_with("exotic/acid_spit"), catalog=_catalog(),
            module=AwnRulesetModule(), cfg=_cfg(), core=_core(),
            actor="Rux", mutation_id="exotic/acid_spit",
        )


def test_save_success_recorded() -> None:
    result = use_mutation(
        state=_state_with("exotic/acid_spit"), catalog=_catalog(),
        module=AwnRulesetModule(), cfg=_cfg(), core=_core(),
        actor="Rux", mutation_id="exotic/acid_spit",
        target_id="raider", save_resolver=lambda stat, target: "success",
    )
    assert result.applied
    assert result.save_stat == "evasion"
    assert result.save_result == "success"
