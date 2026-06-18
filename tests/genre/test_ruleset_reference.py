from pathlib import Path

import pytest

from sidequest.genre.ruleset_reference import (
    RulesetReferenceError,
    load_ruleset_chapters,
    parse_frontmatter,
)


def _write(p: Path, anchor: str, title: str, order: int, body: str, *, srd="fixture", lic="ccby") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nsrd: {srd}\nsrd_ref: \"{title}\"\nlicense: {lic}\n"
        f"anchor: {anchor}\ntitle: {title}\norder: {order}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_parse_frontmatter_splits_meta_and_body():
    meta, body = parse_frontmatter("---\nanchor: x\ntitle: X\n---\nHello **world**.\n")
    assert meta["anchor"] == "x"
    assert body.strip() == "Hello **world**."


def test_parse_frontmatter_missing_delimiter_raises():
    with pytest.raises(RulesetReferenceError):
        parse_frontmatter("no front-matter here")


def test_load_flat_ruleset_orders_by_order_field(tmp_path: Path):
    root = tmp_path / "rulesets"
    _write(root / "fixturefate" / "srd" / "b.md", "asp", "Aspects", 2, "Aspects body")
    _write(root / "fixturefate" / "srd" / "a.md", "basics", "Basics", 1, "Basics body")
    chapters = load_ruleset_chapters("fixturefate", rulesets_root=root)
    assert [c["anchor"] for c in chapters] == ["basics", "asp"]
    assert chapters[0]["body_markdown"].strip() == "Basics body"


def test_missing_required_frontmatter_key_raises(tmp_path: Path):
    root = tmp_path / "rulesets"
    p = root / "fixturefate" / "srd" / "bad.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntitle: NoAnchor\norder: 1\n---\nbody\n", encoding="utf-8")
    with pytest.raises(RulesetReferenceError):
        load_ruleset_chapters("fixturefate", rulesets_root=root)


def test_wn_overlay_overrides_core_by_anchor(tmp_path: Path):
    root = tmp_path / "rulesets"
    _write(root / "without_number" / "core" / "srd" / "combat.md", "combat", "Combat (core)", 1, "Core combat body")
    _write(root / "without_number" / "core" / "srd" / "magic.md", "magic", "Magic", 2, "Core magic body")
    _write(root / "without_number" / "wwn" / "srd" / "combat.md", "combat", "Combat (wwn)", 1, "WWN combat body")
    chapters = load_ruleset_chapters("wwn", rulesets_root=root)
    by_anchor = {c["anchor"]: c for c in chapters}
    assert by_anchor["combat"]["title"] == "Combat (wwn)"          # overlay won
    assert by_anchor["combat"]["body_markdown"].strip() == "WWN combat body"
    assert by_anchor["magic"]["title"] == "Magic"                  # core-only chapter preserved
    assert [c["anchor"] for c in chapters] == ["combat", "magic"]  # ordered by `order`


def test_build_section_none_for_native_ruleset(tmp_path: Path):
    from sidequest.genre.ruleset_reference import build_ruleset_reference_section

    assert build_ruleset_reference_section("dial", rulesets_root=tmp_path / "rulesets") is None


def test_build_section_none_when_unauthored(tmp_path: Path):
    from sidequest.genre.ruleset_reference import build_ruleset_reference_section

    # 'fate' is bound but no content on disk -> None (gate, not builder, enforces presence)
    assert build_ruleset_reference_section("fate", rulesets_root=tmp_path / "rulesets") is None


def test_build_section_shape_and_provenance(tmp_path: Path):
    from sidequest.genre.ruleset_reference import build_ruleset_reference_section

    root = tmp_path / "rulesets"
    _write(root / "fate" / "srd" / "01.md", "fate-basics", "The Basics", 1, "How play works.")
    section = build_ruleset_reference_section("fate", rulesets_root=root)
    assert section is not None
    assert section["id"] == "ruleset_reference"
    assert section["type"] == "rules_document"
    assert section["ruleset"] == "fate"
    assert section["label"] == "The Rules of Fate Core"
    assert section["chapters"][0]["anchor"] == "fate-basics"
    assert section["provenance"]["license"] == "ccby"
    assert "Creative Commons" in section["provenance"]["attribution"]
