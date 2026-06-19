"""Story 126-25 RED — world-tier ``worlds/<world>/gear.yaml`` wiring through the
production loader.

Today ``load_genre_pack`` reads gear from the GENRE tier only
(``loader._load_gear(path / 'gear.yaml')`` → ``GenrePack.gear`` /
``rules.fate.gear_catalog``, loader.py:2130/2562). The world tier is never read,
so a world-distinct GearDef authored in ``worlds/<world>/gear.yaml`` (the Oz silver
shoes shipped inert in 126-21) can never reach the catalog the #945 promoter sees.

This pins the LOAD half of AC1: the loader must populate a new ``World.gear`` field
from ``worlds/<world>/gear.yaml``, mirroring the unconditional genre-tier load
(``GenrePack.gear`` is set for every pack regardless of ruleset; only the merge
INTO the effective Fate catalog is fate-gated at resolution time). Absence is the
current behavior — an authored choice, never an error, never a default catalog.

Pattern: ``tests/genre/test_stocks_world_load.py`` (world-tier ``stocks.yaml`` →
``World.stocks``) — the canonical world-tier optional-file loader-wiring shape. We
use the dial-bound ``test_genre`` clone deliberately: world gear must load
regardless of ruleset, exactly as the genre-tier gear load does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.inventory import GearDef
from sidequest.genre.models.pack import World

_GEAR_YAML = """\
- id: oz_silver_shoes
  name: The Silver Shoes of the Dead Witch
  grants_aspects:
    - text: Three Steps Home
      kind: permission
- id: oz_golden_cap
  name: The Golden Cap
  grants_aspects:
    - text: Three Commands of the Winged Monkeys
      kind: permission
"""


def _arm_world_gear(pack: Any, gear_yaml: str) -> Path:
    """Write a world-tier gear.yaml into the MinimalPack clone's flickering_reach
    world (pattern: test_stocks_world_load._arm_pack)."""
    root = Path(pack.path)
    world_dir = root / "worlds" / "flickering_reach"
    assert world_dir.is_dir(), f"fixture world missing: {world_dir}"
    (world_dir / "gear.yaml").write_text(gear_yaml, encoding="utf-8")
    return root


# ── Model seam ─────────────────────────────────────────────────────────────────


def test_world_model_has_gear_field() -> None:
    """World grows a ``gear`` field — a world-tier CAST/CATALOG list, sibling to
    ``World.classes`` / ``World.seed_tropes``, mirroring the genre-tier
    ``GenrePack.gear: list[GearDef]``."""
    assert "gear" in World.model_fields, "World must grow a world-tier `gear` field"
    field = World.model_fields["gear"]
    args = getattr(field.annotation, "__args__", (field.annotation,))
    assert GearDef in args, "World.gear must be a list[GearDef]"


# ── Production loader path (dial-bound test_genre clone) ─────────────────────────


def test_loader_populates_world_gear(minimal_pack_factory: Any, tmp_path: Path) -> None:
    """worlds/<slug>/gear.yaml present → the loader populates ``World.gear`` with
    its GearDefs, regardless of the pack's ruleset (the genre-tier load is
    unconditional too)."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_world_gear(pack, _GEAR_YAML)
    loaded = load_genre_pack(root)
    world = loaded.worlds["flickering_reach"]
    assert [g.id for g in world.gear] == ["oz_silver_shoes", "oz_golden_cap"], (
        "worlds/<slug>/gear.yaml present → loader must populate World.gear"
    )
    shoes = next(g for g in world.gear if g.id == "oz_silver_shoes")
    assert shoes.name == "The Silver Shoes of the Dead Witch"
    assert shoes.grants_aspects[0].text == "Three Steps Home"
    assert shoes.grants_aspects[0].kind == "permission"


def test_loader_absent_world_gear_is_empty(minimal_pack_factory: Any, tmp_path: Path) -> None:
    """No worlds/<slug>/gear.yaml → ``World.gear == []``. Absence is an authored
    choice (the common case — most worlds ship no world-distinct gear), never an
    error and never a default catalog (No Silent Fallbacks)."""
    pack = minimal_pack_factory(tmp_path)
    loaded = load_genre_pack(pack.path)
    assert loaded.worlds["flickering_reach"].gear == []
