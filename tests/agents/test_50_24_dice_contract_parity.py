"""Story 50-24 AC-1 — SDK narrator prompt §7 (DICE RESOLUTION) contract parity.

Source: /Users/slabgorb/Projects/sq-playtest-pingpong.md, "OQ-2 ARCHITECT
RESOLUTION" bullet under the "Narrator fabricates dice" headline
(Architect/OQ-2, 2026-05-17).

Root cause (Architect-resolved, leg C): the ADR-101/102 SDK-narrator
migration replaced the engine-forced dice authority (output_only.md:89
"the engine will overwrite this from the actual roll") with
``output_only_sdk.md`` §7 — the ONLY one of the eight tool-owned
categories with no MUST/MANDATORY, no trigger enumeration, and a
self-gating clause ("When the prose hinges on an uncertain outcome the
engine should resolve") that a model which already wrote "A 16 buys
real currency" reads as *already resolved* → not required to call the
tool. The contract licensed the fabrication.

AC-1 (verbatim from the story context):
- §7 promoted to MUST/MANDATORY parity with the other seven categories
- Trigger enumeration added (matching the §4 pattern)
- The self-gating clause DELETED — it is the loophole

TEST-DESIGN NOTE (test-paranoia): the sibling 50-2 prompt tests assert
substrings against the *whole* ``NARRATOR_OUTPUT_ONLY`` blob. That is
correct for 50-2 (it hunts type-name tokens that appear nowhere else)
but would be VACUOUS here: "MUST"/"MANDATORY" appear all over this
prompt for §1-§6/§8, so a whole-document "MUST present" assertion
always passes regardless of §7. Every AC-1 assertion below therefore
operates on the *sliced §7 section text*, not the whole document.
"""

from __future__ import annotations

import pytest

from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY

# The self-gating loophole, verbatim from output_only_sdk.md:142-144 as it
# stands pre-fix. AC-1 requires this exact clause be deleted.
_SELF_GATING_LOOPHOLE = "When the prose hinges on an uncertain outcome the engine should resolve"


def _dice_section() -> str:
    """Slice the '7. DICE RESOLUTION' category out of the SDK prompt.

    The eight tool-owned categories are numbered "1." … "8.". §7 runs
    from the '7. DICE RESOLUTION' header to the '8. SCENARIO-CLUE'
    header. Asserting against this slice (not the whole doc) is what
    makes the MUST/MANDATORY assertion meaningful — the rest of the
    document is saturated with MUST language for the other categories.
    """
    text = NARRATOR_OUTPUT_ONLY
    start = text.find("7. DICE RESOLUTION")
    assert start != -1, (
        "Could not locate the '7. DICE RESOLUTION' header in "
        "NARRATOR_OUTPUT_ONLY — the section structure changed; "
        "update this slice helper."
    )
    end = text.find("8. SCENARIO-CLUE", start)
    assert end != -1, (
        "Could not locate the '8. SCENARIO-CLUE' header after §7 — "
        "the section structure changed; update this slice helper."
    )
    return text[start:end]


def test_dice_section_has_mandatory_obligation() -> None:
    """§7 must carry a hard obligation token (MUST / MANDATORY), at
    parity with §1 ('you MUST call apply_status', 'MANDATORY'), §2
    ('CRITICAL LOCATION RULE'), §4 ('you MUST call advance_confrontation
    on the SAME turn'). Pre-fix §7 is a single soft sentence ('call
    roll_dice. When the prose hinges...') with no MUST anywhere in the
    section.
    """
    section = _dice_section()
    assert ("MUST" in section) or ("MANDATORY" in section), (
        "§7 DICE RESOLUTION carries no MUST/MANDATORY obligation. It is "
        "the only one of the eight tool-owned categories without one — "
        "that asymmetry is the contract-design root cause. Promote it to "
        "parity with §1/§2/§4. Section text:\n" + section
    )


def test_dice_section_self_gating_loophole_removed() -> None:
    """The self-gating clause is the exact mechanism by which a model
    that already wrote 'A 16 buys real currency' is *not required* to
    call the tool (it reads its own prose as the resolved 'certain'
    outcome). AC-1 requires this clause be deleted, not softened.
    """
    # WHITESPACE NORMALIZE: the clause wraps across a line in the prompt
    # ("...uncertain\n   outcome the engine should resolve..."), so a raw
    # single-line substring check FALSELY PASSES while the loophole is
    # still present. Collapse runs of whitespace before comparing — this
    # is the exact vacuous-pass the Phase-C self-check exists to catch.
    section_norm = " ".join(_dice_section().split())
    loophole_norm = " ".join(_SELF_GATING_LOOPHOLE.split())
    assert loophole_norm not in section_norm, (
        f"§7 still contains the self-gating loophole {_SELF_GATING_LOOPHOLE!r} "
        "(matched whitespace-insensitively). This clause lets the narrator "
        "opt out of rolling whenever it has already decided the outcome — "
        "i.e. always, after fabricating the number. AC-1 requires it "
        "deleted. Section text:\n" + _dice_section()
    )


