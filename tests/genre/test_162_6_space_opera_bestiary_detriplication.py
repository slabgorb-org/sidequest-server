"""Story 162-6 — space_opera bestiary de-triplication (GATED content assertion).

PREMISE CORRECTION (TEA, 2026-07-06). The story's "byte-identical 12-entry world
bestiaries" was stale. The three space_opera world bestiaries had DIVERGED:

  - ``aureate_span``  (UNZONED) — 12 SWN entries, NO faction tags, no generics.
  - ``perseus_cloud`` (zoned)   — 12 SWN entries + ``factions: ["*"]`` (epic-157).
  - ``coyote_star``   (zoned)   — 12 SWN entries + ``factions: ["*"]`` PLUS a
                                  world-specific ``generics:`` section
                                  (``void_drifter`` / ``wreck_picker``, story
                                  162-3 — "Coyote Star collects them").

The 12 stat blocks are the SAME creatures with the SAME combat numbers in every
world; only the faction-tagging and coyote_star's generics differ.

``GenrePack.effective_bestiary`` is whole-file REPLACE ("the world set REPLACES
the genre pool", ``genre/models/pack.py``): a world resolves its OWN file if it
ships one, else falls through to the genre-tier ``GenrePack.bestiary``. So a world
collapses only if deleting its file and falling through to a genre-root file
reproduces its resolved creature set.

SCOPE (Keith, 2026-07-06 — "collapse perseus + aureate; retain coyote_star"):

  * NEW ``genre_packs/space_opera/bestiary.yaml`` (genre tier): the 12
    genre-generic SWN stat blocks, each tagged ``factions: ["*"]`` so a ZONED
    world falling through still satisfies the story-157-7 strict load validator
    (inert for the unzoned world — see below), no ``generics:`` section.
  * DELETE ``worlds/perseus_cloud/bestiary.yaml`` → falls through. It was
    byte-identical to the new genre root, so the resolved set is unchanged.
  * DELETE ``worlds/aureate_span/bestiary.yaml`` → falls through and inherits the
    ``factions: ["*"]`` tags. VERIFIED INERT: ``game/zone_eligibility.is_eligible``
    short-circuits to ``True`` for an unzoned world (aureate_span declares no
    ``controlled_by``) and for the ``"*"`` sentinel, so encounter behavior is
    byte-for-byte unchanged. The tag is the one tolerated model delta.
  * KEEP ``worlds/coyote_star/bestiary.yaml`` → its world-specific generics
    cannot move to the genre root under whole-file-replace (they would leak into
    every falling-through world). Its base 12 stay duplicated by necessity.

INVARIANT under test: every space_opera world's resolved creature-STAT set is
unchanged; ``factions`` is the sole tolerated delta (aureate_span), and it is
behaviorally inert.

long_foundry "same pattern check" (AC4): the heavy_metal worlds ship DISTINCT
world-flavored rosters (dirge_conscript vs reniksnad_madded_slave vs ulsio) — no
byte-identical triplication exists there, so no collapse applies. Pinned below so
a future accidental collapse reddens.

The ``effective_bestiary`` SEAM logic is unit-tested with synthetic fixtures in
``tests/cli/test_encountergen_bestiary_90_1.py``. THIS file is the complementary
CONTENT assertion — skipped when ``sidequest-content`` is absent, the same split
as ``tests/genre/test_world_bestiary_content.py`` — so a content repoint can
never redden the seam suite.

RED today: no genre-root ``space_opera/bestiary.yaml`` exists and both target
worlds still ship their own file, so the end-state assertions fail. The
invariant/guard assertions pass now AND must keep passing after Dev's collapse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.bestiary import Bestiary
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")

# The 12 genre-generic SWN stat blocks: id -> (level, hp, armor_class,
# attack_bonus, damage). Captured from the pre-collapse world files; the stat
# lines are identical across all three space_opera worlds (only faction tags and
# coyote_star's generics differed). This is the frozen characterization golden:
# the genre root must reproduce it exactly, and no world's resolved set may drift.
CANONICAL_SWN_BLOCK: dict[str, tuple[int, int, int, int, str]] = {
    "dock_tough": (1, 4, 12, 1, "1d6"),
    "pirate_deckhand": (1, 4, 10, 1, "1d8"),
    "security_bot": (1, 4, 15, 1, "1d8"),
    "pirate_veteran": (2, 9, 14, 2, "1d8+1"),
    "soldier_bot": (2, 9, 16, 1, "1d10"),
    "mercenary_elite": (3, 14, 16, 4, "1d8+1"),
    "gengineered_killer": (4, 18, 16, 5, "1d8+1"),
    "void_predator": (5, 22, 15, 6, "1d10"),
    "heavy_warbot": (6, 27, 18, 8, "2d8"),
    "mercenary_commander": (6, 27, 16, 8, "1d8+3"),
    "pirate_king": (7, 32, 18, 9, "1d8+2"),
    "apex_predator": (8, 36, 16, 8, "1d10"),
}
EXPECTED_ENTRY_IDS = frozenset(CANONICAL_SWN_BLOCK)

# Worlds whose per-world bestiary.yaml is deleted so they fall through to the new
# genre root.
COLLAPSED_WORLDS = ("perseus_cloud", "aureate_span")
# The world that keeps its own file (world-specific generics can't be genre-tiered).
RETAINED_WORLD = "coyote_star"
COYOTE_GENERIC_IDS = frozenset({"void_drifter", "wreck_picker"})


def _load(pack_slug: str):
    try:
        return load_genre_pack(find_pack_path(pack_slug))
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))


def _pack_dir(pack_slug: str) -> Path:
    try:
        return find_pack_path(pack_slug)
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))


def _core_stats(entry) -> tuple[int, int, int, int, str | None]:
    return (entry.level, entry.hp, entry.armor_class, entry.attack_bonus, entry.damage)


def _by_id(bestiary: Bestiary) -> dict[str, object]:
    return {e.id: e for e in bestiary.entries}


# ---------------------------------------------------------------------------
# Phase A — end-state assertions (RED today).
# ---------------------------------------------------------------------------


def test_genre_root_bestiary_ships_the_twelve_generic_entries() -> None:
    """RED. The de-triplication creates a genre-tier ``space_opera/bestiary.yaml``
    holding exactly the 12 genre-generic SWN stat blocks and NO ``generics``
    section (world-specific generics stay at the world tier)."""
    pack = _load("space_opera")
    assert pack.bestiary is not None, (
        "de-triplication must create a genre-tier space_opera/bestiary.yaml so "
        "perseus_cloud and aureate_span have something to fall through to"
    )
    ids = {e.id for e in pack.bestiary.entries}
    assert ids == EXPECTED_ENTRY_IDS, (
        f"genre-root roster mismatch: unexpected={sorted(ids - EXPECTED_ENTRY_IDS)} "
        f"missing={sorted(EXPECTED_ENTRY_IDS - ids)}"
    )
    assert not pack.bestiary.generics, (
        "the genre root must NOT carry coyote_star's world-specific generics — "
        "they would leak into every falling-through world"
    )


def test_genre_root_entries_are_world_global_faction_tagged() -> None:
    """RED. Every genre-root entry is tagged ``factions: ["*"]`` so a ZONED world
    (perseus_cloud) that falls through still passes the story-157-7 strict load
    validator, which requires non-empty faction tags on pooled content."""
    pack = _load("space_opera")
    assert pack.bestiary is not None, "precondition: genre root exists (see companion test)"
    untagged = [e.id for e in pack.bestiary.entries if e.factions != ["*"]]
    assert not untagged, (
        f"genre-root entries missing factions:['*']: {untagged} — a zoned world "
        "falling through would fail the 157-7 strict load validator"
    )


@pytest.mark.parametrize(("entry_id", "stats"), sorted(CANONICAL_SWN_BLOCK.items()))
def test_genre_root_entry_stats_match_canonical_swn_block(
    entry_id: str, stats: tuple[int, int, int, int, str]
) -> None:
    """RED. Each genre-root entry reproduces the canonical SWN stat line exactly —
    the collapse must not silently retune any creature's combat numbers."""
    pack = _load("space_opera")
    assert pack.bestiary is not None, "precondition: genre root exists (see companion test)"
    by_id = _by_id(pack.bestiary)
    assert entry_id in by_id, f"genre root missing entry {entry_id!r}"
    assert _core_stats(by_id[entry_id]) == stats, (
        f"{entry_id} stat drift: got {_core_stats(by_id[entry_id])}, expected {stats}"
    )


