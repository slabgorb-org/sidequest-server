"""Story 158-55 (defect 2) — the WWN test-fixture pack must resolve TIER-CORRECT
encounters so chargen-wiring tests seed for real instead of leaning on the
``_isolate_monster_manuals`` tolerance swallow (a live No-Silent-Fallbacks
violation that masks a fixture gap).

``seed_manual`` generates one encounter at each of ``ENCOUNTER_TIERS = (1, 2)``
(``pregen.py``), drawing enemies from the world ``bestiary.yaml`` filtered to that
tier's level band (``tier_to_level_range``: 1 -> [1, 3], 2 -> [4, 6]). If a tier's
band is unpopulated, encountergen silently FULL-POOL-falls-back to the whole
bestiary (encountergen.py:376) — so a bestiary of only tier-1 creatures yields a
*bogus* tier-2 encounter stocked with tier-1 creatures, and a mere
``assert tier1`` (non-emptiness) cannot tell a working tier filter from a broken
one. This test therefore asserts the tier->level CONTRACT on both tiers, which
requires the fixture to carry a real tier-2 creature.

Modeled on tests/server/dispatch/test_pregen_bestiary_90_1.py — real pack -> real
``seed_manual`` (module-direct import, so the ``_isolate_monster_manuals``
attribute patch does NOT intercept it) -> tier-correct encounter pools, asserted
on behavior.

NOTE ON DETERMINISM: ``seed_manual`` does NOT thread its ``rng`` into encounter
generation — ``_generate_encounter`` shells out to ``encountergen_main``, which
mints its own ``random.Random()`` (encountergen.py:785). WHICH creatures land in
an encounter is therefore not reproducible from here; the assertions below hold
for ANY valid, tier-correct selection, so no seed is passed (passing one would
imply a reproducibility that does not exist).
"""

from __future__ import annotations

from pathlib import Path

from sidequest.cli.encountergen.encountergen import tier_to_level_range
from sidequest.game.monster_manual import MonsterManual
from sidequest.server.dispatch.pregen import ENCOUNTER_TIERS, seed_manual

FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "packs"

# The hermetic WWN world the chargen-wiring tests must bind. Its bestiary spans
# both seed tiers (level-1/2 crossroads creatures + a level-5 warlord).
_GENRE = "wwn_test_pack"
_WORLD = "flickering_reach"


def _enemy_levels(encounter) -> list[int]:  # noqa: ANN001 — Encounter (avoid import churn)
    """The integer ``level`` of every enemy in a seeded encounter block."""
    enemies = encounter.data.get("enemies") or []
    return [e["level"] for e in enemies if isinstance(e, dict) and "level" in e]


def test_wwn_fixture_seeds_tier_correct_encounters() -> None:
    """Seeding the WWN fixture's ``flickering_reach`` world yields, for EACH seed
    tier, a non-empty encounter whose every enemy's level falls inside that tier's
    level band — through the REAL seed path (no EncounterSeedError, no tolerance
    swallow, no full-pool fallback).

    RED history: before this rework the fixture carried only tier-1 creatures, so
    the tier-2 seed full-pool-fell-back to level-1/2 enemies and this test's tier-2
    band assertion failed; the fixture now ships a level-5 warlord for tier 2."""
    manual = MonsterManual(genre=_GENRE, world=_WORLD)
    seed_manual(
        genre_packs_path=FIXTURE_PACKS_DIR,
        genre=_GENRE,
        world=_WORLD,
        manual=manual,
    )

    for tier in ENCOUNTER_TIERS:
        band_min, band_max = tier_to_level_range(tier)
        encounters = [e for e in manual.encounters if e.tier == tier]
        assert encounters, (
            f"{_GENRE}/{_WORLD} resolved no tier-{tier} encounter — the fixture "
            f"world must ship a worlds/{_WORLD}/bestiary.yaml with a stat block in "
            f"level band {band_min}-{band_max} so chargen-wiring tests seed for real "
            "instead of relying on the _isolate_monster_manuals tolerance swallow "
            "(story 158-55 defect 2)"
        )
        for encounter in encounters:
            levels = _enemy_levels(encounter)
            assert levels, f"tier-{tier} encounter carried no enemy level data"
            offenders = [lvl for lvl in levels if not band_min <= lvl <= band_max]
            assert not offenders, (
                f"tier-{tier} encounter drew enemy level(s) {offenders} outside the "
                f"tier's {band_min}-{band_max} band — the level filter is broken or "
                "full-pool-fell-back over an unpopulated tier (encountergen.py:376), "
                "which is exactly the silent fixture gap this test guards"
            )
