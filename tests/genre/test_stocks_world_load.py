"""Story 103-2 RED — world-tier stocks.yaml wiring through the production loader.

Stocks are world-tier content (ADR-140; build plan §D-B):
``worlds/<slug>/stocks.yaml`` -> ``World.stocks``. The genre-tier mutation
catalog is the validation authority for granted ids — load-time, loud
(same contract as 103-1's saints.yaml seam, pinned in
tests/genre/test_saints_world_load.py).

Loader contract:
  - absent stocks.yaml -> ``World.stocks is None`` (single-path world — the
    flickering_reach shape; AC1's "absence = current behavior")
  - present + valid    -> ``World.stocks`` is a StockRegistry
  - present + bad id   -> pack load FAILS naming stock id + mutation id
  - present + NO genre mutation catalog -> pack load FAILS naming the world

Real-content half (mirrors the saints suite): seaboard_of_saints ships
``draft: true`` until the 103-9 asset gate and load_genre_pack skips draft
worlds, so the proof content is validated directly against the real genre
catalog — a check that holds regardless of draft status. The story ships
PROOF stocks only: Sleeper + one Animal (enough to prove every branch
class of the schema), the stock chargen scene, and the 5 spec implants as
System-Strain item sources.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.character import CharCreationScene
from sidequest.genre.models.items import WorldItemsCatalog
from sidequest.genre.models.pack import World
from sidequest.mutation.catalog import load_mutation_catalog
from sidequest.mutation.stocks import StockRegistry, load_stock_registry

# ---------------------------------------------------------------------------
# Minimal genre-tier mutations.yaml for the cloned fixture pack
# (full d100 partition — the catalog validator requires gapless 1-100)
# ---------------------------------------------------------------------------

_MUTATIONS_YAML = """\
mp_economy:
  mutant_classes: [Mutant]
stigma:
  body_part: [arm, leg, torso, head, hand, foot]
  nature: [scaled, furred, chitinous, luminous, withered, swollen]
  flavor: [dry, damp, cold, hot, rough, smooth, pale, dark, bright, dull, soft, hard]
negatives:
  - id: negative/test_frail
    name: Frail
    roll_range: [1, 100]
    effect: frail
positives:
  - id: hybrid/test_crushing_jaws
    name: Crushing Jaws
    category: hybrid
    effect: bite
"""

_STOCKS_YAML = """\
stocks:
  - id: harbor_seal
    name: Harbor Seal Uplift
    attr_mods:
      STR: 1
    move: 12
    ac: 14
    granted_mutations:
      - hybrid/test_crushing_jaws
    saint_affinity_allowed: true