@pytest.mark.parametrize("world", COLLAPSED_WORLDS)
def test_collapsed_world_ships_no_own_bestiary(world: str) -> None:
    """RED. The collapsed worlds no longer author their own bestiary — the loader
    sees no world-tier file, which is what forces the genre-tier fall-through."""
    pack = _load("space_opera")
    world_opt = pack.worlds.get(world)
    assert world_opt is not None, f"precondition: {world} world present in pack"
    assert world_opt.bestiary is None, (
        f"{world} must ship no world-tier bestiary after de-triplication so "
        "effective_bestiary falls through to the genre root"
    )


@pytest.mark.parametrize("world", COLLAPSED_WORLDS)
def test_collapsed_world_resolves_to_genre_tier(world: str) -> None:
    """RED (wiring). ``effective_bestiary`` resolves the collapsed world to the
    genre tier and returns the very same genre-root object — proving the new
    content file flows end-to-end through the production resolver."""
    pack = _load("space_opera")
    bestiary, source = pack.effective_bestiary(world)
    assert source == "genre", f"{world} must resolve from the genre tier, got {source!r}"
    assert bestiary is not None and bestiary is pack.bestiary, (
        f"{world} effective bestiary must BE the genre-root object (whole-file "
        "replace fall-through), not a distinct copy"
    )


