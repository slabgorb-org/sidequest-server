"""Genre pack calibration assertions for ADR-093.

Loads each shipped genre pack's rules.yaml and verifies the calibrated v1
state. Filters on **resolution_mode** rather than type name — the ADR's
"combat & chase" framing missed that opposed_check confrontations can carry
non-"combat" type names. The resolution-mode filter is the invariant: anything
resolved through the calibrated tie-band geometry must use the calibrated
threshold.

**Scoping (Phase 3 / 2026-06-17): ADR-093 covers the dial/WN family only.**

Fate packs (tea_and_murder, spaghetti_western) are scoped OUT of this suite —
they resolve via the Contest mode (``resolution_mode: contest``) and carry zero
``opposed_check`` confrontations after Phase 3. Asserting ADR-093 calibration
constraints against Contest-mode defs would be both wrong and vacuous.

The dial/WN family keeps ``opposed_check``; road_warrior (cwn) is the sole
remaining live exemplar. It has two opposed_check confrontations:
  - negotiation @ threshold 10 (by design — ADR-093 does not calibrate
    negotiation thresholds; see test_negotiation_thresholds_not_collapsed_below_5)
  - chase @ threshold 7 (the ADR-093 calibrated value)

road_warrior is therefore both the SHIPPED_PACKS member and the COMBAT_PACKS
existence tripwire: it proves the test suite is non-vacuous (the threshold-7
assertion has at least one opposed_check to check) and guards the WN→dial path
from accidental removal.

1. opponent_default_stats — no value equals 12 (the pre-calibration parity
   number). All present values must be 10 or below.
2. Every ``resolution_mode: opposed_check`` confrontation (excluding negotiation
   type, which ADR-093 explicitly leaves alone) has both
   player_metric.threshold and opponent_metric.threshold equal to 7.
3. Every ``resolution_mode: sealed_letter_lookup`` confrontation keeps its
   pre-calibration threshold (currently space_opera's dogfight at 30) —
   sealed-letter recalibration is v2 territory.
4. Negotiation — the v1 calibration explicitly does NOT touch negotiation
   thresholds. This test asserts no negotiation threshold collapses below
   5 by accident (would over-shorten social scenes).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sidequest.genre.models.rules import OPPONENT_RESERVED_STAT_KEYS

REPO_ROOT = Path(__file__).resolve().parents[3]
GENRE_PACKS_DIR = REPO_ROOT / "sidequest-content" / "genre_packs"

# Packs that ship rules.yaml today. Keeping this list explicit (rather than
# globbing) so a stray work-in-progress pack appearing in the directory
# can't silently bypass calibration checks.
SHIPPED_PACKS = [
    "caverns_and_claudes",
    "elemental_harmony",
    "mutant_wasteland",
    "road_warrior",
    "space_opera",
]

# Packs that MUST expose at least one opposed_check confrontation. This is the
# WN-regression guard: if someone later converts road_warrior's opposed_check
# defs to Contest or removes them, this test fails loudly rather than letting
# the threshold-7 assertion pass vacuously over zero defs.
#
# road_warrior (cwn) is the sole remaining dial/WN pack with opposed_check
# confrontations after Phase 3. All other shipped packs in SHIPPED_PACKS have
# migrated their combat confrontations to beat_selection + hp_depletion (WN
# family) or Contest mode (Fate family):
#   - caverns_and_claudes, elemental_harmony: WWN beat_selection + hp_depletion
#   - mutant_wasteland: AWN beat_selection + hp_depletion
#   - space_opera: SWN beat_selection + hp_depletion
# road_warrior retains two opposed_check confrontations (negotiation @10, chase
# @7) and belongs in this list as the live exemplar ensuring non-vacuous checks.
COMBAT_PACKS: list[str] = ["road_warrior"]

CALIBRATED_THRESHOLD = 7
PRE_CALIBRATION_PARITY_STAT = 12
CALIBRATED_OPPONENT_STAT_CEILING = 10

# Resolution modes drive the calibration filter. opposed_check shares the
# calibrated tie band → threshold 7. sealed_letter_lookup is a different
# resolution algorithm: under ADR-153 §2 the only sealed-letter COMBAT
# confrontation (the space_opera dogfight) resolves via SWN hp_depletion and
# carries NO native dial (158-31 removed it) — superseding ADR-093's v1
# threshold-30 deferral. See test_sealed_letter_combat_has_no_native_dial.
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
            # hp / armor_class are reserved CreatureCore-seed keys, not
            # ability scores — exempt from the ADR-093 stat ceiling.
            if stat_name in OPPONENT_RESERVED_STAT_KEYS:
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
    opposed_check`` (excluding negotiation type, which ADR-093 explicitly
    leaves alone) has both player_metric.threshold and
    opponent_metric.threshold equal to 7. Covers the dial/WN family;
    road_warrior (cwn) is the live exemplar with a chase opposed_check @ 7.
    Negotiation is skipped here — its floor is guarded by
    test_negotiation_thresholds_not_collapsed_below_5."""
    rules = _load_rules_yaml(pack_name)
    confrontations = rules.get("confrontations", [])

    offending: list[tuple[str, str, int]] = []
    for cdef in confrontations:
        ctype = cdef.get("type", "<unknown>")
        if cdef.get("resolution_mode") != OPPOSED_CHECK_MODE:
            continue
        # ADR-093 calibrates combat/chase opposed_checks to 7 but explicitly does
        # NOT touch negotiation thresholds (see test_negotiation_thresholds_*).
        # road_warrior's negotiation is opposed_check @ 10 by design (spec 2026-06-17 §5).
        if ctype == "negotiation":
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
def test_sealed_letter_combat_has_no_native_dial(pack_name: str):
    """ADR-153 §2 firewall (supersedes ADR-093's v1 threshold-30 deferral): a
    sealed_letter COMBAT confrontation resolves via SWN hp_depletion and carries
    NO native energy dial. The space_opera dogfight (the only sealed-letter
    combat) was a dial_threshold/energy-metric duel under ADR-093; 158-31 removed
    the dial. Any sealed_letter combat that declares a non-hp_depletion
    win_condition OR reintroduces player_metric/opponent_metric is the 158-31
    contradiction — also caught at validate time by validate.rules
    (SEALED_LETTER_COMBAT_NOT_HP_DEPLETION)."""
    rules = _load_rules_yaml(pack_name)
    confrontations = rules.get("confrontations", [])

    offending: list[tuple[str, str]] = []
    for cdef in confrontations:
        ctype = cdef.get("type", "<unknown>")
        if cdef.get("resolution_mode") != SEALED_LETTER_MODE:
            continue
        if cdef.get("category") != "combat":
            continue
        win_condition = cdef.get("win_condition")
        if win_condition != "hp_depletion":
            offending.append((ctype, f"win_condition={win_condition!r} (expected hp_depletion)"))
        for side in ("player_metric", "opponent_metric"):
            if cdef.get(side) is not None:
                offending.append((ctype, f"{side} present — native dial must be removed"))

    assert not offending, (
        f"Pack '{pack_name}' has a sealed_letter_lookup combat with a native dial "
        f"(ADR-153 firewall violation, 158-31): {offending}"
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
    pack whose combat confrontation was accidentally removed would still
    pass test_opposed_check_thresholds_calibrated_to_7 vacuously (empty
    `offending` list because no opposed_check entries to check).

    Currently road_warrior is the sole COMBAT_PACKS member — the only
    dial/WN pack retaining opposed_check confrontations after Phase 3.
    See COMBAT_PACKS comment for the full exclusion rationale.
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
