"""Tests: loader picks up classes.yaml and populates GenrePack.classes.

Uses the clone-pack pattern (see test_loader_projection.py) because
load_genre_pack requires many mandatory YAML files.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.character import ClassDef
from tests._helpers.genre_paths import find_pack_path

_CAVERNS_PACK_DIR = find_pack_path("caverns_and_claudes")


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


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_classes_yaml_absent_yields_empty_list(tmp_path: Path) -> None:
    """A pack without classes.yaml loads with pack.classes == []."""
    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "caverns_no_classes")
    classes_file = pack_dir / "classes.yaml"
    if classes_file.exists():
        classes_file.unlink()
    pack = load_genre_pack(pack_dir)
    assert pack.classes == []


@pytest.mark.skipif(
    not _CAVERNS_PACK_DIR.is_dir(),
    reason="sidequest-content not on disk",
)
def test_classes_yaml_loads_entries(tmp_path: Path) -> None:
    """classes.yaml is parsed and all entries become ClassDef instances.

    WWN port (2026-06-12): the cloned caverns rules.yaml declares
    allowed_classes [Warrior, Expert, Mage] and a beat_selection combat pool
    (strike/cast_spell/brace/committed_blow/break_contact/...). The synthetic
    classes.yaml must therefore declare exactly those three Callings with
    encounter_beat_choices drawn from the real pool — _validate_class_filter_refs
    (Task 5) rejects undeclared allowed_classes and dangling beat IDs. cast_spell
    is offered only via the rules.yaml class_filter [Mage], never as a generic
    beat. saving_throws are still required by _validate_saving_throws_refs
    (Task 8) because the pack ships a wwn spell catalog.
    """
    pack_dir = _clone_pack(_CAVERNS_PACK_DIR, tmp_path / "caverns_with_classes")
    saves_block = (
        "  saving_throws:\n"
        "    death_ray_or_poison: 12\n"
        "    magic_wands: 13\n"
        "    paralysis_or_stone: 14\n"
        "    dragon_breath: 15\n"
        "    rods_staves_spells: 16\n"
    )
    (pack_dir / "classes.yaml").write_text(
        "- id: warrior\n"
        "  display_name: Warrior\n"
        "  rpg_role: tank\n"
        "  jungian_default: hero\n"
        "  prime_requisite: STR\n"
        "  minimum_score: 9\n"
        "  kit_table: warrior_kit\n"
        "  encounter_beat_choices: [strike, brace, break_contact]\n"
        + saves_block
        + "- id: expert\n"
        "  display_name: Expert\n"
        "  rpg_role: skirmisher\n"
        "  jungian_default: explorer\n"
        "  prime_requisite: DEX\n"
        "  minimum_score: 9\n"
        "  kit_table: expert_kit\n"
        "  encounter_beat_choices: [strike, brace, break_contact]\n" + saves_block + "- id: mage\n"
        "  display_name: Mage\n"
        "  rpg_role: control\n"
        "  jungian_default: magician\n"
        "  prime_requisite: INT\n"
        "  minimum_score: 9\n"
        "  kit_table: mage_kit\n"
        "  encounter_beat_choices: [strike, brace, break_contact]\n" + saves_block,
        encoding="utf-8",
    )
    pack = load_genre_pack(pack_dir)
    assert len(pack.classes) == 3
    assert all(isinstance(c, ClassDef) for c in pack.classes)
    assert {c.id for c in pack.classes} == {"warrior", "expert", "mage"}
