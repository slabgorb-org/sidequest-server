"""Story 50-24 AC-2 — narrator→ADR-074 seam for player-actor checks.

Source: /Users/slabgorb/Projects/sq-playtest-pingpong.md, "OQ-2 ARCHITECT
RESOLUTION" bullet under the "Narrator fabricates dice" headline
(Architect/OQ-2, 2026-05-17).

Root cause (Architect-resolved, missing-wire defect): the SDK narrator
has no instruction OR mechanism to push a player-actor uncertain
outcome through the player-facing roll path (opposed_check /
ADR-074 ``DICE_REQUEST``). The opposed_check machinery itself is fully
built and wired — see tests/server/test_opposed_check_wiring.py — but it
only engages *inside an active structured confrontation*. The fabricated
rolls in the coyote_star session were *ad-hoc* player checks
(negotiation / stealth / info: "A 16 buys real currency", "17 on the
run, clean and fast", "11, good enough for the dark") that never entered
a structured encounter, so they fell to the free-text path where, by
``BeatSelection.outcome`` design, "the narrator emits it".

AC-2 (verbatim from the story context):
- SDK narrator must have instruction AND mechanism to select
  ``ResolutionMode.opposed_check`` / request a player-facing throw for
  player-actor checks
- Narrator must FORBID free-text tier-emission for player-actor
  uncertainty (the way §4 forbids unstructured confrontation resolution)
- Narrator must defer the outcome tier to the returned ADR-074 face,
  not pre-write it

SCOPE: AC-2's deterministic surface is the prompt CONTRACT plus the
production prompt-selection seam. The end-to-end "the live LLM actually
emits a roll request" is non-deterministic and NOT unit-testable here;
the *engine entrypoint* for ad-hoc (non-confrontation) player rolls is a
Dev design decision for the GREEN phase — flagged in Delivery Findings.
These tests pin the contract that proves the gap, mirroring 50-2.
"""

from __future__ import annotations

import pytest

from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.narrator_prompts import (
    NARRATOR_OUTPUT_ONLY,
    NARRATOR_OUTPUT_ONLY_SDK,
)
from sidequest.agents.prompt_framework.core import PromptRegistry


def _composed_output_format(*, tool_backend: bool) -> str:
    """Exercise the production selection seam.

    ``NarratorAgent.build_output_format`` (narrator.py:274) is, per its
    own docstring, "called exactly once from
    ``Orchestrator.build_narrator_prompt``" with
    ``tool_backend=isinstance(self._client, ToolingLlmClient)``. Driving
    it through a real ``PromptRegistry`` and composing is the same
    technique tests/server/test_opposed_check_wiring.py uses for the
    encounter gate — a genuine wiring test, not a constant read.
    """
    narrator = NarratorAgent()
    registry = PromptRegistry()
    narrator.build_output_format(registry, tool_backend=tool_backend)
    return registry.compose(narrator.name())


# ---------------------------------------------------------------------------
# Contract: the SDK prompt forbids the narrator self-resolving a PLAYER's
# uncertain action and points it at the player-facing roll path.
# ---------------------------------------------------------------------------


def test_sdk_prompt_forbids_self_resolving_player_outcomes() -> None:
    """§4 says "Do NOT resolve these narratively without
    `advance_confrontation`". The opposed_check gate says "do not
    narrate whether it lands or fails". There is NO equivalent hard
    forbiddance for an ad-hoc *player-actor* uncertain action — that is
    the gap that produced the fabricated d20s. The SDK prompt must
    forbid the narrator deciding, in prose, whether a player's uncertain
    attempt succeeds.

    SHAPE assertion (mirrors 50-2's cue sets — exact wording is Dev's to
    choose in GREEN): a forbiddance token must co-occur with a
    player-action-resolution token.
    """
    text = NARRATOR_OUTPUT_ONLY_SDK.lower()
    forbid_tokens = (
        "do not narrate whether",
        "do not decide whether",
        "must not resolve",
        "do not resolve the outcome",
        "you do not roll for the player",
        "must not write the outcome of a player",
    )
    player_resolution_tokens = (
        "player's uncertain",
        "player attempts",
        "a player action whose outcome",
        "whether the player succeeds",
        "player-actor check",
        "the player's roll",
    )
    has_forbid = any(t in text for t in forbid_tokens)
    has_player_ctx = any(t in text for t in player_resolution_tokens)
    assert has_forbid and has_player_ctx, (
        "NARRATOR_OUTPUT_ONLY_SDK does not forbid the narrator from "
        "self-resolving a PLAYER's uncertain action outcome. §4 forbids "
        "the confrontation analog; the ad-hoc player-check case has no "
        "such rule, which is exactly why 'A 16 buys real currency' was "
        f"allowed. (forbid token present={has_forbid}, player-resolution "
        f"context present={has_player_ctx})"
    )


