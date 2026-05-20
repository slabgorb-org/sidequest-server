"""Story 50-2 — prompt-engineering RED tests.

Source: 2026-05-13 pingpong-archive triage. PR #177 added the OTEL warning
span for confrontation mismatch, but the implicit "fixed" work — actually
steering the narrator to emit ``confrontation`` on trigger prose for every
confrontation category the genre offers — was never done. The warning
currently fires as steady-state on Victoria turns because the prompt's
TRIGGER CRITERIA enumeration is missing the Victoria-pack social types
(scandal, social_duel, trial, auction) and the Recency-zone restatement
also omits them.

Scope (per session SM Assessment):
- The fix is the prompt, NOT the keyword detector.
- The keyword scanner was deleted 2026-05-20 (replaced by
  confrontation_intent_validator). This test still pins the prompt content
  so the narrator emits ``confrontation`` proactively.
- The OTEL confrontation.intent_mismatch span is the post-deletion
  regression detector. These tests pin the prompt content so the
  guardrail is real.

AC trigger keywords from the story: chase, intercept, scandal,
negotiation, social_duel, trial, auction. The seven cover the union of
movement (chase / intercept-via-ship-combat), social-mechanical
(negotiation), and Victoria social-pack types (scandal, social_duel,
trial, auction).
"""

from __future__ import annotations

import pytest

from sidequest.agents.narrator import NARRATOR_OUTPUT_ONLY
from sidequest.agents.orchestrator import Orchestrator, TurnContext

# Trigger-type keywords from the story AC. These are confrontation type
# strings the narrator must learn to emit on matching prose. "intercept"
# is a chase/ship_combat shape, not a standalone type — covered
# separately in the Recency-text test.
_SEVEN_AC_TRIGGER_TYPES: tuple[str, ...] = (
    "chase",
    "scandal",
    "negotiation",
    "social_duel",
    "trial",
    "auction",
)
# "intercept" is a prose pattern that should resolve to ship_combat/chase.
# Documented in the CONFRONTATION_TRIGGER_CONSTRAINT prompt text directly.
# Tracked in the Recency-zone-phrases test below.


# ---------------------------------------------------------------------------
# NARRATOR_OUTPUT_ONLY schema text — TRIGGER CRITERIA enumeration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger_type", _SEVEN_AC_TRIGGER_TYPES)
def test_narrator_output_only_trigger_criteria_lists_ac_type(trigger_type: str) -> None:
    """The schema's TRIGGER CRITERIA enumeration must reference every
    confrontation type named in the AC, so the narrator's System-zone
    instructions cover Victoria social types (scandal, social_duel,
    trial, auction) and not just combat/negotiation/chase.

    Currently the schema only lists three bullet categories:
        - Physical violence ... → combat/brawl
        - Bargaining ... → negotiation
        - Fleeing, pursuing, being chased → chase
    A turn-20 Victoria scandal scene gets no rule for emitting
    ``confrontation`` because the schema never names ``scandal``.
    """
    assert trigger_type in NARRATOR_OUTPUT_ONLY, (
        f"NARRATOR_OUTPUT_ONLY must enumerate trigger type {trigger_type!r} so the "
        f"narrator's System-zone schema covers it (story 50-2 AC). Currently the "
        f"schema only mentions combat / negotiation / chase, leaving Victoria "
        f"social-pack types invisible in the schema text."
    )


def test_narrator_output_only_lists_specialized_ship_combat_types() -> None:
    """Space-opera triggers must be enumerable too — narrator picks the
    MOST SPECIFIC type per the available-encounter-types section.

    ``intercept`` (the regex label in PR #177) maps to ship_combat /
    dogfight in space_opera. Without these named in the schema, a
    "patrol cutter spinning her reactor up" turn gets the trigger
    criteria but no anchor for which specific type to pick.
    """
    assert "ship_combat" in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must reference ship_combat so the narrator "
        "doesn't default to generic ``combat`` for a starship engagement."
    )
    assert "dogfight" in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must reference dogfight so the narrator picks "
        "the more specific type when space_opera offers it."
    )


def test_narrator_output_only_trigger_emit_obligation_is_explicit() -> None:
    """The schema must state the must-emit-this-turn rule, not just
    "err on the side of triggering". The deferral failure mode (narrator
    waits a turn, encounter never fires) is exactly what PR #177's
    warning span surfaced.
    """
    text = NARRATOR_OUTPUT_ONLY
    # The schema must explicitly forbid deferring the confrontation emit
    # to a later turn. "no retroactive crediting" is the phrase used in
    # the Recency-zone restatement; the System-zone schema needs the
    # same anchor so the rule survives attention decay.
    assert "no retroactive crediting" in text or "MUST emit" in text, (
        "NARRATOR_OUTPUT_ONLY must explicitly forbid deferring the "
        "confrontation emit. PR #177's warning span fires because the "
        "narrator routinely defers — the schema currently uses soft "
        "language ('err on the side of triggering') instead of a hard "
        "must-emit-this-turn anchor."
    )


