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
