"""Test helper for resolving SYNTHETIC fixture genre-pack paths.

Story 96-1 (epic 96, follow-up to the epic 94 genre/world boundary
migration): server tests must not couple to live ``sidequest-content``
packs — content-only changes must never turn server tests red. Validators
validate content; tests test fixtures. This helper is the fixture-side
counterpart of ``tests/_helpers/genre_paths.py`` (which resolves LIVE
packs and should only be used by tests that genuinely contract against
shipping content — as of 96-1, none of the rewritten ones do).

Fixture packs live under ``tests/fixtures/genre_packs/``:

- ``swn_test_pack`` — minimal Stars Without Number pack (ruleset: swn)
  with firefight / melee / dogfight ConfrontationDefs and a single world
  ``test_world`` whose world-tier ``inventory.yaml`` carries the weapons
  the combat tests contract on (``blaster_sidearm``, ``multifocal_laser``).
  The world-tier catalog exercises the production REPLACE path in
  ``resolve_inventory`` — the same shape live migrated packs use.
- ``wwn_test_pack`` — minimal Worlds Without Number pack (ruleset: wwn)
  whose ``test_world`` authors world-tier caster Callings (``Psychic``,
  ``Gadgeteer``) for the world-tier chargen seeding tests.
- ``test_genre`` — the long-standing frozen mutant_wasteland clone used
  by the server-layer conftest's default search path.
- ``reference_v2_fixture`` — reference-page projection fixture pack.

Use ``load_fixture_pack("swn_test_pack")`` to get a loaded GenrePack.
**No silent fallback:** a missing fixture pack raises ``FixturePackNotFound``
— it is a repo defect (the fixture ships with the tests), never an
environment condition, so callers must NOT ``pytest.skip`` on it.
"""

from __future__ import annotations

from pathlib import Path

# tests/_helpers/fixture_packs.py -> _helpers -> tests
_TESTS_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PACKS_DIR = _TESTS_ROOT / "fixtures" / "genre_packs"

SWN_TEST_PACK = "swn_test_pack"
WWN_TEST_PACK = "wwn_test_pack"
TEST_WORLD = "test_world"


class FixturePackNotFound(FileNotFoundError):
    """Raised when a fixture slug has no pack.yaml under tests/fixtures."""


def fixture_pack_path(slug: str) -> Path:
    """Return the on-disk directory of a fixture pack by slug.

    Raises ``FixturePackNotFound`` when ``pack.yaml`` is absent — fixture
    packs are part of the test suite, so absence is a defect, not a
    skippable environment gap.
    """
    candidate = FIXTURE_PACKS_DIR / slug
    if candidate.is_dir() and (candidate / "pack.yaml").is_file():
        return candidate
    raise FixturePackNotFound(
        f"fixture genre pack {slug!r} not found (no pack.yaml under {FIXTURE_PACKS_DIR})"
    )


def load_fixture_pack(slug: str):
    """Load a fixture pack through the production loader (no shortcuts)."""
    from sidequest.genre.loader import load_genre_pack

    return load_genre_pack(fixture_pack_path(slug))