# ---------------------------------------------------------------------------
# Recency-zone Guardrail — confrontation_trigger_constraint text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger_type",
    ("scandal", "social_duel", "trial", "auction"),
)
async def test_confrontation_trigger_constraint_lists_victoria_social_types(
    trigger_type: str,
) -> None:
    """The Recency-zone Guardrail restatement must enumerate the Victoria
    social-pack types alongside the space-opera examples it already
    carries. Without this, a Victoria turn-20 scandal scene gets a
    Recency restatement that talks about reactors spinning up and
    weapons going hot — irrelevant noise that doesn't reinforce the
    actual social-trigger rule.

    The Recency restatement is what survives attention decay (the
    System-zone schema decays by turn 20+). For social packs to benefit
    from the same fix that PR #177 applied for space packs, the
    Recency text must name social_duel / scandal / trial / auction
    explicitly with concrete prose patterns.
    """
    from tests.agents.test_orchestrator import make_canned_client

    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Miss Halloway")
    prompt, _ = await orch.build_narrator_prompt("open the morning post", context)
    assert "<confrontation-trigger>" in prompt, (
        "confrontation_trigger_constraint section must be registered on every "
        "turn (Recency Guardrail, ADR-082 prompt framework)."
    )
    assert trigger_type in prompt, (
        f"Recency-zone confrontation_trigger_constraint must reference "
        f"the social-pack type {trigger_type!r} so Victoria turns get a "
        f"relevant restatement of the must-emit rule. Currently the text "
        f"only mentions chase / ship_combat / dogfight / combat / "
        f"negotiation — space-opera flavoured. Social packs get no "
        f"trigger-type anchor in Recency."
    )


@pytest.mark.asyncio
async def test_confrontation_trigger_constraint_carries_social_prose_patterns() -> None:
    """The Recency restatement carries concrete trigger phrases for
    space-opera prose (``spinning``, ``permission to engage``). Social
    packs need the same shape: concrete phrases that signal scandal /
    auction / trial / social_duel preludes. Without them the narrator
    has no parallel cue set to bind on.

    Examples we expect to see (any subset is fine — these are SHAPES):
        - scandal: "rumour", "in print", "exposure", "Society pages"
        - auction: "bids", "auctioneer", "the lot is called"
        - trial: "summons", "court convenes", "before the magistrate"
        - social_duel: "card declined", "seconds appointed", "cut direct"
    The test asserts the presence of AT LEAST ONE concrete phrase per
    Victoria social category — so the GREEN-phase prompt edit lands
    real trigger shapes, not just type-name labels.
    """
    from tests.agents.test_orchestrator import make_canned_client

    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Miss Halloway")
    prompt, _ = await orch.build_narrator_prompt("open the morning post", context)
    lowered = prompt.lower()

    # Each Victoria type needs at least one concrete prose-shape phrase
    # in the Recency restatement so the narrator has a binding cue.
    scandal_cues = ("rumour", "rumor", "in print", "exposure", "society pages")
    auction_cues = ("bid", "auctioneer", "the lot", "lots called", "going once")
    trial_cues = ("summons", "magistrate", "court convenes", "before the bench", "docket")
    duel_cues = ("seconds", "cut direct", "card declined", "challenge issued", "first blood")

    for category, cues in (
        ("scandal", scandal_cues),
        ("auction", auction_cues),
        ("trial", trial_cues),
        ("social_duel", duel_cues),
    ):
        assert any(cue in lowered for cue in cues), (
            f"Recency-zone confrontation_trigger_constraint must carry "
            f"AT LEAST ONE concrete prose cue for {category!r} (tried: "
            f"{cues!r}). Without a concrete shape the type-name alone "
            f"won't bind on the narrator's prose-recognition pass — "
            f"compare the space-opera shapes already present "
            f"(``spinning``, ``permission to engage``, ``weapons hot``)."
        )


@pytest.mark.asyncio
async def test_confrontation_trigger_constraint_keeps_existing_space_opera_anchors() -> None:
    """Guard against accidentally removing the space-opera trigger
    anchors when adding social ones. PR #177's fix for Itchy's chase
    must keep working.
    """
    from tests.agents.test_orchestrator import make_canned_client

    client = make_canned_client("narration")
    orch = Orchestrator(client=client)
    context = TurnContext(character_name="Itchy")
    prompt, _ = await orch.build_narrator_prompt("watch the gantry", context)
    assert "<confrontation-trigger>" in prompt
    assert "spinning" in prompt
    assert "permission to engage" in prompt
    assert "ship_combat" in prompt
    assert "dogfight" in prompt
    # The deferral failure mode anchor must survive.
    assert "no retroactive crediting" in prompt


# ---------------------------------------------------------------------------
# Rule coverage — every TRIGGER CRITERIA bullet maps to a real
# confrontation type the genre stack uses. Guards against drift where
# the schema mentions a category the engine doesn't route.
# ---------------------------------------------------------------------------


def test_narrator_output_only_trigger_categories_are_routable() -> None:
    """For each AC trigger-type, the schema text must spell it the same
    way the engine routes it (lowercase type strings, no synonyms). The
    server uses ``encounter_type`` as a closed enum keyed on the pack's
    ``confrontations[].type`` field — a typo in the schema ("Scandal"
    vs "scandal") would silently fail to instantiate.
    """
    # All AC types are spelled lowercase, snake_case where compound.
    # Assert their exact occurrence as standalone tokens in the schema
    # (i.e. fenced by non-alphanumeric chars on at least one side).
    import re

    for trigger_type in _SEVEN_AC_TRIGGER_TYPES:
        # \b doesn't fire on underscores in Python regex, so we use a
        # broader fence that accepts code-quote (``), word boundary, or
        # whitespace.
        pattern = re.compile(rf"(^|[\s`\W])({re.escape(trigger_type)})($|[\s`\W])")
        assert pattern.search(NARRATOR_OUTPUT_ONLY), (
            f"NARRATOR_OUTPUT_ONLY must spell the type {trigger_type!r} "
            f"exactly as the engine routes it (lowercase, snake_case). "
            f"A Capital-S 'Scandal' in the schema would never instantiate "
            f"because pack.rules.confrontations[].type is 'scandal'."
        )
