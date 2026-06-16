"""RED (story 114-10): the Fate-gear content-validator rules (``sidequest-validate``).

The design's validator section names four checks the content validator must enforce
so an author cannot ship a paradigm-mismatched or unbalanced Fate pack:

  1. **No inventory under fate** — a ``ruleset: fate`` pack shipping ``inventory.yaml``
     is a hard error (No Silent Fallbacks; the loader fabricates nothing).
  2. **Refresh invariant** — every archetype's authored ``refresh`` must equal
     ``base_refresh − max(0, total_stunts − free_stunts)`` (the only balance story).
  3. **Dangling gear id** — no archetype references a gear ``id`` absent from the
     genre-tier GearDef set.
  4. **Permission is never a gate** — no resolver path refuses an action for a missing
     permission aspect (The Zork Problem). A permission aspect is a normal invokable
     aspect, tested behaviorally below.

Checks 1-3 are pinned as PURE functions (unambiguous, refactor-stable) Dev wires into
the CLI validator; the integration test (``test_114_10_four_pack_fate_gear.py``) proves
the wiring by running the real validator on the migrated packs. The imports fail in RED.
"""

from __future__ import annotations

# NEW in 114-10 — fail in RED until Dev adds the validator module.
from sidequest.cli.validate.fate_gear import (
    check_dangling_gear_ids,
    check_no_inventory_under_fate,
    check_refresh_invariant,
)
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset.fate import FateRulesetModule

# ---------------------------------------------------------------------------
# Check 1 — no inventory.yaml under a fate pack
# ---------------------------------------------------------------------------


class TestNoInventoryUnderFate:
    def test_fate_pack_with_inventory_is_an_error(self) -> None:
        err = check_no_inventory_under_fate(ruleset="fate", has_inventory=True)
        assert err is not None, "a ruleset: fate pack shipping inventory.yaml must error"
        assert "inventory" in err.lower()

    def test_fate_pack_without_inventory_is_clean(self) -> None:
        assert check_no_inventory_under_fate(ruleset="fate", has_inventory=False) is None

    def test_non_fate_pack_with_inventory_is_clean(self) -> None:
        # WN/native packs legitimately ship inventory.yaml — the check is
        # fate-only and must not flag them.
        assert check_no_inventory_under_fate(ruleset="swn", has_inventory=True) is None


# ---------------------------------------------------------------------------
# Check 2 — the refresh invariant
# ---------------------------------------------------------------------------


class TestRefreshInvariant:
    def test_balanced_archetype_passes(self) -> None:
        # 1 stunt over the free allotment → refresh should be base(3) − 1 = 2.
        err = check_refresh_invariant(
            archetype="The Tinkerer",
            authored_refresh=2,
            base_refresh=3,
            free_stunts=3,
            total_stunts=4,
        )
        assert err is None

    def test_aspect_only_archetype_at_base_refresh_passes(self) -> None:
        err = check_refresh_invariant(
            archetype="The Gumshoe",
            authored_refresh=3,
            base_refresh=3,
            free_stunts=3,
            total_stunts=2,
        )
        assert err is None

    def test_unpaid_stunt_gear_is_flagged(self) -> None:
        # 4 stunts, free 3 → invariant says refresh 2, but the author kept it at
        # 3 (a "free" stunt-item) — the forbidden re-balance. Must flag, naming
        # the offending archetype.
        err = check_refresh_invariant(
            archetype="The Tinkerer",
            authored_refresh=3,
            base_refresh=3,
            free_stunts=3,
            total_stunts=4,
        )
        assert err is not None
        assert "The Tinkerer" in err, "the error must name the offending archetype"

    def test_overpaid_refresh_is_also_flagged(self) -> None:
        # The invariant is equality, not an upper bound — an under-refresh
        # (author charged themselves too much) is also a content error.
        err = check_refresh_invariant(
            archetype="The Ascetic",
            authored_refresh=1,
            base_refresh=3,
            free_stunts=3,
            total_stunts=2,
        )
        assert err is not None
        assert "The Ascetic" in err, (
            "the error must name the offending archetype (parity with the unpaid case)"
        )

    def test_srd_floor_saves_a_pathological_overrun(self) -> None:
        # The boundary case the floor exists for: 10 stunts, 3 free → a raw
        # invariant of 3 − 7 = −4, but the SRD floor clamps the requirement to 1.
        # An author who set refresh=1 here is BALANCED (the floor, not the raw
        # formula, is what they must match) — the check must NOT flag them.
        err = check_refresh_invariant(
            archetype="The Overburdened",
            authored_refresh=1,
            base_refresh=3,
            free_stunts=3,
            total_stunts=10,
        )
        assert err is None, "the SRD floor (1) must be the accepted value for a deep overrun"


# ---------------------------------------------------------------------------
# Check 3 — dangling gear ids
# ---------------------------------------------------------------------------


class TestDanglingGearIds:
    def test_all_ids_present_is_clean(self) -> None:
        errs = check_dangling_gear_ids(
            archetype="The Gumshoe",
            referenced_ids=["noir_fedora", "noir_license"],
            available_ids={"noir_fedora", "noir_license", "noir_lighter"},
        )
        assert errs == []

    def test_missing_id_is_flagged(self) -> None:
        errs = check_dangling_gear_ids(
            archetype="The Gumshoe",
            referenced_ids=["noir_fedora", "ghost_gun"],
            available_ids={"noir_fedora"},
        )
        assert len(errs) == 1
        assert "ghost_gun" in errs[0]
        assert "The Gumshoe" in errs[0]


# ---------------------------------------------------------------------------
# Check 4 — permission is never an engine gate (The Zork Problem, behavioral)
# ---------------------------------------------------------------------------


class TestPermissionIsNeverAGate:
    def test_permission_aspect_is_a_normal_invokable_aspect(self) -> None:
        # P-i: a permission aspect rides Fate's aspect economy like any other —
        # invokable for +2. There is NO special "permission" code path that
        # gates or refuses; the engine treats it as an ordinary aspect. If a
        # permission aspect could NOT be invoked, the engine would be reading
        # kind=="permission" specially — exactly the gate the design forbids.
        module = FateRulesetModule()
        sheet = FateSheet(
            aspects=[Aspect(text="Licensed Investigator", kind="permission")],
            refresh=3,
            fate_points=3,
        )
        bonus = module.invoke_aspect(sheet=sheet, aspect_text="Licensed Investigator", mode="bonus")
        assert bonus == 2, "a permission aspect must invoke for +2 like any aspect (never gated)"
        assert sheet.fate_points == 2, "invoking it spends a fate point — normal economy, no gate"
