"""encountergen must resolve cultures/archetypes world-over-genre.

Regression for the 2026-06-01 playtest finding that encountergen crashed
(``exit_code=1``) for ``the_real_mccoy`` — a combat genre that seeded zero
encounters. Root cause: the CLI read genre-raw ``pack.cultures`` /
``pack.archetypes`` instead of ``pack.effective_cultures(world)`` /
``pack.effective_archetypes(world)``. ``spaghetti_western`` ships an
intentionally empty genre ``cultures.yaml`` (flavor lives in the world per
epic-74), so the genre tier is empty and the CLI bailed with "genre
'spaghetti_western' has no cultures" even though ``the_real_mccoy`` supplies
seven cultures at the world tier. This is the same world-over-genre
divergence the ``GenrePack.effective_*`` docstring documents (perseus_cloud,
session 894) — namegen + pregen.seed_manual already resolve via ``effective_*``.

Lives in its own module (not ``test_encountergen.py``) because that module is
blanket-skipped by the caverns_sunden deprecation hook in ``tests/conftest.py``;
this test binds ``the_real_mccoy``, not the deprecated world.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sidequest.cli.encountergen.encountergen import main

CONTENT_ROOT = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"


def _mccoy_content_available() -> bool:
    return (CONTENT_ROOT / "spaghetti_western" / "worlds" / "the_real_mccoy" / "cultures").is_dir()


@pytest.mark.skipif(not _mccoy_content_available(), reason="the_real_mccoy not checked out")
def test_encountergen_resolves_world_tier_cultures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A world whose cultures live at the world tier seeds an enemy (rc 0)."""
    rc = main(
        [
            "--genre-packs-path",
            str(CONTENT_ROOT),
            "--genre",
            "spaghetti_western",
            "--world",
            "the_real_mccoy",
            "--count",
            "1",
            "--tier",
            "1",
        ]
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["enemies"], "expected at least one enemy from world-tier resolution"