def test_sdk_prompt_routes_player_checks_to_player_facing_roll() -> None:
    """Pre-fix §7 points ONLY at the narrator-private ``roll_dice`` (no
    dice cup — Keith's ground truth: "the table never saw dice"). The
    SDK prompt must additionally route player-actor uncertain outcomes
    to the player-FACING path — opposed_check / a requested throw the
    table rolls / the engine resolving and the narrator deferring to the
    returned face.

    SHAPE assertion: at least one player-facing-roll concept must be
    referenced in the prompt.
    """
    text = NARRATOR_OUTPUT_ONLY_SDK.lower()
    player_facing_tokens = (
        "opposed_check",
        "opposed check",
        "dice_request",
        "the player rolls",
        "request a roll",
        "request a throw",
        "defer the outcome",
        "defer to the returned",
        "wait for the roll",
    )
    assert any(t in text for t in player_facing_tokens), (
        "NARRATOR_OUTPUT_ONLY_SDK never references the player-facing roll "
        "path. §7 points only at the private roll_dice tool, so even a "
        "perfectly compliant narrator yields no dice cup. The prompt must "
        f"route player-actor checks to the player-facing path (tried: "
        f"{player_facing_tokens!r})."
    )


# ---------------------------------------------------------------------------
# Wiring: the rule must reach the composed prompt ON THE SDK PATH
# (tool_backend=True). A fix landed in the non-SDK prompt is a non-fix —
# the coyote_star fabrication was SDK-path-specific.
# ---------------------------------------------------------------------------


def test_player_check_rule_reaches_composed_sdk_prompt() -> None:
    """WIRING (CLAUDE.md: every test suite needs one). Drive the
    production selector with ``tool_backend=True`` and assert the
    player-check rule survives composition into the registry section the
    live tool-backed narrator actually receives.
    """
    composed = _composed_output_format(tool_backend=True).lower()
    routing_tokens = (
        "opposed_check",
        "opposed check",
        "dice_request",
        "request a roll",
        "request a throw",
        "defer the outcome",
        "defer to the returned",
        "do not narrate whether",
        "must not resolve",
    )
    assert any(t in composed for t in routing_tokens), (
        "The composed SDK output-format section (build_output_format, "
        "tool_backend=True — the section the live tool-backed narrator "
        "receives) carries no player-actor-check routing rule. The "
        "machinery exists (test_opposed_check_wiring.py); the missing "
        "wire is the narrator instruction reaching this composed prompt."
    )


def test_player_check_rule_is_sdk_path_specific_not_legacy_only() -> None:
    """NEGATIVE WIRING / anti-mis-fix guard. The coyote_star fabrication
    was on the SDK (tool_backend=True) path. A GREEN-phase fix that adds
    the rule ONLY to the legacy non-SDK prompt would leave the actual
    bug unfixed while turning the content tests green via the shared
    constant. Assert the rule is present on the SDK composition; if it is
    *also* only-in-legacy this still fails, forcing the fix onto the
    right path.
    """
    sdk_composed = _composed_output_format(tool_backend=True).lower()
    routing_tokens = (
        "opposed_check",
        "opposed check",
        "dice_request",
        "request a roll",
        "request a throw",
        "defer the outcome",
        "defer to the returned",
        "do not narrate whether",
        "must not resolve",
    )
    in_sdk = any(t in sdk_composed for t in routing_tokens)
    assert in_sdk, (
        "Player-check routing rule is absent from the tool_backend=True "
        "composition. The bug is SDK-path-specific (ADR-101/102) — the "
        "fix MUST land where NARRATOR_OUTPUT_ONLY_SDK is selected, not "
        "only in the legacy prompt. (Shared-constant note: if Dev edits "
        "output_only_sdk.md the content tests AND this wiring test go "
        "green together, which is correct; this guard exists so a "
        "legacy-only edit cannot fake a pass.)"
    )


@pytest.mark.parametrize(
    "legacy_anchor",
    [
        # §4 confrontation forbiddance must survive — proves we did not
        # achieve "forbid player resolution" by gutting the existing
        # confrontation forbiddance and reusing its text loosely.
        # Story 59-1 corrected the named tool: STARTING a confrontation
        # routes to begin_confrontation (advance_confrontation cannot start
        # one). The forbiddance is unchanged; only the tool name moved.
        "Do NOT resolve these narratively without `begin_confrontation`",
    ],
)
def test_existing_confrontation_forbiddance_survives(legacy_anchor: str) -> None:
    """SENTINEL (passes now by design; must stay green). The AC-2 fix
    adds a player-check forbiddance; it must not be achieved by
    cannibalising §4's confrontation forbiddance.
    """
    assert legacy_anchor in NARRATOR_OUTPUT_ONLY_SDK, (
        f"Regression sentinel: §4 anchor {legacy_anchor!r} vanished. The "
        "player-check forbiddance must be ADDED, not carved out of §4."
    )
    # The legacy non-SDK prompt is the playgroup's current path until
    # merge (narrator.py docstring: "MUST NOT drift"). Touch only the
    # SDK prompt for the player-check rule.
    assert NARRATOR_OUTPUT_ONLY is not None
