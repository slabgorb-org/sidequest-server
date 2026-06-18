"""Ruleset-tier SRD reference content: load, compose, and project player chapters.

Content lives in ``sidequest-content/rulesets/<ruleset>/srd/*.md`` (Markdown + YAML
front-matter). The four Without Number games share ``without_number/core`` and add a
thin per-game overlay; Fate is flat. See ADR-135/145/142 and the 2026-06-18 spec.
"""

from __future__ import annotations

from pathlib import Path

import yaml

BOUND_RULESET_SLUGS = frozenset({"fate", "wwn", "cwn", "swn", "awn"})
WN_FAMILY = frozenset({"wwn", "cwn", "swn", "awn"})
REQUIRED_FRONTMATTER = ("srd", "srd_ref", "license", "anchor", "title", "order")


class RulesetReferenceError(Exception):
    """Raised when ruleset reference content is missing or malformed."""


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a Markdown file into (front-matter dict, body). Fail loud if absent."""
    if not text.startswith("---"):
        raise RulesetReferenceError("missing '---' front-matter delimiter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise RulesetReferenceError("unterminated '---' front-matter block")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise RulesetReferenceError("front-matter is not a mapping")
    return meta, parts[2].lstrip("\n")


def _chapter_dirs(ruleset: str, rulesets_root: Path) -> list[Path]:
    if ruleset in WN_FAMILY:
        return [
            rulesets_root / "without_number" / "core" / "srd",
            rulesets_root / "without_number" / ruleset / "srd",
        ]
    return [rulesets_root / ruleset / "srd"]


def load_ruleset_chapters(ruleset: str, *, rulesets_root: Path) -> list[dict]:
    """Compose the ordered player chapters for ``ruleset``.

    Flat for Fate; core + per-game overlay for the WN family (overlay overrides core
    by shared ``anchor``). Raises ``RulesetReferenceError`` on a file missing any
    required front-matter key.
    """
    by_anchor: dict[str, dict] = {}
    for chapter_dir in _chapter_dirs(ruleset, rulesets_root):
        if not chapter_dir.is_dir():
            continue
        for md in sorted(chapter_dir.glob("*.md")):
            meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
            missing = [k for k in REQUIRED_FRONTMATTER if k not in meta]
            if missing:
                raise RulesetReferenceError(f"{md}: missing front-matter {missing}")
            by_anchor[str(meta["anchor"])] = {
                "anchor": str(meta["anchor"]),
                "title": str(meta["title"]),
                "order": int(meta["order"]),
                "srd_ref": str(meta["srd_ref"]),
                "body_markdown": body,
            }
    return sorted(by_anchor.values(), key=lambda c: c["order"])