"""

_STOCKS_YAML_BAD_GRANT = _STOCKS_YAML.replace("hybrid/test_crushing_jaws", "hybrid/does_not_exist")

# The 5 spec implants (story scope, world design §13 via the addendum).
_SPEC_IMPLANTS = {
    "subdermal_weave",
    "cortex_booster",
    "optic_suite",
    "dermal_vox",
    "blood_filter",
}


def _arm_pack(pack: Any, *, mutations: bool, stocks_yaml: str | None) -> Path:
    """Write mutations.yaml (pack root) and/or stocks.yaml (world tier) into a
    MinimalPack clone; returns the pack path. Pattern: test_saints_world_load."""
    root = Path(pack.path)
    if mutations:
        (root / "mutations.yaml").write_text(_MUTATIONS_YAML, encoding="utf-8")
    if stocks_yaml is not None:
        world_dir = root / "worlds" / "flickering_reach"
        assert world_dir.is_dir(), f"fixture world missing: {world_dir}"
        (world_dir / "stocks.yaml").write_text(stocks_yaml, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Model seams
# ---------------------------------------------------------------------------


def test_world_model_has_stocks_field() -> None:
    """World grows a ``stocks`` field, default None, typed StockRegistry | None
    (pattern: World.saints, 103-1)."""
    assert "stocks" in World.model_fields
    field = World.model_fields["stocks"]
    assert field.default is None
    assert StockRegistry in getattr(field.annotation, "__args__", (field.annotation,))


def test_world_items_catalog_has_implants_section() -> None:
    """WorldItemsCatalog grows an ``implants`` section — the System-Strain
    item-source lane (story AC3), sibling to the five existing lanes."""
    assert "implants" in WorldItemsCatalog.model_fields
    catalog = WorldItemsCatalog()
    assert catalog.implants == []


# ---------------------------------------------------------------------------
# Production loader path (cloned fixture pack, non-draft world)
# ---------------------------------------------------------------------------


def test_loader_populates_world_stocks(minimal_pack_factory: Any, tmp_path: Path) -> None:
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=True, stocks_yaml=_STOCKS_YAML)
    loaded = load_genre_pack(root)
    world = loaded.worlds["flickering_reach"]
    assert world.stocks is not None, (
        "worlds/<slug>/stocks.yaml present + valid -> loader must populate World.stocks"
    )
    stock = world.stocks.by_id("harbor_seal")
    assert stock.granted_mutations == ["hybrid/test_crushing_jaws"]


def test_loader_absent_stocks_yaml_is_none(minimal_pack_factory: Any, tmp_path: Path) -> None:
    """No stocks.yaml -> None. Absence is an authored choice (single-path
    chargen, current behavior) — never an error, never a default registry."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=True, stocks_yaml=None)
    loaded = load_genre_pack(root)
    assert loaded.worlds["flickering_reach"].stocks is None


