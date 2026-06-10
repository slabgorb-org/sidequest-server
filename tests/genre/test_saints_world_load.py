"""Story 103-1 RED — world-tier saints.yaml wiring through the production loader.

The Saint canon is world-tier content (ADR-140: the world owns the cast and
catalog): ``worlds/<slug>/saints.yaml`` -> ``World.saints``. The genre-tier
mutation catalog is the validation authority — every Saint bundle/drawback/
affinity id must resolve against ``GenrePack.mutations`` AT LOAD TIME, loudly
(story AC2/AC6; No Silent Fallbacks).

Loader contract pinned here:
  - absent saints.yaml  -> World.saints is None (a world without Saints is a
    valid authored choice — flickering_reach stays Saint-less forever)
  - present + valid     -> World.saints is a SaintRegistry
  - present + bad id    -> pack load FAILS naming saint id + mutation id
  - present + NO genre mutation catalog -> pack load FAILS (a Saint file with
    nothing to validate against is a configuration error, not a no-op)

Draft note: seaboard_of_saints ships ``draft: true`` until the 103-9 asset
gate, and the loader skips draft worlds entirely — so the production-path
wiring tests run on the cloned fixture pack's non-draft world, and the real
seaboard proof content is validated directly against the real genre catalog
(a check that holds regardless of draft status).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import World
from sidequest.mutation.catalog import load_mutation_catalog
from sidequest.mutation.saints import SaintRegistry, load_saint_registry

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
  - id: negative/test_obsessive
    name: Obsessive Monologue
    roll_range: [1, 50]
    effect: cannot stop telling a story once begun
  - id: negative/test_frail
    name: Frail
    roll_range: [51, 100]
    effect: frail
positives:
  - id: structure/test_bone_density
    name: Whale-Bone Density
    category: structure
    effect: dense bones
  - id: sense/test_deep_sight
    name: Deep-Pressure Sight
    category: sense
    effect: see in the deep
"""

_SAINTS_YAML = """\
saints:
  - id: herman_of_the_acushnet
    name: Saint Herman of the Acushnet
    tradition: literary
    patron_regions: [whalecoast]
    bundle:
      - structure/test_bone_density
      - sense/test_deep_sight
    drawback: negative/test_obsessive
"""

_SAINTS_YAML_BAD_BUNDLE = _SAINTS_YAML.replace(
    "structure/test_bone_density", "structure/does_not_exist"
)


def _arm_pack(pack: Any, *, mutations: bool, saints_yaml: str | None) -> Path:
    """Write mutations.yaml (pack root) and/or saints.yaml (world tier) into a
    MinimalPack clone; returns the pack path."""
    root = Path(pack.path)
    if mutations:
        (root / "mutations.yaml").write_text(_MUTATIONS_YAML, encoding="utf-8")
    if saints_yaml is not None:
        world_dir = root / "worlds" / "flickering_reach"
        assert world_dir.is_dir(), f"fixture world missing: {world_dir}"
        (world_dir / "saints.yaml").write_text(saints_yaml, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Model seam
# ---------------------------------------------------------------------------


def test_world_model_has_saints_field() -> None:
    """World grows a ``saints`` field, default None, typed SaintRegistry | None
    (pattern: tests/mutation/test_pack_loading.py for GenrePack.mutations)."""
    assert "saints" in World.model_fields
    field = World.model_fields["saints"]
    assert field.default is None
    assert SaintRegistry in getattr(field.annotation, "__args__", (field.annotation,))


# ---------------------------------------------------------------------------
# Production loader path (cloned fixture pack, non-draft world)
# ---------------------------------------------------------------------------


def test_loader_populates_world_saints(minimal_pack_factory: Any, tmp_path: Path) -> None:
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=True, saints_yaml=_SAINTS_YAML)
    loaded = load_genre_pack(root)
    world = loaded.worlds["flickering_reach"]
    assert world.saints is not None, (
        "worlds/<slug>/saints.yaml present + valid -> loader must populate World.saints"
    )
    saint = world.saints.by_id("herman_of_the_acushnet")
    assert saint.drawback == "negative/test_obsessive"


