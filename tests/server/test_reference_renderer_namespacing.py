"""Anchor ids on list-of-dict items must be namespaced by their containing
file's kind so cross-file name collisions produce distinct anchors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sidequest.server.reference_renderer import (
    _kind_for_stem,
    _render_file,
)


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("classes", "class"),
        ("archetypes", "archetype"),
        ("cultures", "culture"),
        ("legends", "legend"),
        ("locations", "location"),
        ("history", "history"),
        ("lore", "lore"),
        ("world", "world"),
        ("achievements", "achievement"),
        ("tropes", "trope"),
    ],
)
def test_kind_for_stem(stem: str, expected: str) -> None:
    assert _kind_for_stem(stem) == expected


def test_namespaced_id_for_class_item(tmp_path: Path) -> None:
    path = tmp_path / "classes.yaml"
    path.write_text(yaml.safe_dump({"classes": [{"name": "Knight"}]}))
    rendered = _render_file(path)
    assert 'id="class-knight"' in rendered


def test_namespaced_id_for_culture_item(tmp_path: Path) -> None:
    path = tmp_path / "cultures.yaml"
    path.write_text(yaml.safe_dump({"cultures": [{"name": "Knight"}]}))
    rendered = _render_file(path)
    assert 'id="culture-knight"' in rendered


def test_class_and_culture_with_same_name_do_not_collide(tmp_path: Path) -> None:
    classes = tmp_path / "classes.yaml"
    cultures = tmp_path / "cultures.yaml"
    classes.write_text(yaml.safe_dump({"classes": [{"name": "Knight"}]}))
    cultures.write_text(yaml.safe_dump({"cultures": [{"name": "Knight"}]}))
    rendered_classes = _render_file(classes)
    rendered_cultures = _render_file(cultures)
    assert 'id="class-knight"' in rendered_classes
    assert 'id="culture-knight"' in rendered_cultures


def test_top_level_dict_keys_keep_flat_slug(tmp_path: Path) -> None:
    """Top-level keys are unique within the file so they don't need namespacing."""
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"core_rules": {"description": "x"}}))
    rendered = _render_file(path)
    assert 'id="core-rules"' in rendered


def test_file_wrapper_keeps_existing_id(tmp_path: Path) -> None:
    path = tmp_path / "classes.yaml"
    path.write_text(yaml.safe_dump({"classes": [{"name": "Knight"}]}))
    rendered = _render_file(path)
    assert 'id="file-classes"' in rendered
