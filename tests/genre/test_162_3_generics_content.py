"""Story 162-3 — shipped-content assertion (GATED): the high-traffic worlds
author a bestiary ``generics:`` section.

The generics SEAM (schema + seating + loud failure) is unit-tested with zero
content in tests/game/test_162_3_bestiary_generics_schema.py and
tests/server/test_162_3_generics_last_resort_seating.py. THIS file is the
complementary content assertion, mirroring tests/genre/
test_world_bestiary_content.py: the story populates representative generics
for the recommended high-traffic worlds so their sessions have a sanctioned
last-resort Other the moment stub minting becomes a loud failure. Without
these rows, every unbacked router-named opponent in these worlds raises.

Scope (story context): 2-3 worlds now; the remaining catalog is follow-up
content work. Tier is deliberately unpinned — a pack may author generics at
the world tier (beneath_sunden pattern) or genre tier (neon_dystopia's
genre-root bestiary); the assertion rides ``effective_bestiary`` so either
authoring home satisfies it.

RED today: no bestiary carries a ``generics`` section (the field itself does
not exist until the schema half lands).
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")

# The story context's recommended high-traffic worlds (one per pack tier
# pattern: WWN world-tier, SWN world-tier, CWN genre-tier bestiary).
_GENERICS_WORLDS = (
    ("caverns_and_claudes", "beneath_sunden"),
    ("space_opera", "coyote_star"),
    ("neon_dystopia", "franchise_nations"),
)


def _load(pack_slug: str):
    try:
        return load_genre_pack(find_pack_path(pack_slug))
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))


@pytest.mark.parametrize(("pack_slug", "world_slug"), _GENERICS_WORLDS)
def test_high_traffic_world_authors_generics(pack_slug: str, world_slug: str) -> None:
    """Each recommended world resolves a non-empty ``generics`` section through
    the same effective-bestiary layering the seater uses."""
    pack = _load(pack_slug)
    bestiary, source = pack.effective_bestiary(world_slug)
    assert bestiary is not None, (
        f"{pack_slug}/{world_slug} resolves no bestiary at all (source tier: {source!r})"
    )
    generics = getattr(bestiary, "generics", None)
    assert generics, (
        f"{pack_slug}/{world_slug} authors no bestiary generics — the world has "
        f"no sanctioned last-resort Other and every unbacked opponent seat raises "
        f"(resolved from tier {source!r})"
    )


@pytest.mark.parametrize(("pack_slug", "world_slug"), _GENERICS_WORLDS)
def test_world_generics_ids_do_not_collide_with_entries(pack_slug: str, world_slug: str) -> None:
    """Identity is id-keyed (162-2): a generic sharing an id with a main-roster
    entry would fork identity between two stat blocks. The schema validator
    enforces this within one file; this guards the shipped layering too."""
    pack = _load(pack_slug)
    bestiary, _source = pack.effective_bestiary(world_slug)
    assert bestiary is not None
    generics = getattr(bestiary, "generics", None) or []
    assert generics, f"{pack_slug}/{world_slug}: no generics authored (see companion test)"
    entry_ids = {e.id for e in bestiary.entries}
    generic_ids = [g.id for g in generics]
    collisions = entry_ids.intersection(generic_ids)
    assert not collisions, (
        f"{pack_slug}/{world_slug}: generics ids collide with roster entries: "
        f"{sorted(collisions)!r}"
    )
    assert len(set(generic_ids)) == len(generic_ids), (
        f"{pack_slug}/{world_slug}: duplicate ids within generics: {generic_ids!r}"
    )