def test_loader_absent_saints_yaml_is_none(minimal_pack_factory: Any, tmp_path: Path) -> None:
    """No saints.yaml -> None. Absence is an authored choice, never an error."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=True, saints_yaml=None)
    loaded = load_genre_pack(root)
    assert loaded.worlds["flickering_reach"].saints is None


def test_loader_rejects_unresolvable_saint_id_loudly(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """AC2 at the PRODUCTION seam: the pack-load error names the saint and the
    missing mutation id — proof the loader actually runs the cross-validation,
    not merely that load_saint_registry can (that unit proof lives in
    tests/mutation/test_saints_loader.py)."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=True, saints_yaml=_SAINTS_YAML_BAD_BUNDLE)
    with pytest.raises(Exception) as exc_info:
        load_genre_pack(root)
    message = str(exc_info.value)
    assert "herman_of_the_acushnet" in message
    assert "structure/does_not_exist" in message


def test_loader_rejects_saints_without_mutation_catalog(
    minimal_pack_factory: Any, tmp_path: Path
) -> None:
    """saints.yaml in a pack with no mutations.yaml is a configuration error:
    there is nothing to validate the bundle against. Loud, naming the world —
    never load-and-hope (No Silent Fallbacks)."""
    pack = minimal_pack_factory(tmp_path)
    root = _arm_pack(pack, mutations=False, saints_yaml=_SAINTS_YAML)
    with pytest.raises(Exception) as exc_info:
        load_genre_pack(root)
    assert "flickering_reach" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Real content (sidequest-content) — holds regardless of draft status
# ---------------------------------------------------------------------------


def test_real_seaboard_proof_saints_resolve_against_real_catalog(
    content_dir: Path,
) -> None:
    """The 3 proof Saints (story scope: bundle-only / bundle+affinity /
    drawback-bearing) must exist in the REAL world dir and every id must
    resolve against the REAL genre catalog. Runs the same loader+validator the
    production path uses; immune to the draft flag (which hides the world from
    load_genre_pack until the 103-9 asset gate)."""
    pack_root = content_dir / "genre_packs" / "mutant_wasteland"
    catalog = load_mutation_catalog(pack_root / "mutations.yaml")
    saints_path = pack_root / "worlds" / "seaboard_of_saints" / "saints.yaml"
    assert saints_path.is_file(), (
        "103-1 ships proof content: worlds/seaboard_of_saints/saints.yaml "
        "(content repo branch feat/103-1-saint-layer)"
    )
    registry = load_saint_registry(saints_path, catalog)
    assert len(registry.saints) >= 3, "story scope: at least 3 proof Saints"
    shapes = {
        "bundle_only": False,
        "bundle_with_affinity": False,
    }
    for saint in registry.saints:
        assert saint.bundle, f"{saint.id}: empty bundle"
        assert saint.drawback.startswith("negative/")
        if saint.affinity:
            shapes["bundle_with_affinity"] = True
        else:
            shapes["bundle_only"] = True
    assert all(shapes.values()), (
        f"proof saints must cover both pipeline shapes (bundle-only AND "
        f"bundle+affinity); got {shapes}"
    )


def test_real_flickering_reach_stays_saintless(content_dir: Path) -> None:
    """Sibling-world regression (story AC6): flickering_reach loads through the
    full production pack load with saints=None — no saints.yaml exists there
    and none may appear."""
    pack_root = content_dir / "genre_packs" / "mutant_wasteland"
    assert not (pack_root / "worlds" / "flickering_reach" / "saints.yaml").exists(), (
        "flickering_reach must remain Saint-less (addendum: no New Catholicism "
        "reached it; its mutants are raw AWN MP spend)"
    )
    loaded = load_genre_pack(pack_root)
    assert loaded.worlds["flickering_reach"].saints is None
