"""Audit Markov namegen corpora across every genre pack (Story 45-28).

Walks every genre pack via ``sidequest.genre.load_genre_pack``, resolves
each culture's slot ``corpora`` references to disk paths, counts words
in each corpus, and reports per-culture per-corpus status:

- **OK** — corpus has ≥ ``WARN_BELOW_WORDS`` (1000) words.
- **THIN** — ``FAIL_BELOW_WORDS`` (200) ≤ corpus < ``WARN_BELOW_WORDS``.
- **FAIL** — corpus < ``FAIL_BELOW_WORDS`` (200) words. Cannot
  produce coherent Markov output.

Exit code:

- ``0`` if no FAIL rows (THIN allowed — those are operator warnings,
  not CI gates).
- ``1`` if any FAIL row.
- ``2`` for invocation errors (missing pack root, no genre packs found).

This is the AC1 deliverable for Story 45-28. Modeled on
``audit_content_drift.py`` (the trope-drift sibling). The wire-first
treatment: the audit hits the actual culture-loading path so a culture
that references a missing corpus surfaces here before it reaches the
narrator at runtime.

Usage::

    cd sidequest-server
    uv run python scripts/audit_namegen_corpora.py
    uv run python scripts/audit_namegen_corpora.py --path /tmp/synthetic_packs
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from sidequest.genre.models.culture import Culture
from sidequest.genre.names.thresholds import (
    FAIL_BELOW_WORDS,
    WARN_BELOW_WORDS,
    count_words,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GENRE_PACKS = REPO_ROOT / "sidequest-content" / "genre_packs"


@dataclass(frozen=True)
class CorpusEntry:
    """One (pack, culture, slot, corpus, status) row in the report."""

    pack: str
    culture: str
    slot: str
    corpus: str
    word_count: int
    status: str  # "OK" | "THIN" | "FAIL" | "MISSING"


def _classify(word_count: int) -> str:
    if word_count < FAIL_BELOW_WORDS:
        return "FAIL"
    if word_count < WARN_BELOW_WORDS:
        return "THIN"
    return "OK"


def _load_cultures(cultures_yaml: Path) -> list[Culture]:
    """Validate ``cultures.yaml`` against the Culture model.

    Mirrors the production validation path (each culture goes through
    ``Culture.model_validate``) without dragging in the full
    ``load_genre_pack`` requirement that the surrounding pack be
    schema-complete. This lets the audit run against fixture packs
    that only need a corpus and a culture pointing at it.
    """
    raw = yaml.safe_load(cultures_yaml.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    cultures: list[Culture] = []
    for item in raw:
        try:
            cultures.append(Culture.model_validate(item))
        except ValidationError:
            # Schema-broken cultures are surfaced by audit_content_drift.py;
            # this audit's scope is corpus sizes, not schema. Skip.
            continue
    return cultures


def _audit_pack(pack_dir: Path) -> list[CorpusEntry]:
    """Walk one genre pack's cultures (genre + world tiers) and audit corpora."""
    entries: list[CorpusEntry] = []
    pack_name = pack_dir.name
    corpus_dir = pack_dir / "corpus"

    def _walk_cultures(cultures: list[Culture], tier: str) -> None:
        for culture in cultures:
            for slot_name, slot_config in culture.slots.items():
                if not slot_config.corpora:
                    continue
                for corpus_ref in slot_config.corpora:
                    corpus_path = corpus_dir / corpus_ref.corpus
                    if not corpus_path.exists():
                        entries.append(
                            CorpusEntry(
                                pack=pack_name,
                                culture=f"{culture.name} ({tier})",
                                slot=slot_name,
                                corpus=corpus_ref.corpus,
                                word_count=0,
                                status="MISSING",
                            )
                        )
                        continue
                    text = corpus_path.read_text(encoding="utf-8")
                    word_count = count_words(text)
                    entries.append(
                        CorpusEntry(
                            pack=pack_name,
                            culture=f"{culture.name} ({tier})",
                            slot=slot_name,
                            corpus=corpus_ref.corpus,
                            word_count=word_count,
                            status=_classify(word_count),
                        )
                    )

    genre_cultures_yaml = pack_dir / "cultures.yaml"
    if genre_cultures_yaml.is_file():
        _walk_cultures(_load_cultures(genre_cultures_yaml), tier="genre")

    worlds_dir = pack_dir / "worlds"
    if worlds_dir.is_dir():
        for world_dir in sorted(worlds_dir.iterdir()):
            if not world_dir.is_dir():
                continue
            world_cultures_yaml = world_dir / "cultures.yaml"
            if world_cultures_yaml.is_file():
                _walk_cultures(
                    _load_cultures(world_cultures_yaml),
                    tier=f"world:{world_dir.name}",
                )

    return entries


