"""Loader wires spells_wwn.yaml onto GenrePack.wwn_spell_catalog.

Three behavioral cases:
1. wwn pack WITH a caster class but NO spells_wwn.yaml → fail loud (GenreLoadError).
2. wwn pack WITH spells_wwn.yaml → pack.wwn_spell_catalog populated.
3. Non-wwn pack WITH spells_wwn.yaml present → catalog NOT loaded (field stays None).

Uses the clone-pack pattern (see test_classes_yaml_loader.py): clone elemental_harmony
(already a wwn pack) and mutate the copy for each scenario.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import find_pack_path

# We clone elemental_harmony (ruleset: wwn) for cases 1+2;
# clone caverns_and_claudes (non-wwn) for case 3.
try:
    _EH_PACK_DIR = find_pack_path("elemental_harmony")
    _EH_AVAILABLE = _EH_PACK_DIR.is_dir()
except Exception:
    _EH_PACK_DIR = Path("/nonexistent")
    _EH_AVAILABLE = False

try:
    _CC_PACK_DIR = find_pack_path("caverns_and_claudes")
    _CC_AVAILABLE = _CC_PACK_DIR.is_dir()
except Exception:
    _CC_PACK_DIR = Path("/nonexistent")
    _CC_AVAILABLE = False


def _clone_pack(src: Path, dst: Path) -> Path:
    """Deep-copy a pack so the test can mutate the copy safely.

    Also updates lethality_policy.yaml genre_key to match the new directory
    name, since the loader validates genre_key matches the pack directory name.
    """
    shutil.copytree(src, dst)
    lethality_yaml = dst / "lethality_policy.yaml"
    if lethality_yaml.exists():
        with lethality_yaml.open("r", encoding="utf-8") as f:
            policy_data = yaml.safe_load(f)
        policy_data["genre_key"] = dst.name
        with lethality_yaml.open("w", encoding="utf-8") as f:
            yaml.dump(policy_data, f, default_flow_style=False, sort_keys=False)
    return dst


_MINIMAL_CASTER_CLASS = """\
- id: elementalist
  display_name: Elementalist
  rpg_role: control
  jungian_default: magician
  prime_requisite: INT
  minimum_score: 9
  kit_table: elementalist_kit
  magic_access: wwn
  wwn_magic:
    effort_sources:
      - source: elementalist
        governing_attr: INTELLIGENCE
        relevant_skill: Magic
        starting_skill_level: 1
    casts_per_day_by_level:
      "1": 1
      "2": 2
    max_spell_level_by_level:
      "1": 1
      "2": 1
    prepared_by_level:
      "1": 2
      "2": 3
"""

_MINIMAL_SPELL_CATALOG = """\
version: "1.0"
spells:
  - id: cinder_lance
    name: Cinder Lance
    level: 1
    save: evasion
    damage_die: "1d6"
    damage_per_level: true
    genre_description: "A lance of condensed flame hurled at a foe."
    mechanical_effect: "Ranged attack; evasion save halves damage (caster_level × 1d6)."
  - id: still_the_breath
    name: Still the Breath
    level: 1
    save: mental
    genre_description: "The caster commands a creature's breath to still."
    mechanical_effect: "Mental save or the target cannot take actions this round."
  - id: river_step
    name: River Step
    level: 1
    genre_description: "The caster flows like water, stepping through narrow gaps."
    mechanical_effect: "Caster may pass through any opening wide enough for water."
"""


@pytest.mark.skipif(not _EH_AVAILABLE, reason="sidequest-content not on disk")
def test_wwn_pack_with_caster_class_and_no_spells_file_fails_loud(tmp_path: Path) -> None:
    """A wwn pack that declares a caster class but has no spells_wwn.yaml must raise."""
    pack_dir = _clone_pack(_EH_PACK_DIR, tmp_path / "eh_no_spells")
    # Remove spells_wwn.yaml if it somehow already exists in the clone.
    spells_file = pack_dir / "spells_wwn.yaml"
    spells_file.unlink(missing_ok=True)
    # Write a classes.yaml with one wwn caster class.
    (pack_dir / "classes.yaml").write_text(_MINIMAL_CASTER_CLASS, encoding="utf-8")

    with pytest.raises(GenreLoadError):
        load_genre_pack(pack_dir)


@pytest.mark.skipif(not _EH_AVAILABLE, reason="sidequest-content not on disk")
def test_wwn_pack_with_spells_file_populates_catalog(tmp_path: Path) -> None:
    """A wwn pack WITH spells_wwn.yaml loads with a non-None wwn_spell_catalog."""
    pack_dir = _clone_pack(_EH_PACK_DIR, tmp_path / "eh_with_spells")
    # Remove classes.yaml so the caster-class branch doesn't trigger.
    (pack_dir / "classes.yaml").unlink(missing_ok=True)
    # Author a minimal spells_wwn.yaml.
    (pack_dir / "spells_wwn.yaml").write_text(_MINIMAL_SPELL_CATALOG, encoding="utf-8")

    pack = load_genre_pack(pack_dir)

    assert pack.wwn_spell_catalog is not None
    ids = {s.id for s in pack.wwn_spell_catalog.spells}
    assert "cinder_lance" in ids
    assert "still_the_breath" in ids
    assert "river_step" in ids


@pytest.mark.skipif(not _CC_AVAILABLE, reason="sidequest-content not on disk")
def test_non_wwn_pack_with_spells_file_does_not_load_catalog(tmp_path: Path) -> None:
    """A non-wwn pack ignores spells_wwn.yaml — field stays None."""
    pack_dir = _clone_pack(_CC_PACK_DIR, tmp_path / "cc_with_wwn_spells")
    # Drop a spells_wwn.yaml in a non-wwn pack — it must be silently ignored.
    (pack_dir / "spells_wwn.yaml").write_text(_MINIMAL_SPELL_CATALOG, encoding="utf-8")

    pack = load_genre_pack(pack_dir)

    assert pack.wwn_spell_catalog is None
