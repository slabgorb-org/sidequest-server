"""mutant_wasteland → AWN binding + standard-six sweep (Story 88-2, design §6.2/§6.3).

RED-phase content gates for Plan-1 Story B (the CONTENT half of the AWN port).
Story 88-1 (engine) already landed the `awn` ruleset module, `AwnConfig`, and the
`_validate_awn` validator (PR #682). This story binds the REAL `mutant_wasteland`
pack to `ruleset: awn`, adopts the standard six (STR/DEX/CON/INT/WIS/CHA) in place
of the flavor names (Brawn/Reflexes/Toughness/Wits/Instinct/Presence), migrates the
"Wasteland Brawl" combat confrontation from momentum/`opposed_check` to
`win_condition: hp_depletion`, and retires the `magic_level` flag.

Why these load the REAL pack (not a synthetic fixture): per the project's wiring
doctrine, a validator PASS is not proof a pack loads, and `_validate_awn` only fires
through `load_genre_pack`. These tests catch the failures a synthetic RulesConfig
cannot — a dead stat name left in `archetypes.yaml`, an incomplete attribute_map, a
combat confrontation that never migrated.

All RED until Story 88-2 content lands:
  * the pack runs the `native` engine today (no `ruleset:` line) → awn assertions fail
  * `ability_score_names` are the flavor six → standard-six assertions fail
  * "Wasteland Brawl" is `resolution_mode: opposed_check` / momentum → hp_depletion fails
  * confrontation `stat_check`s and archetype `stat_ranges` use flavor names → sweep fails
  * `magic_level: none` is still present in rules.yaml → retire assertion fails

Skips (not fails) when sidequest-content is not checked out, matching the
road_warrior / calibration-suite environment-guard convention.
"""

from __future__ import annotations

import pytest
import yaml

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from tests._helpers.genre_paths import PackNotFound, find_pack_path

# The standard six, as they must appear in `ability_score_names` and as the VALUES
# of the awn attribute_map (AWN natively uses the standard six — design D4).
STANDARD_SIX = {"STR", "DEX", "CON", "INT", "WIS", "CHA"}

# Canonical AWN/CWN attribute_map KEYS (full words) — what `_validate_awn` requires.
CANONICAL_SIX = {
    "STRENGTH",
    "DEXTERITY",
    "CONSTITUTION",
    "INTELLIGENCE",
    "WISDOM",
    "CHARISMA",
}

# The flavor names being retired (design §6.3). Any of these surviving as a
# *mechanical* key (stat_check / stat_ranges / opponent_default_stats) is a dead
# reference the narrator will read off a sheet that no longer has that stat.
RETIRED_FLAVOR_NAMES = {
    "Brawn",
    "Reflexes",
    "Toughness",
    "Wits",
    "Instinct",
    "Presence",
}

_PACK_SLUG = "mutant_wasteland"


def _load_pack() -> GenrePack:
    try:
        return load_genre_pack(find_pack_path(_PACK_SLUG))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _load_raw_rules() -> dict:
    try:
        rules_path = find_pack_path(_PACK_SLUG) / "rules.yaml"
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))
    with rules_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_raw_yaml(filename: str) -> object:
    try:
        path = find_pack_path(_PACK_SLUG) / filename
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _combat_confrontation(pack: GenrePack):
    combat = next(
        (c for c in pack.rules.confrontations if c.category == "combat"),
        None,
    )
    assert combat is not None, (
        "mutant_wasteland must declare a combat-category confrontation "
        "(the migrated 'Wasteland Brawl')"
    )
    return combat


# ─────────────────────────── binding gates ────────────────────────────


def test_mutant_wasteland_loads_and_binds_awn() -> None:
    """The REAL pack loads through `load_genre_pack` (not just `validate`) and is
    bound to `ruleset: awn` with a non-None `AwnConfig`. This is the validator≠loader
    gate: `_validate_awn` only fires on a real load."""
    pack = _load_pack()
    assert pack.rules.ruleset == "awn", (
        "mutant_wasteland must bind `ruleset: awn` (design §6.2); "
        f"got ruleset={pack.rules.ruleset!r}"
    )
    assert pack.rules.awn is not None, (
        "an awn-bound pack must carry an `awn:` config block; rules.awn is None"
    )


def test_mutant_wasteland_ability_score_names_are_standard_six() -> None:
    """The sheet declares the standard six, not the flavor six (design D4/§6.2)."""
    pack = _load_pack()
    names = set(pack.rules.ability_score_names)
    assert names == STANDARD_SIX, (
        "ability_score_names must be the standard six STR/DEX/CON/INT/WIS/CHA "
        f"(flavor names retired per §6.3); got {sorted(names)}"
    )


def test_mutant_wasteland_awn_attribute_map_complete_standard_six() -> None:
    """`awn.attribute_map` carries all six canonical keys mapped to the standard six,
    and the System-Strain max_source is a key of the map (the `_validate_awn`
    contract + the documented 'attribute_map must be COMPLETE' gotcha)."""
    pack = _load_pack()
    awn = pack.rules.awn
    assert awn is not None, "rules.awn must be present (see binding test)"
    amap = awn.attribute_map
    assert set(amap.keys()) == CANONICAL_SIX, (
        f"awn.attribute_map must key on all six canonical attributes; got {sorted(amap.keys())}"
    )
    assert set(amap.values()) == STANDARD_SIX, (
        "awn.attribute_map must map canonical → the standard-six flavor labels; "
        f"got values {sorted(amap.values())}"
    )
    strain_source = awn.system_strain.max_source
    assert strain_source in amap, (
        f"awn.system_strain.max_source={strain_source!r} must be a key of "
        f"attribute_map {sorted(amap.keys())} (System Strain pool = CON)"
    )


