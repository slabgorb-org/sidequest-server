"""Genre pack calibration assertions for ADR-093.

Loads each shipped genre pack's rules.yaml and verifies the calibrated v1
state. Filters on **resolution_mode** rather than type name — the ADR's
"combat & chase" framing missed that space_opera's ship_combat also uses
``resolution_mode: opposed_check`` (its category is "combat"). The
resolution-mode filter is the invariant: anything resolved through the
calibrated tie-band geometry must use the calibrated threshold.

1. opponent_default_stats — no value equals 12 (the pre-calibration parity
   number). All present values must be 10 or below.
2. Every ``resolution_mode: opposed_check`` confrontation has both
   player_metric.threshold and opponent_metric.threshold equal to 7
   (lowered from 10). Covers combat across all packs AND space_opera's
   ship_combat. See TEA deviation log on session 45-41 for why this
   broadens AC-3's literal "combat and chase" reading.
3. Every ``resolution_mode: sealed_letter_lookup`` confrontation keeps its
   pre-calibration threshold (currently space_opera's dogfight at 30) —
   sealed-letter recalibration is v2 territory.
4. Negotiation — the v1 calibration explicitly does NOT touch negotiation
   thresholds. This test asserts no negotiation threshold collapses below
   5 by accident (would over-shorten social scenes).

Pack list covers the four packs calibrated by ADR-093 (caverns_and_claudes,
elemental_harmony, mutant_wasteland, space_opera) plus tea_and_murder.
Tea & Murder is included because it is social-only by design (no opposed_check
confrontations) — its parametrize rows pass trivially today, but inclusion
ensures any future addition of an opposed_check confrontation to tea_and_murder
gets caught automatically. The COMBAT_PACKS list (below) is the per-pack
wiring guard's stricter set, excluding tea_and_murder.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GENRE_PACKS_DIR = REPO_ROOT / "sidequest-content" / "genre_packs"

# Packs that ship rules.yaml today. Keeping this list explicit (rather than
# globbing) so a stray work-in-progress pack appearing in the directory
# can't silently bypass calibration checks.
SHIPPED_PACKS = [
    "caverns_and_claudes",
    "elemental_harmony",
    "mutant_wasteland",
    "space_opera",
    "tea_and_murder",
]

# Packs that MUST expose at least one opposed_check confrontation. tea_and_murder is
# excluded because it is social-only by design (negotiation, trial, auction,
# social_duel, scandal — all beat_selection mode). The per-pack wiring test
# enforces that each combat pack still has at least one opposed_check
# confrontation; without this, the parametrized calibration tests would pass
# vacuously for any pack whose combat confrontations were accidentally
# removed.
COMBAT_PACKS = [
    "caverns_and_claudes",
    "elemental_harmony",
    "mutant_wasteland",
    "space_opera",
]

CALIBRATED_THRESHOLD = 7
SEALED_LETTER_THRESHOLD = 30
PRE_CALIBRATION_PARITY_STAT = 12
CALIBRATED_OPPONENT_STAT_CEILING = 10

# Resolution modes drive the calibration filter. opposed_check shares the
# calibrated tie band → threshold 7. sealed_letter_lookup is a different
# resolution algorithm → kept at its pre-calibration value (30 for the only
# current entry, dogfight; recalibration deferred to v2).
OPPOSED_CHECK_MODE = "opposed_check"
SEALED_LETTER_MODE = "sealed_letter_lookup"


def _load_rules_yaml(pack_name: str) -> dict:
    """Load and parse <pack>/rules.yaml. Skip if the pack is absent on
    disk (CI runs without sidequest-content checked out)."""
    rules_path = GENRE_PACKS_DIR / pack_name / "rules.yaml"
    if not rules_path.exists():
        pytest.skip(f"Genre pack '{pack_name}' not present at {rules_path}")
    with rules_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.parametrize("pack_name", SHIPPED_PACKS)
def test_opponent_default_stats_no_parity_12_remains(pack_name: str):
    """ADR-093 AC-1: every opponent_default_stats value across the pack
    must be lowered from 12. The post-calibration ceiling is 10."""
    rules = _load_rules_yaml(pack_name)
    confrontations = rules.get("confrontations", [])

    offending: list[tuple[str, str, int]] = []
    for cdef in confrontations:
        ctype = cdef.get("type", "<unknown>")
        ods = cdef.get("opponent_default_stats")
        if not ods:
            continue
        for stat_name, value in ods.items():
            if not isinstance(value, int):
                continue
            if value == PRE_CALIBRATION_PARITY_STAT or value > CALIBRATED_OPPONENT_STAT_CEILING:
                offending.append((ctype, stat_name, value))

    assert not offending, (
        f"Pack '{pack_name}' has un-calibrated opponent_default_stats "
        f"entries (must be ≤ {CALIBRATED_OPPONENT_STAT_CEILING}): {offending}"
    )


@pytest.mark.parametrize("pack_name", SHIPPED_PACKS)
def test_opposed_check_thresholds_calibrated_to_7(pack_name: str):
    """ADR-093 AC-3: every confrontation using ``resolution_mode:
    opposed_check`` has both player_metric.threshold and
    opponent_metric.threshold equal to 7. Filtering by resolution_mode
    rather than type name catches space_opera's ship_combat (mode is
    opposed_check, type is "ship_combat") in addition to the literal
    "combat" type across the other four packs."""
    rules = _load_rules_yaml(pack_name)
    confrontations = rules.get("confrontations", [])

    offending: list[tuple[str, str, int]] = []
    for cdef in confrontations:
        ctype = cdef.get("type", "<unknown>")
        if cdef.get("resolution_mode") != OPPOSED_CHECK_MODE:
            continue
        for side in ("player_metric", "opponent_metric"):
            metric = cdef.get(side, {})
            threshold = metric.get("threshold")
            if threshold != CALIBRATED_THRESHOLD:
                offending.append((ctype, side, threshold))

    assert not offending, (
        f"Pack '{pack_name}' has opposed_check confrontations with "
        f"thresholds != {CALIBRATED_THRESHOLD}: {offending}"
    )


@pytest.mark.parametrize("pack_name", SHIPPED_PACKS)
def test_sealed_letter_thresholds_unchanged(pack_name: str):
    """ADR-093 explicitly excludes sealed-letter confrontations from v1
    calibration. Their thresholds must stay at 30 (currently the only
    entry is space_opera dogfight) — recalibration is deferred to v2."""
    rules = _load_rules_yaml(pack_name)
    confrontations = rules.get("confrontations", [])

    offending: list[tuple[str, str, int]] = []
    for cdef in confrontations:
        ctype = cdef.get("type", "<unknown>")
        if cdef.get("resolution_mode") != SEALED_LETTER_MODE:
            continue
        for side in ("player_metric", "opponent_metric"):
            metric = cdef.get(side, {})
            threshold = metric.get("threshold")
            if threshold != SEALED_LETTER_THRESHOLD:
                offending.append((ctype, side, threshold))

    assert not offending, (
        f"Pack '{pack_name}' has sealed_letter_lookup thresholds != "
        f"{SEALED_LETTER_THRESHOLD} (v2 territory): {offending}"
    )


@pytest.mark.parametrize("pack_name", SHIPPED_PACKS)
def test_negotiation_thresholds_not_collapsed_below_5(pack_name: str):
    """ADR-093 explicitly leaves negotiation thresholds untouched. A v1
    edit that accidentally drops a negotiation threshold below 5 would
    over-shorten social scenes; this test catches that drift."""
    rules = _load_rules_yaml(pack_name)
    confrontations = rules.get("confrontations", [])

    offending: list[tuple[str, str, int]] = []
    for cdef in confrontations:
        ctype = cdef.get("type", "<unknown>")
        if ctype != "negotiation":
            continue
        for side in ("player_metric", "opponent_metric"):
            metric = cdef.get(side, {})
            threshold = metric.get("threshold")
            if not isinstance(threshold, int):
                continue
            if threshold < 5:
                offending.append((ctype, side, threshold))

    assert not offending, (
        f"Pack '{pack_name}' negotiation thresholds dropped below 5 "
        f"(v1 should not touch negotiation): {offending}"
    )


@pytest.mark.parametrize("pack_name", COMBAT_PACKS)
def test_combat_pack_exposes_at_least_one_opposed_check_confrontation(pack_name: str):
    """Per-pack wiring guard: every COMBAT_PACKS entry must expose at least
    one ``resolution_mode: opposed_check`` confrontation. Without this, a
    pack whose combat confrontation was accidentally deleted would still
    pass test_opposed_check_thresholds_calibrated_to_7 vacuously (empty
    `offending` list because no opposed_check entries to check).

    Excludes tea_and_murder deliberately — it is social-only by design and has
    no opposed_check confrontations. See COMBAT_PACKS comment.
    """
    rules = _load_rules_yaml(pack_name)
    confrontations = rules.get("confrontations", [])
    has_opposed_check = any(
        cdef.get("resolution_mode") == OPPOSED_CHECK_MODE for cdef in confrontations
    )
    assert has_opposed_check, (
        f"Pack '{pack_name}' has no opposed_check confrontation — the "
        f"calibration tests for this pack would pass vacuously. Combat "
        f"pack list: {COMBAT_PACKS}"
    )
