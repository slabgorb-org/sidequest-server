from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sidequest.mutation.catalog import load_mutation_catalog

VALID_YAML = """
mp_economy:
  mutant_classes: [Mutant]
stigma:
  body_part: [eyes, skin, hands, spine, jaw, hair]
  nature: [luminous, scaled, withered, oversized, translucent, ridged]
  flavor: [amber, silver, weeping, cracked, humming, cold, hot, twitching, numb, bright, dark, shifting]
negatives:
  - id: negative/withered_arm
    name: Withered Arm
    roll_range: [1, 100]
    effect: one arm is weak
    attr_penalties: {STR: -1}
positives:
  - id: structure/crushing_jaws
    name: Crushing Jaws
    category: structure
    effect: bite as a Punch attack
    attack: {skill: Punch, damage: 1d8, shock: "2/AC15"}
"""


def test_loads_valid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "mutations.yaml"
    p.write_text(VALID_YAML, encoding="utf-8")
    cat = load_mutation_catalog(p)
    assert cat.positive_by_id("structure/crushing_jaws").attack.skill == "Punch"
    assert cat.mp_economy.mutant_classes == ["Mutant"]


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="mutations.yaml"):
        load_mutation_catalog(tmp_path / "mutations.yaml")


def test_invalid_yaml_fails_loud(tmp_path: Path) -> None:
    p = tmp_path / "mutations.yaml"
    p.write_text("mp_economy: {mutant_classes: [Mutant]}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_mutation_catalog(p)
