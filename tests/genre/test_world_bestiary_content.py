"""Shipped-content validation (GATED): ruleset-module worlds author resolvable
bestiaries.

The ``effective_bestiary`` SEAM logic — world-over-genre REPLACE, fail-loud
routing — is unit-tested with zero content in
``tests/cli/test_encountergen_bestiary_90_1.py`` (in-memory + synthetic
fixture). THIS file is the complementary *content* assertion: it checks that the
real shipped packs satisfy the contract, and it is skipped when
``sidequest-content`` is not on disk. Keeping the two apart is deliberate — a
content repoint must never be able to redden the seam suite (the prod-rows-in-
tests anti-pattern that originally coupled a content move to server-test
breakage).

Genre/world repoint context: creature rosters moved off the genre tier to
``worlds/<slug>/bestiary.yaml`` ("genre is the rulebook, the world owns the
cast/catalog"). Every world of a live ruleset-module pack must therefore resolve
a non-empty bestiary so the Monster Manual pool is never silently empty
(ADR-117 + No Silent Fallbacks).
"""

from __future__ import annotations

import pytest
import yaml

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")

BESTIARY_ENTRY_REQUIRED_FIELDS = {"id", "name", "level", "hp", "armor_class", "attack_bonus"}

# Packs that may bind a ruleset module (ADR-117). Native packs among these are
# skipped per-pack at runtime — the assertion only applies to ruleset != native.
_CANDIDATE_RULESET_PACKS = (
    "heavy_metal",
    "elemental_harmony",
    "neon_dystopia",
    "space_opera",
    "mutant_wasteland",
    "road_warrior",
)


def test_ruleset_module_worlds_resolve_a_nonempty_bestiary() -> None:
    """Every world of a live ruleset-module pack resolves a non-None, non-empty
    ``effective_bestiary`` — from the world tier or the genre tier. Pins the seam
    as ruleset-generic across shipped content, not a heavy_metal special case."""
    failures: list[str] = []
    found_any = False
    for slug in _CANDIDATE_RULESET_PACKS:
        try:
            pack_dir = find_pack_path(slug)
        except PackNotFound:
            continue
        pack = load_genre_pack(pack_dir)
        if pack.rules.ruleset == "dial":
            continue
        found_any = True
        for world_slug in pack.worlds:
            bestiary, source = pack.effective_bestiary(world_slug)
            if bestiary is None:
                failures.append(f"{slug}/{world_slug} (ruleset: {pack.rules.ruleset})")
            else:
                assert bestiary.entries, (
                    f"{slug}/{world_slug} resolves an empty bestiary (source={source})"
                )
    if not found_any:
        pytest.skip("no ruleset-module packs on disk")
    assert not failures, (
        f"ruleset-module worlds resolve no bestiary: {failures} — the seam is "
        "world-over-genre (worlds/<slug>/bestiary.yaml or the genre tier)"
    )


def test_heavy_metal_evropi_ships_wellformed_world_bestiary() -> None:
    """heavy_metal (ruleset: wwn) ships a well-formed ``worlds/evropi/
    bestiary.yaml`` with the agreed combat-layer fields and WWN-sane numbers."""
    try:
        pack_dir = find_pack_path("heavy_metal")
    except PackNotFound:
        pytest.skip("heavy_metal not on disk")
    bestiary_path = pack_dir / "worlds" / "evropi" / "bestiary.yaml"
    assert bestiary_path.is_file(), (
        "ruleset-module worlds REQUIRE a bestiary (90-1 fail-loud contract); "
        f"missing at {bestiary_path}"
    )
    data = yaml.safe_load(bestiary_path.read_text(encoding="utf-8"))
    entries = data.get("entries") if isinstance(data, dict) else None
    assert entries, "bestiary.yaml must define a non-empty top-level `entries:` list"
    for entry in entries:
        missing = BESTIARY_ENTRY_REQUIRED_FIELDS - set(entry)
        assert not missing, (
            f"bestiary entry {entry.get('id', '?')!r} missing fields: {sorted(missing)}"
        )
        assert 5 <= entry["armor_class"] <= 22, (
            f"WWN-sane armor_class expected, got {entry['armor_class']!r} for {entry.get('id')!r}"
        )
        assert isinstance(entry["attack_bonus"], int)
        assert isinstance(entry["hp"], int) and entry["hp"] > 0


def test_barsoom_world_bestiary_wins_over_genre() -> None:
    """barsoom ships its own canonical (Burroughs) roster, so the WORLD tier must
    win — the world-over-genre rule keeps Martian fauna unpolluted by generic
    genre creatures."""
    try:
        pack_dir = find_pack_path("heavy_metal")
    except PackNotFound:
        pytest.skip("heavy_metal not on disk")
    pack = load_genre_pack(pack_dir)
    assert pack.rules.ruleset != "dial", "precondition: heavy_metal is a ruleset-module pack"
    assert "barsoom" in pack.worlds, "precondition: barsoom world present"

    bestiary, source = pack.effective_bestiary("barsoom")
    assert source == "world", "barsoom ships its own bestiary → world tier must win"
    assert bestiary is not None and bestiary.entries
