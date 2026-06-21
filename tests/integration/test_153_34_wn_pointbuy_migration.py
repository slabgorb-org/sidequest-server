"""Story 153-34 (scope item 1) — the four WN point-buy packs must migrate their
DEAD ``stat_generation: point_buy`` + ``point_buy_budget`` config to an honest
``stat_generation: standard_array`` authoring the WN SRD spread.

FINDING (153-4 review): under a WN binding the player never reaches a point-buy
surface, so ``WithoutNumberRulesetModule._generate_attribute_values`` silently
supersedes ``point_buy`` with the shaped ``[14, 12, 11, 10, 9, 7]`` spread. The
``point_buy_budget: 27`` the four packs author is therefore DEAD config — the
authoring surface lies (an author tuning the budget sees no effect). Migrate the
four WN packs to author what actually happens.

In scope (WN-family — migrate): space_opera (swn), mutant_wasteland (awn),
neon_dystopia (cwn), road_warrior (cwn).
OUT of scope (Fate — leave untouched): spaghetti_western. A bare ``grep
point_buy`` would sweep it in, but it binds ``ruleset: fate`` (ADR-144) and does
not use ``stat_generation`` at all.

These are content-data + wiring assertions (the migrated config IS the
deliverable). They load real packs from ``sidequest-content`` and skip cleanly
when content is not on disk.

RED until the four packs are migrated.
"""

from __future__ import annotations

import random

import pytest
import yaml

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# The four in-scope WN packs and their bound ruleset (sanity-asserted below).
_WN_POINTBUY_PACKS = {
    "space_opera": "swn",
    "mutant_wasteland": "awn",
    "neon_dystopia": "cwn",
    "road_warrior": "cwn",
}

# The WN SRD standard array the migration must author.
_WN_SHAPED_SPREAD = [14, 12, 11, 10, 9, 7]


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_rules_yaml(pack_slug: str) -> dict:
    """Parse a pack's rules.yaml with the SafeLoader (lang-review #8 — never
    yaml.load untrusted input)."""
    try:
        path = find_pack_path(pack_slug) / "rules.yaml"
    except PackNotFound:
        pytest.skip(f"{pack_slug} not on disk in this checkout")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# AC-1: the four WN packs author standard_array, not dead point_buy.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("pack_slug,expected_ruleset", sorted(_WN_POINTBUY_PACKS.items()))
def test_wn_pack_migrated_to_standard_array(pack_slug: str, expected_ruleset: str) -> None:
    """Each in-scope WN pack authors ``stat_generation: standard_array`` with the
    WN SRD spread, and the dead ``point_buy_budget`` field is gone.

    RED today: every one of these packs authors ``stat_generation: point_buy`` +
    ``point_buy_budget: 27``."""
    data = _load_rules_yaml(pack_slug)

    assert data.get("ruleset") == expected_ruleset, (
        f"{pack_slug} sanity: expected a WN-family ruleset {expected_ruleset!r}; "
        f"got {data.get('ruleset')!r}"
    )
    assert data.get("stat_generation") == "standard_array", (
        f"{pack_slug} must author stat_generation: standard_array (dead point_buy "
        f"superseded by the shaped spread); got {data.get('stat_generation')!r}"
    )
    assert data.get("standard_array") == _WN_SHAPED_SPREAD, (
        f"{pack_slug} must author the WN SRD spread {_WN_SHAPED_SPREAD}; "
        f"got {data.get('standard_array')!r}"
    )
    assert "point_buy_budget" not in data, (
        f"{pack_slug} must drop the now-dead point_buy_budget field "
        f"(it does nothing under a WN binding); still present = {data.get('point_buy_budget')!r}"
    )


# ---------------------------------------------------------------------------
# Guard: spaghetti_western (Fate) is NOT swept into the migration.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_spaghetti_western_fate_pack_left_untouched() -> None:
    """spaghetti_western binds Fate (ADR-144), not a WN ruleset. It must NOT be
    migrated to the WN ``standard_array`` spread — a bare ``grep point_buy`` would
    sweep it in, so this is the explicit out-of-scope tripwire."""
    data = _load_rules_yaml("spaghetti_western")

    assert data.get("ruleset") == "fate", (
        f"spaghetti_western must remain a Fate pack; got ruleset={data.get('ruleset')!r}"
    )
    assert data.get("standard_array") != _WN_SHAPED_SPREAD, (
        "spaghetti_western (Fate) must NOT be given the WN SRD spread — it was "
        "wrongly swept into the WN migration"
    )
    assert data.get("stat_generation") != "standard_array", (
        "spaghetti_western (Fate) does not use d20/WN stat_generation; it must not "
        "be migrated to standard_array"
    )


# ---------------------------------------------------------------------------
# AC (no-regression wiring): a migrated pack loads through the REAL loader and
# its standard_array config still yields the shaped WN spread at chargen.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.timeout(120)
def test_migrated_pack_loads_and_yields_shaped_spread() -> None:
    """WIRING: load the real space_opera pack through ``load_genre_pack`` and
    prove the migrated ``standard_array`` config (a) survives strict pack
    validation and (b) still produces the shaped ``[14, 12, 11, 10, 9, 7]`` pool
    through the production ruleset module — i.e. the migration is honest config,
    not a chargen regression.

    RED today: pack.rules.stat_generation is still ``point_buy``."""
    from sidequest.game.builder import AccumulatedChoices
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.genre.loader import load_genre_pack

    try:
        pack = load_genre_pack(find_pack_path("space_opera"))
    except PackNotFound:
        pytest.skip("space_opera not on disk in this checkout")

    rules = pack.rules
    assert rules.stat_generation == "standard_array", (
        f"loaded space_opera must carry the migrated stat_generation; got {rules.stat_generation!r}"
    )
    assert rules.standard_array == _WN_SHAPED_SPREAD, (
        f"loaded space_opera must carry the WN spread; got {rules.standard_array!r}"
    )
    assert rules.point_buy_budget == 0, (
        f"the dead point_buy_budget must be gone (defaults to 0); got {rules.point_buy_budget!r}"
    )

    pool = get_ruleset_module(rules.ruleset).generate_attributes(
        method=rules.stat_generation,
        ability_names=rules.ability_score_names,
        standard_array=rules.standard_array,
        point_buy_budget=rules.point_buy_budget,
        rolled_stats=None,
        acc=AccumulatedChoices(),
        rng=random.Random(1),
        class_def=None,
    )
    assert sorted(pool.values(), reverse=True) == _WN_SHAPED_SPREAD, (
        f"migrated space_opera chargen must still yield the shaped WN spread "
        f"{_WN_SHAPED_SPREAD}; got {sorted(pool.values(), reverse=True)}"
    )