def test_dice_section_carries_anti_fabrication_anchor() -> None:
    """§7 must explicitly forbid writing a die result the engine did not
    produce — the anti-fabrication anchor. This is the §7 analog of §4's
    'Do NOT resolve these narratively without advance_confrontation' and
    §1's 'a tool you don't call is a mechanic that never happened'.

    SHAPE assertion (mirrors 50-2's cue-set approach): any one concrete
    anti-fabrication phrasing satisfies the contract — the exact wording
    is Dev's to choose in GREEN; the *concept* must be present.
    """
    section = _dice_section().lower()
    anchors = (
        "must not write",
        "did not get from a tool",
        "before writing the number",
        "before you write",
        "not a number you invented",
        "never narrate a roll",
        "no roll behind",
    )
    assert any(a in section for a in anchors), (
        "§7 has no anti-fabrication anchor. The narrator must be told, in "
        "this section, that it MUST NOT write a d20/check/save/damage "
        f"result it did not obtain from a tool (tried shapes: {anchors!r}). "
        "Without it, §7 still tacitly permits 'A 16/A 19/A 7'. "
        "Section text:\n" + _dice_section()
    )


def test_dice_section_enumerates_triggers() -> None:
    """§4 enumerates its triggers (physical violence, bargaining,
    fleeing, …). §7 must likewise enumerate WHEN the obligation fires —
    any numeric result / save / check / damage figure for ANY actor —
    so the rule survives attention decay and is not a single vague line.
    Pre-fix §7 names none of these.
    """
    section = _dice_section().lower()
    # The obligation must reference the kinds of outcomes that require a
    # roll. Require coverage of the spread, not a single token.
    required = ("check", "save", "damage")
    missing = [tok for tok in required if tok not in section]
    assert not missing, (
        f"§7 does not enumerate its trigger conditions; missing {missing!r}. "
        "It must spell out that a check, a save, a damage figure (and any "
        "asserted numeric result for ANY actor) require a tool roll — the "
        "way §4 enumerates its confrontation triggers. Section text:\n" + _dice_section()
    )


# ---------------------------------------------------------------------------
# Non-regression guard (sentinel — PASSES now by design; must STAY green
# through the GREEN phase). Mirrors 50-2's
# test_confrontation_trigger_constraint_keeps_existing_space_opera_anchors:
# hardening §7 must not soften the other seven categories.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "anchor",
    [
        "you MUST call `apply_status`",  # §1
        # Story 61-12 AC-5 demoted §2's "CRITICAL LOCATION RULE" banner —
        # banner count capped at 4. The §2 routing rule remains expressible
        # via "Every location header ... is a scene boundary state must
        # track" + the apply_world_patch arrow.
        "Every location header",  # §2
        "you MUST call",  # §3/§4 generic obligation
        # Story 59-1: §4 STARTING routes to begin_confrontation (the SDK
        # engagement writer); advance_confrontation cannot start an encounter.
        "Do NOT resolve these narratively without `begin_confrontation`",  # §4
        "you MUST call `tick_tropes`",  # §5
    ],
)
def test_other_tool_categories_keep_mandatory_language(anchor: str) -> None:
    """SENTINEL (not an AC test): these anchors exist pre-fix and must
    survive the GREEN-phase §7 edit. If Dev's §7 hardening accidentally
    rewrites/relaxes a sibling category, this flips red and catches it.

    Story 61-12 updated the §2 anchor from "CRITICAL LOCATION RULE" to
    "Every location header" — AC-5 caps banner count at 4 and §2's banner
    was demoted; the routing rule itself remains expressible per AC-6.
    """
    assert anchor in NARRATOR_OUTPUT_ONLY, (
        f"Regression sentinel: the existing obligation anchor {anchor!r} "
        "disappeared from NARRATOR_OUTPUT_ONLY. Hardening §7 must not "
        "soften the other seven tool-owned categories."
    )