def test_loader_rejects_unresolvable_grant_loudly(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC5 at the PRODUCTION seam: the pack-load error names the stock and
    the missing mutation id — proof the loader runs the cross-validation."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=True, stocks_yaml=_STOCKS_YAML_BAD_GRANT)
    with pytest.raises(Exception) as exc_info:
        load_genre_pack(root)
    message = str(exc_info.value)
    assert "harbor_seal" in message
    assert "hybrid/does_not_exist" in message


def test_loader_rejects_stocks_without_mutation_catalog(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """stocks.yaml in a pack with no mutations.yaml is a configuration error:
    there is nothing to validate granted ids against. Loud, naming the world."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=False, stocks_yaml=_STOCKS_YAML)
    with pytest.raises(Exception) as exc_info:
        load_genre_pack(root)
    assert "flickering_reach" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Real content (sidequest-content) — holds regardless of draft status
# ---------------------------------------------------------------------------


def _seaboard(content_dir: Path) -> Path:
    return content_dir / "genre_packs" / "mutant_wasteland" / "worlds" / "seaboard_of_saints"


def test_real_seaboard_proof_stocks_resolve_against_real_catalog(content_dir: Path) -> None:
    """Proof stocks (story scope): Sleeper + one Animal — enough to prove
    every branch class of the schema. Sleeper is all-inert hooks + no Saints;
    the Animal carries a trait set AND saint_affinity_allowed (the AC4
    layering shape)."""
    pack_root = content_dir / "genre_packs" / "mutant_wasteland"
    catalog = load_mutation_catalog(pack_root / "mutations.yaml")
    stocks_path = _seaboard(content_dir) / "stocks.yaml"
    assert stocks_path.is_file(), "103-2 ships proof content: worlds/seaboard_of_saints/stocks.yaml"
    registry = load_stock_registry(stocks_path, catalog)
    ids = {s.id for s in registry.stocks}
    assert "sleeper" in ids, "the Sleeper proof stock is in scope"
    sleeper = registry.by_id("sleeper")
    assert sleeper.granted_mutations == [], "Sleeper mutates nothing — implants are its lane"
    assert sleeper.saint_affinity_allowed is False
    animals = [s for s in registry.stocks if s.id != "sleeper" and s.granted_mutations]
    assert animals, "one Animal proof stock with a granted trait set is in scope"
    assert any(s.saint_affinity_allowed for s in animals), (
        "the Animal proof stock must allow one Saint affinity bundle (AC4)"
    )


def test_real_seaboard_chargen_has_stock_step_with_branches(content_dir: Path) -> None:
    """The world's char_creation.yaml authors the stock step: a scene whose
    choices carry stock_id (every id resolving to stocks.yaml), and at least
    the Sleeper branch scene (requires_stock) offering implant item_hints."""
    world_dir = _seaboard(content_dir)
    cc_path = world_dir / "char_creation.yaml"
    assert cc_path.is_file(), "103-2 ships the seaboard chargen flow (world char_creation.yaml)"
    raw = yaml.safe_load(cc_path.read_text(encoding="utf-8"))
    scenes = [CharCreationScene.model_validate(s) for s in raw]

    catalog = load_mutation_catalog(
        content_dir / "genre_packs" / "mutant_wasteland" / "mutations.yaml"
    )
    registry = load_stock_registry(world_dir / "stocks.yaml", catalog)
    stock_ids = {s.id for s in registry.stocks}

    chosen_ids = {
        c.mechanical_effects.stock_id
        for s in scenes
        for c in s.choices
        if c.mechanical_effects.stock_id is not None
    }
    assert chosen_ids, "a stock-selection scene with stock_id choices must exist"
    assert chosen_ids <= stock_ids, (
        f"every chargen stock_id must resolve to stocks.yaml; orphans: {chosen_ids - stock_ids}"
    )

    branch_tags = {s.requires_stock for s in scenes if s.requires_stock is not None}
    assert branch_tags <= stock_ids, (
        f"every requires_stock tag must resolve to stocks.yaml; orphans: {branch_tags - stock_ids}"
    )
    sleeper_branches = [s for s in scenes if s.requires_stock == "sleeper"]
    assert sleeper_branches, "the Sleeper implant-choice branch scene is in scope"
    implant_hints = {
        c.mechanical_effects.item_hint
        for s in sleeper_branches
        for c in s.choices
        if c.mechanical_effects.item_hint is not None
    }
    assert implant_hints, "the Sleeper branch must offer implant choices via item_hint"
    assert implant_hints <= _SPEC_IMPLANTS, (
        f"Sleeper branch hints must be the spec implants; unknown: {implant_hints - _SPEC_IMPLANTS}"
    )


def test_real_seaboard_implants_are_strain_sources(content_dir: Path) -> None:
    """AC3 content half: the 5 spec implants ship as world items.yaml
    entries in the implants lane, each a System-Strain source (positive
    strain_cost)."""
    items_path = _seaboard(content_dir) / "items.yaml"
    assert items_path.is_file(), "103-2 ships the seaboard items.yaml with the implant lane"
    catalog = WorldItemsCatalog.model_validate(
        yaml.safe_load(items_path.read_text(encoding="utf-8"))
    )
    implants = {item.id: item for item in catalog.implants}
    missing = _SPEC_IMPLANTS - set(implants)
    assert not missing, f"spec implants missing from items.yaml implants lane: {sorted(missing)}"
    for item_id in _SPEC_IMPLANTS:
        dumped = implants[item_id].model_dump()
        cost = dumped.get("strain_cost")
        assert isinstance(cost, int) and cost >= 1, (
            f"implant {item_id!r} must carry a positive strain_cost (got {cost!r})"
        )


def test_real_flickering_reach_stays_stockless(content_dir: Path) -> None:
    """Sibling-world regression (AC1): flickering_reach loads through the
    full production pack load with stocks=None and an untouched chargen —
    no stocks.yaml exists there and none may appear."""
    pack_root = content_dir / "genre_packs" / "mutant_wasteland"
    assert not (pack_root / "worlds" / "flickering_reach" / "stocks.yaml").exists(), (
        "flickering_reach must remain stock-less (absence = single-path chargen)"
    )
    loaded = load_genre_pack(pack_root)
    assert loaded.worlds["flickering_reach"].stocks is None
    # The genre-tier chargen flow carries no stock machinery: no stock_id
    # choices, no requires_stock branches — current behavior preserved.
    for scene in loaded.char_creation:
        assert scene.requires_stock is None
        for choice in scene.choices:
            assert choice.mechanical_effects.stock_id is None