# ─────────────────────────── combat migration ─────────────────────────


def test_mutant_wasteland_combat_is_hp_depletion() -> None:
    """'Wasteland Brawl' moves off momentum/`opposed_check` to `win_condition:
    hp_depletion` — real ablative HP under the strike beats (design §6.2). This is
    the by-design break that drops mutant_wasteland from the dial COMBAT_PACKS set."""
    pack = _load_pack()
    combat = _combat_confrontation(pack)
    win = (
        combat.win_condition.value
        if hasattr(combat.win_condition, "value")
        else str(combat.win_condition)
    )
    mode = (
        combat.resolution_mode.value
        if hasattr(combat.resolution_mode, "value")
        else str(combat.resolution_mode)
    )
    assert win == "hp_depletion", (
        f"combat confrontation {combat.label!r} must use win_condition 'hp_depletion'; got {win!r}"
    )
    assert mode != "opposed_check", (
        "an hp_depletion combat must not also be a dial opposed_check confrontation "
        f"(it would be re-swept into the ADR-093 threshold calibration); got mode {mode!r}"
    )


def test_mutant_wasteland_combat_opponent_stats_have_all_six_plus_hp_ac() -> None:
    """The hp_depletion combat seats a real opponent: all six ability scores (for
    saves) PLUS the reserved `hp` and `armor_class` seed keys (the documented
    'needs ALL SIX for saves' gotcha; the pydantic model only enforces hp/ac/dex)."""
    pack = _load_pack()
    combat = _combat_confrontation(pack)
    ods = combat.opponent_default_stats or {}
    missing_scores = STANDARD_SIX - ods.keys()
    assert not missing_scores, (
        "hp_depletion combat opponent_default_stats must author all six standard "
        f"ability scores for save resolution; missing {sorted(missing_scores)} "
        f"(present: {sorted(ods.keys())})"
    )
    for reserved in ("hp", "armor_class"):
        assert reserved in ods, (
            f"hp_depletion combat opponent_default_stats must seat {reserved!r}; "
            f"present: {sorted(ods.keys())}"
        )


def test_mutant_wasteland_social_and_movement_stay_dial() -> None:
    """Parley (negotiation) and Pursuit (chase) are NOT combat and must stay dial
    confrontations — guard against an over-eager migration sweeping non-combat
    scenes into hp_depletion (design §6.2: 'Keep ... as dial confrontations')."""
    pack = _load_pack()
    for category in ("social", "movement"):
        confs = [c for c in pack.rules.confrontations if c.category == category]
        for c in confs:
            win = (
                c.win_condition.value if hasattr(c.win_condition, "value") else str(c.win_condition)
            )
            assert win != "hp_depletion", (
                f"{category} confrontation {c.label!r} must remain a dial "
                f"confrontation, not hp_depletion; got win_condition {win!r}"
            )


# ─────────────────────────── standard-six sweep ───────────────────────


def test_mutant_wasteland_all_confrontation_stat_checks_standard_six() -> None:
    """Every confrontation beat's `stat_check` references a standard-six attribute —
    no retired flavor name survives in rules.yaml (design §6.3). A dead stat_check
    is a mechanical reference to a stat the sheet no longer has."""
    pack = _load_pack()
    offending: list[tuple[str, str, str]] = []
    for c in pack.rules.confrontations:
        for beat in c.beats:
            sc = beat.stat_check
            if sc and sc not in STANDARD_SIX:
                offending.append((c.label, beat.label, sc))
    assert not offending, (
        "every confrontation beat stat_check must use a standard-six attribute; "
        f"found retired/dead stat names (label, beat, stat_check): {offending}"
    )


def test_mutant_wasteland_archetype_stat_ranges_standard_six() -> None:
    """Every `archetypes.yaml` `stat_ranges` key is a standard-six attribute.

    NpcArchetype is `extra='allow'` and the loader does NOT cross-check stat_ranges
    keys against ability_score_names — so a half-finished sweep leaves Wits/Presence
    in the archetype stat blocks and the pack still loads clean. This is exactly the
    silent dead-stat-name failure design §10 warns about; assert it explicitly."""
    raw = _load_raw_yaml("archetypes.yaml")
    assert isinstance(raw, list), "archetypes.yaml is a list of archetype dicts"
    offending: list[tuple[str, str]] = []
    for arch in raw:
        name = arch.get("name", "<unnamed>")
        for stat_key in arch.get("stat_ranges") or {}:
            if stat_key in RETIRED_FLAVOR_NAMES or stat_key not in STANDARD_SIX:
                offending.append((name, stat_key))
    assert not offending, (
        "archetype stat_ranges must use standard-six keys (no retired flavor "
        f"names); found (archetype, dead_stat): {offending}"
    )


def test_mutant_wasteland_magic_level_retired() -> None:
    """The `magic_level` flag is retired from rules.yaml (design §6.2 + the flag's
    own DRAFT note). Mutation-as-magic framing moves to Plan 2; until then the flag
    must not imply a live magic system that isn't bound."""
    raw = _load_raw_rules()
    assert "magic_level" not in raw, (
        "the magic_level flag must be removed from mutant_wasteland/rules.yaml "
        "(retired per §6.2; mutation framing → Plan 2); still present with value "
        f"{raw.get('magic_level')!r}"
    )
