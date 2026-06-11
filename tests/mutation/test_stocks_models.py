"""Story 103-2 RED — StockDef / StockRegistry / load_stock_registry.

Build plan §D-B: stocks are a chargen branching layer built from AWN
primitives ONLY — ``worlds/<slug>/stocks.yaml`` defines trait sets as
``{attr_mods, move, ac, trauma_target_mod, granted_mutations,
saint_affinity_allowed}`` and the engine applies them through ONE generic
path. Zero per-stock special cases: nothing in this module may branch on a
stock's id.

Contract pinned here (mirrors saints.py, story 103-1 — same loud-failure
shape, same world-tier id discipline):

  - ``StockDef``: bare snake_case id (a slash signals a catalog-id mixup);
    ``granted_mutations`` hold POSITIVE catalog ids only (a stock granting
    a negative is authoring error — drawbacks belong to Saints);
    duplicates rejected naming the stock.
  - ``StockRegistry``: unique ids, ``by_id`` raises KeyError naming the
    miss and the known ids.
  - ``load_stock_registry(path, catalog)``: FileNotFoundError on a missing
    file (absence is the CALLER's decision, mirroring load_saint_registry);
    every granted id cross-validated against the genre catalog AT LOAD,
    ValueError naming stock id + offending mutation id (No Silent
    Fallbacks; same contract/helper discipline as 103-1 per story context
    "reuse the same validation helper — don't duplicate it").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.mutation.stocks import StockDef, StockRegistry, load_stock_registry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(
                id="negative/test_frail",
                name="Frail",
                roll_range=(1, 100),
                effect="frail",
            ),
        ],
        positives=[
            PositiveMutationDef(
                id="hybrid/test_crushing_jaws",
                name="Crushing Jaws",
                category="hybrid",
                effect="bite",
            ),
            PositiveMutationDef(
                id="structure/test_thick_hide",
                name="Thick Hide",
                category="structure",
                effect="natural armor",
            ),
        ],
    )


def _stock(**overrides) -> StockDef:
    base = dict(
        id="harbor_seal",
        name="Harbor Seal Uplift",
        description="An Animal stock of the Whalecoast.",
        attr_mods={"STR": 1, "WIS": 1},
        move=12,
        ac=14,
        trauma_target_mod=1,
        granted_mutations=["hybrid/test_crushing_jaws"],
        saint_affinity_allowed=True,
    )
    base.update(overrides)
    return StockDef(**base)


_STOCKS_YAML = """\
stocks:
  - id: sleeper
    name: Sleeper
    description: Woke from the long cold.
    granted_mutations: []
    saint_affinity_allowed: false
  - id: harbor_seal
    name: Harbor Seal Uplift
    attr_mods:
      STR: 1
    move: 12
    ac: 14
    trauma_target_mod: 1
    granted_mutations:
      - hybrid/test_crushing_jaws
    saint_affinity_allowed: true
"""


# ---------------------------------------------------------------------------
# StockDef — schema-level validation
# ---------------------------------------------------------------------------


class TestStockDef:
    def test_full_trait_set_constructs(self) -> None:
        s = _stock()
        assert s.id == "harbor_seal"
        assert s.attr_mods == {"STR": 1, "WIS": 1}
        assert s.move == 12
        assert s.ac == 14
        assert s.trauma_target_mod == 1
        assert s.granted_mutations == ["hybrid/test_crushing_jaws"]
        assert s.saint_affinity_allowed is True

    def test_trait_hooks_default_to_inert(self) -> None:
        """A minimal stock touches nothing: no attr mods, no overrides, no
        grants, no Saint affinity. Defaults ARE the no-op — that's what makes
        the application path generic (absence of a hook is data, not code)."""
        s = StockDef(id="sleeper", name="Sleeper")
        assert s.attr_mods == {}
        assert s.move is None
        assert s.ac is None
        assert s.trauma_target_mod == 0
        assert s.granted_mutations == []
        assert s.saint_affinity_allowed is False

    def test_id_must_be_bare_snake_case(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            _stock(id="stock/harbor_seal")
        assert "stock/harbor_seal" in str(exc_info.value)

    def test_granted_mutations_reject_negatives(self) -> None:
        """Drawbacks are the Saint layer's vocabulary. A stock that grants a
        negative is an authoring mixup, refused at the model — naming the
        offending id."""
        with pytest.raises(ValueError) as exc_info:
            _stock(granted_mutations=["negative/test_frail"])
        assert "negative/test_frail" in str(exc_info.value)

    def test_duplicate_granted_mutations_rejected_naming_stock(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            _stock(
                granted_mutations=[
                    "hybrid/test_crushing_jaws",
                    "hybrid/test_crushing_jaws",
                ]
            )
        message = str(exc_info.value)
        assert "harbor_seal" in message
        assert "hybrid/test_crushing_jaws" in message


# ---------------------------------------------------------------------------
# StockRegistry
# ---------------------------------------------------------------------------


class TestStockRegistry:
    def test_duplicate_stock_ids_rejected(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            StockRegistry(stocks=[_stock(), _stock()])
        assert "harbor_seal" in str(exc_info.value)

    def test_by_id_returns_stock(self) -> None:
        registry = StockRegistry(stocks=[_stock()])
        assert registry.by_id("harbor_seal").name == "Harbor Seal Uplift"

    def test_by_id_unknown_raises_keyerror_naming_known(self) -> None:
        registry = StockRegistry(stocks=[_stock()])
        with pytest.raises(KeyError) as exc_info:
            registry.by_id("synthetic")
        message = str(exc_info.value)
        assert "synthetic" in message
        assert "harbor_seal" in message


# ---------------------------------------------------------------------------
# load_stock_registry — file + catalog cross-validation
# ---------------------------------------------------------------------------


class TestLoadStockRegistry:
    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        """Absence is the caller's decision (the genre loader treats a missing
        stocks.yaml as 'world has no stocks' — mirrors load_saint_registry)."""
        with pytest.raises(FileNotFoundError):
            load_stock_registry(tmp_path / "stocks.yaml", _catalog())

    def test_valid_yaml_loads_both_stocks(self, tmp_path: Path) -> None:
        path = tmp_path / "stocks.yaml"
        path.write_text(_STOCKS_YAML, encoding="utf-8")
        registry = load_stock_registry(path, _catalog())
        assert {s.id for s in registry.stocks} == {"sleeper", "harbor_seal"}
        assert registry.by_id("sleeper").granted_mutations == []
        assert registry.by_id("harbor_seal").move == 12

    def test_unknown_granted_mutation_fails_loudly(self, tmp_path: Path) -> None:
        """AC5: bad granted_mutation ID fails load loudly, naming the stock
        AND the offending id — the same contract 103-1 pinned for Saints."""
        path = tmp_path / "stocks.yaml"
        path.write_text(
            _STOCKS_YAML.replace("hybrid/test_crushing_jaws", "hybrid/does_not_exist"),
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as exc_info:
            load_stock_registry(path, _catalog())
        message = str(exc_info.value)
        assert "harbor_seal" in message
        assert "hybrid/does_not_exist" in message
