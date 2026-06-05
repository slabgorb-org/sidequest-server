"""heavy_metal → WWN Story 2 — cross-file class-id consistency (AC7).

RED for story 87-2. classes.yaml is the single source of truth for the class
roster. Every other place that names a class — char_creation ``class_hint`` (genre
+ evropi), ``power_tiers.yaml`` keys, world ``typical_classes`` — must reference a
class that exists in classes.yaml (by id or display_name), and NO 5e class name may
survive anywhere in those files.

This protects the reference-page anchors (content CLAUDE.md): an anchor is derived
from the class name, so a dangling/renamed reference silently bad-anchors. A typo in
any of these files is caught here rather than in play.

Reads content YAML directly with yaml.safe_load (the files ARE the thing under test).
Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_5E_CLASS_NAMES = {
    "fighter", "ranger", "rogue", "cleric", "druid", "bard",
    "barbarian", "monk", "wizard", "warlock", "sorcerer", "paladin",
}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _pack_dir() -> Path:
    try:
        return Path(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _load_yaml(path: Path):
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _valid_class_labels(pack_dir: Path) -> set[str]:
    classes_path = pack_dir / "classes.yaml"
    assert classes_path.is_file(), f"classes.yaml must be authored (Story 2): {classes_path}"
    classes = _load_yaml(classes_path)
    assert isinstance(classes, list) and classes, "classes.yaml must be a non-empty list of classes"
    labels: set[str] = set()
    for c in classes:
        if c.get("id"):
            labels.add(str(c["id"]).lower())
        if c.get("display_name"):
            labels.add(str(c["display_name"]).lower())
    return labels


def _char_creation_class_hints(path: Path) -> list[str]:
    if not path.is_file():
        return []
    scenes = _load_yaml(path) or []
    hints: list[str] = []
    for scene in scenes:
        for choice in (scene.get("choices") or []):
            me = choice.get("mechanical_effects") or {}
            hint = me.get("class_hint")
            if hint:
                hints.append(str(hint))
    return hints


def _typical_classes(path: Path) -> list[str]:
    if not path.is_file():
        return []
    archetypes = _load_yaml(path) or []
    out: list[str] = []
    for arch in archetypes:
        if not isinstance(arch, dict):
            continue
        for tc in (arch.get("typical_classes") or []):
            out.append(str(tc))
    return out


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_every_class_reference_resolves_and_no_5e_survives() -> None:
    pack_dir = _pack_dir()
    valid = _valid_class_labels(pack_dir)

    # (label, source) for every class reference across the content files.
    refs: list[tuple[str, str]] = []

    for hint in _char_creation_class_hints(pack_dir / "char_creation.yaml"):
        refs.append((hint, "char_creation.yaml class_hint"))
    for hint in _char_creation_class_hints(pack_dir / "worlds" / "evropi" / "char_creation.yaml"):
        refs.append((hint, "evropi/char_creation.yaml class_hint"))

    power_tiers_path = pack_dir / "power_tiers.yaml"
    assert power_tiers_path.is_file(), "power_tiers.yaml must exist"
    power_tiers = _load_yaml(power_tiers_path) or {}
    assert isinstance(power_tiers, dict), "power_tiers.yaml must be a class-keyed mapping"
    for key in power_tiers:
        refs.append((str(key), "power_tiers.yaml key"))

    for world in ("evropi", "long_foundry"):
        for tc in _typical_classes(pack_dir / "worlds" / world / "archetypes.yaml"):
            refs.append((tc, f"{world}/archetypes.yaml typical_classes"))

    assert refs, "expected class references across content files — none found (test wiring?)"

    # No 5e class name may survive in any reference.
    surviving_5e = sorted({f"{label} ({src})" for label, src in refs if label.lower() in _5E_CLASS_NAMES})
    assert not surviving_5e, f"5e class names survive in content: {surviving_5e}"

    # Every reference must resolve to a real class in classes.yaml.
    dangling = sorted({f"{label} ({src})" for label, src in refs if label.lower() not in valid})
    assert not dangling, (
        f"class references not found in classes.yaml (anchor-breaking drift): {dangling}; "
        f"valid labels: {sorted(valid)}"
    )
