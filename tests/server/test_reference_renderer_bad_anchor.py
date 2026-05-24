"""JSON island + bad-anchor banner are injected into rendered pages."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from sidequest.server.reference_renderer import assemble_rules_page


def _make_fixture_pack(root: Path) -> Path:
    pack = root / "fixture_pack"
    pack.mkdir()
    (pack / "classes.yaml").write_text(
        yaml.safe_dump({"classes": [{"name": "Knight"}, {"name": "Burglar"}]})
    )
    (pack / "rules.yaml").write_text(yaml.safe_dump({"core_rules": "Roll d6."}))
    return pack


def _extract_anchor_island(html: str) -> list[str]:
    match = re.search(
        r'<script id="ref-anchors" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "ref-anchors JSON island missing from rendered HTML"
    return json.loads(match.group(1))


def test_json_island_contains_namespaced_ids(tmp_path: Path) -> None:
    pack_dir = _make_fixture_pack(tmp_path)
    html = assemble_rules_page("fixture_pack", pack_dir)
    anchors = _extract_anchor_island(html)
    assert "class-knight" in anchors
    assert "class-burglar" in anchors
    assert "core-rules" in anchors
    assert "file-classes" in anchors
    assert "file-rules" in anchors


def test_anchor_island_has_no_duplicates(tmp_path: Path) -> None:
    pack_dir = _make_fixture_pack(tmp_path)
    html = assemble_rules_page("fixture_pack", pack_dir)
    anchors = _extract_anchor_island(html)
    assert len(anchors) == len(set(anchors))


def test_bad_anchor_banner_is_hidden_by_default(tmp_path: Path) -> None:
    pack_dir = _make_fixture_pack(tmp_path)
    html = assemble_rules_page("fixture_pack", pack_dir)
    assert '<div id="ref-bad-anchor" hidden>' in html


def test_bad_anchor_script_present(tmp_path: Path) -> None:
    pack_dir = _make_fixture_pack(tmp_path)
    html = assemble_rules_page("fixture_pack", pack_dir)
    # Look for the hash-check + banner-toggle behaviour.
    assert "location.hash" in html
    assert "ref-anchors" in html
    assert "ref-bad-anchor" in html
