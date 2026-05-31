"""Story 71-31 — space_opera culture doctrine: genre = mechanics only, flavor at world tier.

**epic-74-strict** (Keith, 2026-05-31): genre packs ship MECHANICS ONLY; named/backstoried
cultures live at the WORLD tier ("Crunch in the Genre, Flavor in the World"). 74-1 already
made genre ``cultures.yaml`` OPTIONAL at the loader. This story DELETES space_opera's
genre-tier ``cultures.yaml`` (5 named cultures: Hegemonic, Frontier, Voidborn, Synthetic,
Xeno) and makes every live world self-sufficient for name-generation cultures.

Measured pre-fix state (real pack, 2026-05-31)::

    genre cultures : Hegemonic, Frontier, Voidborn, Synthetic, Xeno
    perseus_cloud  : source=world  (Spacer / Thari / Yulan)              ✅ self-sufficient
    aureate_span   : source=world  (its 5)                               ✅ self-sufficient
    coyote_star    : source=GENRE  (falls back to the genre set!)        ⚠️ NOT self-sufficient

``coyote_star`` is the load-bearing case: its 5 ``cultures/`` files are ``visual_tokens``-only
art overlays (no ``name:`` key), so the loader skips them (``loader.py:902-906``) → the world
declares 0 name-generation cultures → ``effective_cultures`` returns the GENRE set. The moment
the genre ``cultures.yaml`` is deleted, ``coyote_star`` namegen is orphaned unless world-tier
namegen cultures are authored (``broken_drift`` / ``free_miners`` / ``tsveri`` — the three the
genre never supplied; ``hegemonic`` / ``voidborn`` are a Dev/review decision — see TEA
Assessment). The genre→world key mismatch the Architect flagged (art keys broken_drift/
free_miners/tsveri vs borrowed genre names Frontier/Synthetic/Xeno) is resolved as a
side effect: the world authors namegen cultures whose keys match its own art overlays.

These tests load the REAL space_opera pack and assert behavior through
``GenrePack.effective_cultures`` — the single resolution path both namegen and
``pregen.seed_manual`` call (``pack.py``). No production-source grepping
(CLAUDE.md "No Source-Text Wiring Tests"); ``effective_cultures`` against a really-loaded
pack is the behavioral wiring assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

# The named/backstoried cultures that currently live at the space_opera GENRE tier and
# must NOT remain there after this story (epic-74-strict: genre = mechanics only).
GENRE_NAMED_CULTURES: frozenset[str] = frozenset(
    {"Hegemonic", "Frontier", "Voidborn", "Synthetic", "Xeno"}
)

# Genre-tier names that NO live world re-declares (perseus=spacer/thari/yulan,
# aureate=its 5, coyote=broken_drift/free_miners/tsveri[+hegemonic/voidborn]).
# After the migration these names must not appear in ANY world's resolved set —
# their presence means a world is still falling back to the (deleted) genre tier.
GENRE_ONLY_NAMES: frozenset[str] = frozenset({"Frontier", "Synthetic", "Xeno"})

# coyote_star's three world-unique cultures — they have NO genre equivalent, so they
# can only resolve if authored at the world tier. (hegemonic / voidborn share a name
# with deleted genre cultures and are an open Dev decision; deliberately NOT pinned.)
COYOTE_REQUIRED_UNIQUE: frozenset[str] = frozenset(
    {"broken_drift", "free_miners", "tsveri"}
)

LIVE_WORLDS: tuple[str, ...] = ("perseus_cloud", "aureate_span", "coyote_star")


def _has_real_content() -> bool:
    return (CONTENT_ROOT / "space_opera").is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_content(),
    reason="sidequest-content not on disk alongside sidequest-server",
)


def _norm(name: str) -> str:
    """Normalise a culture name to a key for identity comparison, tolerant of the
    snake_case-file → Title-Case-name convention Dev will use (matches perseus:
    spacer.yaml → "Spacer", aureate: cinder_collective.yaml → "Cinder Collective")."""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


@pytest.fixture(scope="module")
def space_opera_pack() -> GenrePack:
    return load_genre_pack(CONTENT_ROOT / "space_opera")


# --- AC1 / AC3: genre tier ships NO named/backstoried cultures --------------


def test_ac1_genre_tier_has_no_named_cultures(space_opera_pack: GenrePack) -> None:
    """Doctrine invariant: none of the named/backstoried cultures remain at the
    genre tier. This is the durable guard against future content re-adding flavor
    to the genre pack.

    RED today: genre cultures.yaml still declares all five.
    """
    genre_names = {c.name for c in space_opera_pack.cultures}
    leaked = genre_names & GENRE_NAMED_CULTURES
    assert not leaked, (
        f"space_opera GENRE tier still ships named/backstoried cultures {sorted(leaked)} — "
        f"epic-74-strict requires these move to the world tier. genre cultures: {sorted(genre_names)}"
    )


def test_ac1_genre_cultures_list_is_empty(space_opera_pack: GenrePack) -> None:
    """epic-74-strict end state (Keith, 2026-05-31): DELETE the genre cultures.yaml
    outright — genre = mechanics only, no residual culture scaffolding.

    RED today: genre tier carries 5 cultures.
    """
    genre_names = [c.name for c in space_opera_pack.cultures]
    assert genre_names == [], (
        f"space_opera genre cultures should be empty after deletion (epic-74-strict), "
        f"got {genre_names}"
    )


def test_ac3_pack_loads_cleanly_with_genre_cultures_gone(
    space_opera_pack: GenrePack,
) -> None:
    """Regression guard (74-1 made genre flavor optional): deleting the genre
    cultures.yaml must NOT break pack load — mechanics (rules) stay intact.

    The fixture loading at all proves no GenreLoadError; this pins that mechanics
    survive the flavor strip.
    """
    assert isinstance(space_opera_pack, GenrePack)
    assert space_opera_pack.rules is not None, (
        "deleting genre culture FLAVOR must not strip genre MECHANICS — rules.yaml gone"
    )


# --- AC2: each live world is authoritative for its own cultures -------------


def test_ac2_perseus_cloud_resolves_world_cultures(space_opera_pack: GenrePack) -> None:
    """Regression guard: perseus_cloud already self-sufficient — must stay world-sourced
    after the genre strip (this is the world whose Monster-Manual seeding bug — genre
    'Hegemonic' → 0 NPCs — motivated effective_cultures in the first place)."""
    cultures, source = space_opera_pack.effective_cultures("perseus_cloud")
    assert source == "world"
    assert {c.name for c in cultures} == {"Spacer", "Thari", "Yulan"}


def test_ac2_aureate_span_resolves_world_cultures(space_opera_pack: GenrePack) -> None:
    """Regression guard: aureate_span already self-sufficient — must stay world-sourced."""
    cultures, source = space_opera_pack.effective_cultures("aureate_span")
    assert source == "world"
    assert {c.name for c in cultures} == {
        "Cinder Collective",
        "Crystalline Choir",
        "Makhani",
        "Span Aristocracy",
        "Vaal-Kesh",
    }


def test_ac2_coyote_star_resolves_world_cultures_not_genre_fallback(
    space_opera_pack: GenrePack,
) -> None:
    """THE load-bearing case. coyote_star must author its own namegen cultures so it
    stops borrowing the (about-to-be-deleted) genre set.

    RED today: coyote_star has 0 namegen world cultures (5 files are visual_tokens-only)
    → effective_cultures returns source='genre' with the genre names.
    """
    cultures, source = space_opera_pack.effective_cultures("coyote_star")
    names = {c.name for c in cultures}
    norm_names = {_norm(n) for n in names}

    assert source == "world", (
        f"coyote_star must be authoritative for its own cultures, but resolved "
        f"source={source!r} (names={sorted(names)}). Its cultures/ dir holds only "
        f"visual_tokens art overlays — author namegen Culture files."
    )
    assert names, "coyote_star resolved an empty culture set"
    # Its three world-unique cultures (no genre equivalent) must be present.
    missing = COYOTE_REQUIRED_UNIQUE - norm_names
    assert not missing, (
        f"coyote_star missing world-unique namegen cultures {sorted(missing)} "
        f"(resolved: {sorted(names)})"
    )
    # And it must not be carrying genre-only flavor names (proof it is not falling back).
    leaked = {n for n in names if n in GENRE_ONLY_NAMES}
    assert not leaked, (
        f"coyote_star resolved genre-only culture names {sorted(leaked)} — still falling "
        f"back to the genre tier instead of resolving its own world cultures"
    )


# --- Doctrine invariant: no live world falls back to the genre tier ---------


@pytest.mark.parametrize("world", LIVE_WORLDS)
def test_no_space_opera_world_falls_back_to_genre_for_cultures(
    space_opera_pack: GenrePack, world: str
) -> None:
    """The durable doctrine guard: every live space_opera world supplies its own
    namegen cultures, so none depends on a genre-tier set that no longer exists.

    RED today: coyote_star resolves source='genre'.
    """
    cultures, source = space_opera_pack.effective_cultures(world)
    assert source == "world", (
        f"{world} falls back to the GENRE culture set (source={source!r}); after the "
        f"genre cultures.yaml is deleted this resolves to an empty namegen set and breaks "
        f"name generation. Author world-tier cultures for {world}."
    )
    assert cultures, f"{world} resolved an empty world culture list"