@pytest.mark.parametrize("world", COLLAPSED_WORLDS)
def test_collapsed_world_bestiary_file_removed_from_disk(world: str) -> None:
    """RED (content deliverable). The per-world ``bestiary.yaml`` is physically
    gone. This checks a DATA file, not source shape — the removal IS the story's
    deliverable, and the model-level ``world.bestiary is None`` check above proves
    the behavioral consequence."""
    pack_dir = _pack_dir("space_opera")
    path = pack_dir / "worlds" / world / "bestiary.yaml"
    assert not path.exists(), (
        f"{world}/bestiary.yaml must be deleted (it duplicates the genre root); "
        f"still present at {path}"
    )


# ---------------------------------------------------------------------------
# Phase B — invariants & guards (pass now, MUST keep passing after the collapse).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("world", COLLAPSED_WORLDS)
def test_collapsed_world_effective_stats_unchanged(world: str) -> None:
    """INVARIANT. The whole point: a collapsed world's resolved creature-stat set
    is identical to what it shipped before. Compares the stat-bearing fields only
    (``factions`` is the tolerated delta — inert on the unzoned aureate_span)."""
    pack = _load("space_opera")
    bestiary, _source = pack.effective_bestiary(world)
    assert bestiary is not None, f"{world} resolves no bestiary"
    by_id = _by_id(bestiary)
    assert set(by_id) == EXPECTED_ENTRY_IDS, (
        f"{world} resolved roster changed: "
        f"unexpected={sorted(set(by_id) - EXPECTED_ENTRY_IDS)} "
        f"missing={sorted(EXPECTED_ENTRY_IDS - set(by_id))}"
    )
    drifted = {
        eid: _core_stats(by_id[eid])
        for eid, stats in CANONICAL_SWN_BLOCK.items()
        if _core_stats(by_id[eid]) != stats
    }
    assert not drifted, f"{world} resolved stat drift vs pre-collapse golden: {drifted}"


@pytest.mark.parametrize("world", COLLAPSED_WORLDS)
def test_collapsed_world_does_not_inherit_coyote_generics(world: str) -> None:
    """INVARIANT. A collapsed world must NOT gain coyote_star's world-specific
    generics. Guards the failure mode where Dev wrongly hoists the generics to the
    genre root — that would inject void_drifter/wreck_picker into every world."""
    pack = _load("space_opera")
    bestiary, _source = pack.effective_bestiary(world)
    assert bestiary is not None
    leaked = {g.id for g in bestiary.generics} & COYOTE_GENERIC_IDS
    assert not leaked, (
        f"{world} inherited coyote_star's world-specific generics {sorted(leaked)} "
        "— generics must stay at the coyote_star world tier"
    )


