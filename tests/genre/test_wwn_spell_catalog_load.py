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


def _strip_class_filter_from_cast_spell(pack_dir: Path) -> None:
    """Remove class_filter from the cast_spell encounter beat in rules.yaml.

    When a cloned pack's rules.yaml carries a class_filter that references
    real class display_names (e.g. Channeler, Spirit Medium) but the test
    overrides classes.yaml with synthetic classes (e.g. Elementalist), the
    loader's _validate_class_filter_refs raises PackError before the test's
    intended assertion is reached.  Stripping class_filter makes the beat
    universal in the synthetic pack — acceptable because these tests exercise
    catalog/starting_prepared loading, not the cast gate itself.
    """
    rules_yaml = pack_dir / "rules.yaml"
    if not rules_yaml.exists():
        return
    with rules_yaml.open("r", encoding="utf-8") as f:
        rules_data = yaml.safe_load(f)
    # Beats are nested under confrontations[].beats[] in rules.yaml.
    for confrontation in rules_data.get("confrontations") or []:
        if not isinstance(confrontation, dict):
            continue
        for beat in confrontation.get("beats") or []:
            if isinstance(beat, dict) and beat.get("id") == "cast_spell":
                beat.pop("class_filter", None)
    with rules_yaml.open("w", encoding="utf-8") as f:
        yaml.dump(rules_data, f, default_flow_style=False, sort_keys=False)


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

_CASTER_CLASS_WITH_STARTING_PREPARED = """\
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
    starting_prepared:
      - cinder_lance
      - still_the_breath
"""

_CASTER_CLASS_WITH_UNKNOWN_STARTING_PREPARED = """\
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
    starting_prepared:
      - cinder_lance
      - no_such_spell_id
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
    # The cloned rules.yaml may have class_filter referencing real class names that
    # don't exist in our synthetic classes.yaml.  Strip it so the loader reaches the
    # intended assertion (no spells_wwn.yaml present with a caster class → fail loud).
    _strip_class_filter_from_cast_spell(pack_dir)

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


@pytest.mark.skipif(not _EH_AVAILABLE, reason="sidequest-content not on disk")
def test_wwn_caster_class_with_valid_starting_prepared_loads_ok(tmp_path: Path) -> None:
    """A caster class whose starting_prepared ids are all in the catalog loads fine."""
    pack_dir = _clone_pack(_EH_PACK_DIR, tmp_path / "eh_valid_starting_prepared")
    (pack_dir / "spells_wwn.yaml").write_text(_MINIMAL_SPELL_CATALOG, encoding="utf-8")
    (pack_dir / "classes.yaml").write_text(_CASTER_CLASS_WITH_STARTING_PREPARED, encoding="utf-8")
    # Reconcile class_filter in the cloned rules.yaml so the synthetic Elementalist
    # class is accepted by _validate_class_filter_refs before the catalog check.
    _strip_class_filter_from_cast_spell(pack_dir)

    pack = load_genre_pack(pack_dir)

    assert pack.wwn_spell_catalog is not None
    assert pack.classes is not None
    assert any(c.id == "elementalist" for c in pack.classes)


@pytest.mark.skipif(not _EH_AVAILABLE, reason="sidequest-content not on disk")
def test_wwn_caster_class_with_unknown_starting_prepared_id_fails_loud(tmp_path: Path) -> None:
    """A caster class whose starting_prepared references an unknown spell id raises GenreLoadError."""
    pack_dir = _clone_pack(_EH_PACK_DIR, tmp_path / "eh_bad_starting_prepared")
    (pack_dir / "spells_wwn.yaml").write_text(_MINIMAL_SPELL_CATALOG, encoding="utf-8")
    (pack_dir / "classes.yaml").write_text(
        _CASTER_CLASS_WITH_UNKNOWN_STARTING_PREPARED, encoding="utf-8"
    )
    # Reconcile class_filter in the cloned rules.yaml so the loader reaches the
    # intended assertion (unknown starting_prepared id → fail loud), not an earlier
    # PackError about class_filter referencing Channeler/Spirit Medium.
    _strip_class_filter_from_cast_spell(pack_dir)

    with pytest.raises(GenreLoadError, match="no_such_spell_id"):
        load_genre_pack(pack_dir)
