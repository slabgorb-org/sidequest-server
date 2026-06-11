"""Story 103-2 review rework — _stockify_scene_message loud-failure contract.

Review findings [MEDIUM][SEC][SILENT] (2026-06-11): the stock-frame upgrade
helper had two silent early-returns that violate No Silent Fallbacks:

  1. choices carry stock_id but the world ships NO stock registry — the
     frame silently degraded to a plain choice scene and the
     misconfiguration surfaced only as a deferred confirm-time error.
     The FIRST wrong moment must be the loud one: raise naming the world
     and the offending stock_id(s).
  2. a loaded registry guarantees a mutation catalog (load-time invariant),
     so a granting stock rendered with ``catalog is None`` silently showed
     mechanics-first players an empty mutations list. Raise naming the
     stock instead of misrepresenting its trait set.

The happy path is pinned here too — no direct test existed for the frame
upgrade (it was covered only end-to-end through the UI wiring suite).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.game.builder import CharacterBuilder, ChoiceInput
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.stocks import StockDef, StockRegistry
from sidequest.server.websocket_handlers.chargen_mixin import _stockify_scene_message


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(
                id="negative/test_frail", name="Frail", roll_range=(1, 100), effect="frail"
            )
        ],
        positives=[
            PositiveMutationDef(
                id="hybrid/test_crushing_jaws",
                name="Crushing Jaws",
                category="hybrid",
                effect="bite",
            ),
        ],
    )


def _registry() -> StockRegistry:
    return StockRegistry(
        stocks=[
            StockDef(id="sleeper", name="Sleeper"),
            StockDef(
                id="harbor_seal",
                name="Harbor Seal Uplift",
                attr_mods={"STR": 1},
                move=12,
                ac=14,
                trauma_target_mod=1,
                granted_mutations=["hybrid/test_crushing_jaws"],
                saint_affinity_allowed=True,
            ),
        ]
    )


def _stock_scene_builder() -> CharacterBuilder:
    scenes = [
        CharCreationScene(
            id="stock",
            title="What You Are",
            narration="Choose.",
            choices=[
                CharCreationChoice(
                    label="Wild Mutant",
                    description="freehand",
                    mechanical_effects=MechanicalEffects(),
                ),
                CharCreationChoice(
                    label="Sleeper",
                    description="cold racks",
                    mechanical_effects=MechanicalEffects(stock_id="sleeper"),
                ),
                CharCreationChoice(
                    label="Harbor Seal Uplift",
                    description="whalecoast",
                    mechanical_effects=MechanicalEffects(stock_id="harbor_seal"),
                ),
            ],
        ),
    ]
    return CharacterBuilder(scenes=scenes, rules=RulesConfig())


def _sd(*, stocks: StockRegistry | None, mutations: MutationCatalog | None) -> SimpleNamespace:
    """Minimal _SessionData shape: genre_pack.worlds dict + world_slug."""
    world = SimpleNamespace(stocks=stocks)
    pack = SimpleNamespace(worlds={"seaboard_of_saints": world}, mutations=mutations)
    return SimpleNamespace(genre_pack=pack, world_slug="seaboard_of_saints")


def _frame(builder: CharacterBuilder):
    return builder.to_scene_message("p1")


class TestStockifyHappyPath:
    def test_frame_upgraded_with_aligned_options_and_display_names(self) -> None:
        builder = _stock_scene_builder()
        msg = _frame(builder)
        _stockify_scene_message(msg, builder, _sd(stocks=_registry(), mutations=_catalog()))
        assert msg.payload.input_type == "stock"
        options = msg.payload.stock_options
        assert options is not None and len(options) == 3
        # 1:1 alignment with choices — Wild rides along with empty deltas.
        assert options[0].label == "Wild Mutant"
        assert options[0].deltas.granted_mutations == []
        seal = options[2]
        assert seal.id == "harbor_seal"
        assert seal.deltas.attr_mods == {"STR": 1}
        assert seal.deltas.ac == 14
        # Display names, never catalog ids (mechanics must be LEGIBLE).
        assert seal.deltas.granted_mutations == ["Crushing Jaws"]

    def test_non_stock_scene_untouched(self) -> None:
        scenes = [
            CharCreationScene(
                id="origins",
                title="Origins",
                narration="n",
                choices=[
                    CharCreationChoice(
                        label="A", description="a", mechanical_effects=MechanicalEffects()
                    )
                ],
            )
        ]
        builder = CharacterBuilder(scenes=scenes, rules=RulesConfig())
        msg = _frame(builder)
        _stockify_scene_message(msg, builder, _sd(stocks=_registry(), mutations=_catalog()))
        assert msg.payload.input_type == "choice"
        assert msg.payload.stock_options is None


class TestStockifyLoudFailures:
    def test_stock_choices_with_no_registry_fail_loud(self) -> None:
        """Review finding 2: stock_id choices against a world with no
        stocks.yaml is a content misconfiguration — loud at the FIRST wrong
        moment (frame render), naming the world and the offending ids."""
        builder = _stock_scene_builder()
        msg = _frame(builder)
        with pytest.raises(ValueError) as exc_info:
            _stockify_scene_message(msg, builder, _sd(stocks=None, mutations=_catalog()))
        message = str(exc_info.value)
        assert "seaboard_of_saints" in message
        assert "sleeper" in message

    def test_granting_stock_with_no_catalog_fails_loud(self) -> None:
        """Review finding 3: a loaded registry guarantees a catalog; a
        granting stock with catalog=None must raise naming the stock, never
        render an empty mutations list (false math for Sebastien/Jade)."""
        builder = _stock_scene_builder()
        msg = _frame(builder)
        with pytest.raises(ValueError) as exc_info:
            _stockify_scene_message(msg, builder, _sd(stocks=_registry(), mutations=None))
        assert "harbor_seal" in str(exc_info.value)
