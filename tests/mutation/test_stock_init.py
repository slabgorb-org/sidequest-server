"""Story 103-2 RED — the stock route through init_mutation_state_for_session.

The production join point: chargen confirm (chargen_mixin) already calls
``init_mutation_state_for_session`` with the world's Saint canon plumbed
(103-1, with the comment "saint_id stays None until the stock chargen step
(103-2) gives the player a selection surface"). This story delivers that
surface, so the init seam grows the stock parameters:

    init_mutation_state_for_session(
        snapshot, catalog=..., character_name=..., character_class=...,
        session_id=..., saints=..., saint_id=...,
        stocks=..., stock_id=..., character=...,
    )

Contract (mirrors the 103-1 saint route's loud-config discipline):
  - ``stock_id`` with no ``stocks`` registry -> ValueError (a stock pick
    against a world that ships no stocks.yaml is a configuration error)
  - ``stock_id`` with no mutation catalog -> ValueError
  - ``stock_id`` with no ``character`` -> ValueError (traits need a sheet)
  - the stock route applies the stock GENERICALLY (apply_stock) — character
    traits delta'd, mutation state seeded under the actor — and the
    optional saint_id layers through the same call (AC4).
"""

from __future__ import annotations

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.saints import SaintDef, SaintRegistry
from sidequest.mutation.stocks import StockDef, StockRegistry
from sidequest.server.mutation_init import init_mutation_state_for_session


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(
                id="negative/test_obsessive",
                name="Obsessive",
                roll_range=(1, 100),
                effect="obsessive",
            )
        ],
        positives=[
            PositiveMutationDef(
                id="hybrid/test_crushing_jaws",
                name="Crushing Jaws",
                category="hybrid",
                effect="bite",
            ),
            PositiveMutationDef(
                id="sense/test_deep_sight",
                name="Deep-Pressure Sight",
                category="sense",
                effect="see in the deep",
            ),
        ],
    )


def _stocks() -> StockRegistry:
    return StockRegistry(
        stocks=[
            StockDef(
                id="harbor_seal",
                name="Harbor Seal Uplift",
                attr_mods={"STR": 1},
                granted_mutations=["hybrid/test_crushing_jaws"],
                saint_affinity_allowed=True,
            ),
        ]
    )


def _saints() -> SaintRegistry:
    return SaintRegistry(
        saints=[
            SaintDef(
                id="herman_of_the_acushnet",
                name="Saint Herman of the Acushnet",
                tradition="literary",
                bundle=["sense/test_deep_sight"],
                drawback="negative/test_obsessive",
            )
        ]
    )


def _character() -> Character:
    return Character(
        core=CreatureCore(name="Pup", description="seal-kin", personality="loyal"),
        backstory="raised by the tide",
        char_class="Mutant",
        race="uplift",
        stats={"STR": 10},
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot.model_construct(mutation_state=None)


def _init(snapshot: GameSnapshot, **overrides) -> None:
    kwargs: dict = dict(
        catalog=_catalog(),
        character_name="Pup",
        character_class="Mutant",
        session_id="stock-init-test",
        stocks=_stocks(),
        stock_id="harbor_seal",
        character=_character(),
    )
    kwargs.update(overrides)
    init_mutation_state_for_session(snapshot, **kwargs)


def test_stock_route_applies_stock_to_character_and_state() -> None:
    """The wiring proof at the production join point: the stock route must
    reach apply_stock — traits on the sheet, grants in the mutation state."""
    snapshot = _snapshot()
    character = _character()
    _init(snapshot, character=character)
    assert character.stats["STR"] == 11
    assert snapshot.mutation_state is not None
    cs = snapshot.mutation_state.characters["Pup"]
    assert cs.positive_ids == ["hybrid/test_crushing_jaws"]


def test_stock_route_layers_saint_when_given() -> None:
    """AC4 through the init seam: stock + saint in one confirm."""
    snapshot = _snapshot()
    _init(snapshot, saints=_saints(), saint_id="herman_of_the_acushnet")
    assert snapshot.mutation_state is not None
    cs = snapshot.mutation_state.characters["Pup"]
    assert set(cs.positive_ids) == {"hybrid/test_crushing_jaws", "sense/test_deep_sight"}
    assert cs.negative_ids == ["negative/test_obsessive"]


def test_stock_id_without_registry_fails_loud() -> None:
    with pytest.raises(ValueError) as exc_info:
        _init(_snapshot(), stocks=None)
    assert "harbor_seal" in str(exc_info.value)


def test_stock_id_without_catalog_fails_loud() -> None:
    with pytest.raises(ValueError):
        _init(_snapshot(), catalog=None)


def test_stock_id_without_character_fails_loud() -> None:
    """Traits need a sheet — a stock route with no Character object is a
    caller bug, never a partial apply (mutations granted but traits lost)."""
    with pytest.raises(ValueError):
        _init(_snapshot(), character=None)