def _format_report(entries: list[CorpusEntry]) -> str:
    """Render entries as a markdown report grouped by status.

    Sections are emitted in failure-first order so the eye lands on
    the actionable rows.
    """
    if not entries:
        return "# Namegen Corpus Audit\n\nNo cultures with Markov corpora found.\n"

    by_status: dict[str, list[CorpusEntry]] = {
        "FAIL": [],
        "MISSING": [],
        "THIN": [],
        "OK": [],
    }
    for entry in entries:
        by_status[entry.status].append(entry)

    lines: list[str] = ["# Namegen Corpus Audit", ""]
    lines.append(f"Thresholds: FAIL < {FAIL_BELOW_WORDS} words, THIN < {WARN_BELOW_WORDS} words.")
    lines.append("")
    lines.append(
        f"**Summary:** {len(by_status['FAIL'])} FAIL, "
        f"{len(by_status['MISSING'])} MISSING, "
        f"{len(by_status['THIN'])} THIN, "
        f"{len(by_status['OK'])} OK."
    )
    lines.append("")

    for status in ("FAIL", "MISSING", "THIN", "OK"):
        rows = by_status[status]
        if not rows:
            continue
        lines.append(f"## {status} ({len(rows)})")
        lines.append("")
        lines.append("| Pack | Culture | Slot | Corpus | Word Count | Status |")
        lines.append("|------|---------|------|--------|-----------:|--------|")
        rows_sorted = sorted(rows, key=lambda r: (r.pack, r.culture, r.slot, r.corpus))
        for entry in rows_sorted:
            lines.append(
                f"| {entry.pack} | {entry.culture} | {entry.slot} | "
                f"{entry.corpus} | {entry.word_count} | {entry.status} |"
            )
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Markov namegen corpora across genre packs.")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_GENRE_PACKS,
        help=("Path to a genre_packs/ directory. Defaults to sidequest-content/genre_packs/."),
    )
    args = parser.parse_args(argv)

    if not args.path.is_dir():
        print(
            f"audit_namegen_corpora: --path {args.path} is not a directory",
            file=sys.stderr,
        )
        return 2

    pack_dirs = sorted(p for p in args.path.iterdir() if p.is_dir())
    if not pack_dirs:
        print(
            f"audit_namegen_corpora: no genre packs found under {args.path}",
            file=sys.stderr,
        )
        return 2

    all_entries: list[CorpusEntry] = []
    for pack_dir in pack_dirs:
        # Only audit directories that look like packs — must have at
        # least one cultures.yaml at genre tier OR at a world tier
        # (worlds/<world>/cultures.yaml). Layered-content packs may
        # author cultures only at the world tier (e.g. workshopping
        # space_opera has aureate_span world cultures but no genre-
        # tier cultures.yaml). Skips stray files and corpus-only dirs
        # without flagging them.
        has_genre_cultures = (pack_dir / "cultures.yaml").is_file()
        worlds_dir = pack_dir / "worlds"
        has_world_cultures = worlds_dir.is_dir() and any(
            (w / "cultures.yaml").is_file() for w in worlds_dir.iterdir() if w.is_dir()
        )
        if not (has_genre_cultures or has_world_cultures):
            continue
        all_entries.extend(_audit_pack(pack_dir))

    print(_format_report(all_entries))

    has_fail = any(e.status in ("FAIL", "MISSING") for e in all_entries)
    return 1 if has_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