def test_coyote_star_retains_world_bestiary_and_generics() -> None:
    """GUARD. coyote_star keeps its own file (world-specific generics can't be
    genre-tiered under whole-file-replace). Its resolution is untouched by the
    collapse: source 'world', its two generics intact, file still on disk."""
    pack = _load("space_opera")
    world_opt = pack.worlds.get(RETAINED_WORLD)
    assert world_opt is not None and world_opt.bestiary is not None, (
        "coyote_star must retain its world-tier bestiary"
    )
    bestiary, source = pack.effective_bestiary(RETAINED_WORLD)
    assert source == "world", "coyote_star must still resolve from its own world tier"
    assert bestiary is not None
    generic_ids = {g.id for g in bestiary.generics}
    assert generic_ids == COYOTE_GENERIC_IDS, (
        f"coyote_star generics changed: {sorted(generic_ids)} != {sorted(COYOTE_GENERIC_IDS)}"
    )
    path = _pack_dir("space_opera") / "worlds" / RETAINED_WORLD / "bestiary.yaml"
    assert path.is_file(), f"coyote_star must keep its bestiary file at {path}"


def test_retained_coyote_star_base_roster_matches_genre_generic_block() -> None:
    """INVARIANT. coyote_star's base 12 entries stay identical to the genre-generic
    block, so the retained duplicate never silently diverges from the genre root."""
    pack = _load("space_opera")
    bestiary, _source = pack.effective_bestiary(RETAINED_WORLD)
    assert bestiary is not None
    by_id = _by_id(bestiary)
    assert set(by_id) == EXPECTED_ENTRY_IDS, (
        f"coyote_star base roster diverged from the genre-generic set: "
        f"unexpected={sorted(set(by_id) - EXPECTED_ENTRY_IDS)} "
        f"missing={sorted(EXPECTED_ENTRY_IDS - set(by_id))}"
    )
    drifted = {
        eid: _core_stats(by_id[eid])
        for eid, stats in CANONICAL_SWN_BLOCK.items()
        if _core_stats(by_id[eid]) != stats
    }
    assert not drifted, f"coyote_star base stat drift vs genre-generic golden: {drifted}"


# ---------------------------------------------------------------------------
# Phase C — long_foundry "same pattern check" (AC4): no triplication → no action.
# ---------------------------------------------------------------------------

_HEAVY_METAL_WORLDS = ("barsoom", "evropi", "long_foundry")


def test_long_foundry_pattern_check_finds_no_triplication() -> None:
    """AC4 evidence. The "same pattern check for long_foundry": the heavy_metal
    worlds ship DISTINCT world-flavored rosters, so no byte-identical triplication
    exists and no genre-root collapse applies. Assert there is NO heavy_metal
    genre-root bestiary, every world resolves from its OWN tier, and the rosters
    are pairwise non-identical. Reddens if a future edit ever makes them identical
    (a real triplication that WOULD then warrant collapse)."""
    pack = _load("heavy_metal")
    assert pack.bestiary is None, (
        "heavy_metal ships no genre-root bestiary — its worlds own distinct "
        "rosters, so 162-6 makes no change here"
    )
    id_sets: dict[str, frozenset[str]] = {}
    for world in _HEAVY_METAL_WORLDS:
        assert world in pack.worlds, f"precondition: heavy_metal world {world} present"
        bestiary, source = pack.effective_bestiary(world)
        assert source == "world", f"{world} must resolve from its own world tier, got {source!r}"
        assert bestiary is not None and bestiary.entries
        id_sets[world] = frozenset(e.id for e in bestiary.entries)
    pairs = [
        ("barsoom", "evropi"),
        ("barsoom", "long_foundry"),
        ("evropi", "long_foundry"),
    ]
    identical = [(a, b) for a, b in pairs if id_sets[a] == id_sets[b]]
    assert not identical, (
        f"heavy_metal worlds have IDENTICAL rosters {identical} — that is a real "
        "triplication to collapse; the 162-6 'no pattern here' finding is now stale"
    )
