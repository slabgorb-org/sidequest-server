"""Ruleset-tier SRD reference content: load, compose, and project player chapters.

Content lives in ``sidequest-content/rulesets/<ruleset>/srd/*.md`` (Markdown + YAML
front-matter). The four Without Number games share ``without_number/core`` and add a
thin per-game overlay; Fate is flat. See ADR-135/145/142 and the 2026-06-18 spec.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from sidequest.genre.error import GenreLoadError

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
    required front-matter key. Note: front-matter keys ``srd`` and ``license`` are
    validated as present but are intentionally not projected into the returned chapter
    dict; provenance and labels are added later by the projection layer.
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


RULESET_LABEL: dict[str, str] = {
    "fate": "The Rules of Fate Core",
    "wwn": "Worlds Without Number — Player Reference",
    "cwn": "Cities Without Number — Player Reference",
    "swn": "Stars Without Number — Player Reference",
    "awn": "Ashes Without Number — Player Reference",
}

_WN_ATTRIB = (
    "Reproduced from the {name} System Reference Document under its free-use terms. "
    "Not affiliated with, endorsed by, or reviewed by Sine Nomine Publishing."
)

RULESET_PROVENANCE: dict[str, dict] = {
    "fate": {
        "source": "Fate Core System (Evil Hat Productions)",
        "license": "ccby",
        "attribution": (
            "This work is based on Fate Core System (found at http://www.faterpg.com/), "
            "a product of Evil Hat Productions, LLC, developed, authored, and edited by "
            "Leonard Balsera, Brian Engard, Jeremy Keller, Ryan Macklin, Mike Olson, "
            "Clark Valentine, Amanda Valentine, Fred Hicks, and Rob Donoghue, and licensed "
            "for our use under the Creative Commons Attribution 3.0 Unported license "
            "(http://creativecommons.org/licenses/by/3.0/)."
        ),
    },
    "wwn": {
        "source": "Worlds Without Number SRD",
        "license": "wn-free",
        "attribution": _WN_ATTRIB.format(name="Worlds Without Number"),
    },
    "cwn": {
        "source": "Cities Without Number SRD",
        "license": "wn-free",
        "attribution": _WN_ATTRIB.format(name="Cities Without Number"),
    },
    "swn": {
        "source": "Stars Without Number SRD",
        "license": "wn-free",
        "attribution": _WN_ATTRIB.format(name="Stars Without Number"),
    },
    "awn": {
        "source": "Ashes Without Number SRD",
        "license": "wn-free",
        "attribution": _WN_ATTRIB.format(name="Ashes Without Number"),
    },
}


def build_ruleset_reference_section(ruleset: str, *, rulesets_root: Path) -> dict | None:
    """Build the ``rules_document`` section for ``ruleset``, or ``None`` if not applicable."""
    if ruleset not in BOUND_RULESET_SLUGS:
        return None
    chapters = load_ruleset_chapters(ruleset, rulesets_root=rulesets_root)
    if not chapters:
        return None
    return {
        "id": "ruleset_reference",
        "type": "rules_document",
        "label": RULESET_LABEL[ruleset],
        "ruleset": ruleset,
        "chapters": chapters,
        "provenance": RULESET_PROVENANCE[ruleset],
    }


# Rulesets whose reference content MUST be present + complete (fail-loud).
# Phase 1 ships Fate only; Plan B (WN family) extends this set.
RULESETS_WITH_REFERENCE = frozenset({"fate"})


def validate_ruleset_reference(
    ruleset: str,
    *,
    rulesets_root: Path,
    pack_name: str,
    required: frozenset[str] = RULESETS_WITH_REFERENCE,
) -> None:
    """Fail loud if a required ruleset has missing/unstamped reference content."""
    if ruleset not in required:
        return
    try:
        chapters = load_ruleset_chapters(ruleset, rulesets_root=rulesets_root)
    except RulesetReferenceError as exc:
        raise GenreLoadError(
            path=rulesets_root / ruleset,
            detail=f"pack '{pack_name}': malformed {ruleset} reference content — {exc}",
        ) from exc
    if not chapters:
        raise GenreLoadError(
            path=rulesets_root / ruleset / "srd",
            detail=(
                f"pack '{pack_name}': ruleset '{ruleset}' requires reference content under "
                f"rulesets/{ruleset}/srd/ but none was found (No Silent Fallbacks)"
            ),
        )
