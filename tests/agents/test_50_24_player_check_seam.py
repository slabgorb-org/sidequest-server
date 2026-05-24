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

Story 61-9 collapse: the legacy ``claude -p`` narrator path retired with
ADR-101 amendment; ``build_output_format`` is now backend-agnostic and
the dual ``tool_backend`` parameterisation is gone. ``NARRATOR_OUTPUT_ONLY``
is the SDK prose post-rename.
"""

from __future__ import annotations

from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY
from sidequest.agents.prompt_framework.core import PromptRegistry


def _composed_output_format() -> str:
    """Exercise the production selection seam.

    ``NarratorAgent.build_output_format`` is, per its own docstring,
    "called exactly once from ``Orchestrator.build_narrator_prompt``"
    (post-61-9 with no backend kwarg). Driving it through a real
    ``PromptRegistry`` and composing is the same technique
    tests/server/test_opposed_check_wiring.py uses for the encounter
    gate — a genuine wiring test, not a constant read.
    """
    narrator = NarratorAgent()
    registry = PromptRegistry()
    narrator.build_output_format(registry)
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
    text = NARRATOR_OUTPUT_ONLY.lower()
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
        "NARRATOR_OUTPUT_ONLY does not forbid the narrator from "
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
    text = NARRATOR_OUTPUT_ONLY.lower()
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
        "NARRATOR_OUTPUT_ONLY never references the player-facing roll "
        "path. §7 points only at the private roll_dice tool, so even a "
        "perfectly compliant narrator yields no dice cup. The prompt must "
        f"route player-actor checks to the player-facing path (tried: "
        f"{player_facing_tokens!r})."
    )


# ---------------------------------------------------------------------------
# Wiring: the rule must reach the composed prompt the SDK narrator
# actually receives.
# ---------------------------------------------------------------------------


def test_player_check_rule_reaches_composed_sdk_prompt() -> None:
    """WIRING (CLAUDE.md: every test suite needs one). Drive the
    production selector and assert the player-check rule survives
    composition into the registry section the live tool-backed narrator
    actually receives.
    """
    composed = _composed_output_format().lower()
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
        "The composed output-format section (build_output_format — the "
        "section the live tool-backed narrator receives) carries no "
        "player-actor-check routing rule. The machinery exists "
        "(test_opposed_check_wiring.py); the missing wire is the narrator "
        "instruction reaching this composed prompt."
    )


def test_existing_confrontation_forbiddance_survives() -> None:
    """SENTINEL (passes now by design; must stay green). The AC-2 fix
    adds a player-check forbiddance; it must not be achieved by
    cannibalising §4's confrontation forbiddance.
    """
    # Story 59-1 corrected the named tool: STARTING a confrontation
    # routes to begin_confrontation (advance_confrontation cannot start
    # one). The forbiddance is unchanged; only the tool name moved.
    legacy_anchor = "Do NOT resolve these narratively without `begin_confrontation`"
    assert legacy_anchor in NARRATOR_OUTPUT_ONLY, (
        f"Regression sentinel: §4 anchor {legacy_anchor!r} vanished. The "
        "player-check forbiddance must be ADDED, not carved out of §4."
    )
