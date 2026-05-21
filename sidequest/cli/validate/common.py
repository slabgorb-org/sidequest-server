"""Shared helpers for ``pf validate <subcommand>`` validators.

Pulled out of ``locations.py`` + ``audio.py`` during the 50-9 verify pass
because both validators discover packs the same way and a third validator
(planned: ``pf validate world`` per ADR-109) would otherwise repeat the
same code. Keep additions here strictly to logic that is genuinely shared
across validators — domain-specific helpers belong in the validator module
that owns them.
"""

from __future__ import annotations

from pathlib import Path


def packs_in(root: Path) -> list[Path]:
    """Return every directory under ``root`` that looks like a genre pack.

    Two shapes are accepted: ``root`` is itself a pack (``pack.yaml``
    present at ``root``), or ``root`` is a directory containing many packs
    (each child with its own ``pack.yaml``). Children without a
    ``pack.yaml`` are silently skipped — a parent directory may legitimately
    hold residual ``images/``/``worlds/`` subtrees from a content-team
    pack relocation (see commit 0b7c311 moving heavy_metal +
    spaghetti_western to genre_workshopping/).
    """
    if not root.is_dir():
        return []
    if (root / "pack.yaml").is_file():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "pack.yaml").is_file())
