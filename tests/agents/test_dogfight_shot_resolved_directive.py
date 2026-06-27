"""Story 158-35 / ADR-153 §7 — dogfight dice-replay re-entry must narrate the
RESOLVED beat, not the prior turn's prose.

The live repro (2026-06-25 coyote_star playtest, recorded in ADR-153's
forensics): the player committed "firewall the throttle, gun pass" + a Throttle
Up beat; the returned narration re-described the PRIOR turn's sensor sweep
(rock, debris, an old claim beacon) and never mentioned the maneuver, gun pass,
dogfight, or opponent.

Root cause (deterministic, prompt-precondition layer): the dogfight dice-replay
re-entry hands the narrator a terse mechanical marker
``[DOGFIGHT_SHOT_RESOLVED] ...``. The default action framing wrapped it as
``"<PC> says: [DOGFIGHT_SHOT_RESOLVED] ..."`` — a mechanical tag presented as PC
dialogue. With nothing narratable in the action, the strongest signal in the
prompt was the prior turn's scene in the load-bearing Recency zone
(``recent_narrative_context``, 49-1), so the narrator re-emitted it.

These are NOT behavioral assertions against the LLM (we do not replay Claude);
they are precondition assertions against the prompt, the same methodology as
``test_glenross_replay_recency_window.py``: if the action is framed as a
resolved-beat directive — not PC speech — and the prior scene is explicitly
out-of-bounds, then a good narrator HAS the visible context to narrate the gun
pass instead of the sensor sweep. The continuity Recency window is preserved
(the maneuver setup must stay visible), only the action framing changes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sidequest.agents.orchestrator import Orchestrator
from sidequest.agents.prompt_framework.types import AttentionZone
from sidequest.game.session import NarrativeEntry

# The terse mechanical marker the dice_throw dogfight branch builds and hands to
# ``_execute_narration_turn`` (see handlers/dice_throw.py).
DOGFIGHT_REPLAY_ACTION = "[DOGFIGHT_SHOT_RESOLVED] Your laser: HIT, 6 dmg to Bandit Ace (hull 6/12)"

# Distinctive prior-turn prose — the sensor sweep that leaked into the gun-pass
# narration in the coyote_star repro. It must remain visible for continuity but
# the action directive must forbid re-describing it.
PRIOR_SENSOR_SWEEP = (
    "Your scopes sweep the belt: a tumbling rock, scattered debris, and an old "
    "claim beacon blinking in the dark."
)


def _section_by_name(registry, agent_name: str, name: str):
    for section in registry.registry(agent_name):
        if section.name == name:
            return section
    return None


def _dogfight_recency_log() -> list[NarrativeEntry]:
    """A prior maneuver-setup turn + the sensor-sweep prose that leaked."""
    return [
        NarrativeEntry(
            round=4,
            author="Player",
            content="I throttle up and loop in behind the bandit for a gun pass.",
        ),
        NarrativeEntry(round=5, author="narrator", content=PRIOR_SENSOR_SWEEP),
    ]


@pytest.mark.asyncio
async def test_dogfight_shot_resolved_action_is_not_framed_as_pc_speech(
    simple_turn_context_turn_three,
):
    """The headline fix: a ``[DOGFIGHT_SHOT_RESOLVED]`` action must NOT be framed
    as ``"<PC> says: ..."``. That framing is what made the narrator treat the
    mechanical tag as un-narratable dialogue and fall back to the prior scene."""
    ctx = replace(
        simple_turn_context_turn_three,
        recent_narrative_log=_dogfight_recency_log(),
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt(DOGFIGHT_REPLAY_ACTION, ctx)

    section = _section_by_name(registry, orch._narrator.name(), "player_action")
    assert section is not None, "player_action section must be registered"
    body = section.content

    assert f"{ctx.character_name} says:" not in body, (
        "dogfight shot-resolved replay was framed as PC speech "
        f"('{ctx.character_name} says: ...') — the narrator cannot narrate a "
        "mechanical tag as dialogue and falls back to the prior scene"
    )


@pytest.mark.asyncio
async def test_dogfight_shot_resolved_action_carries_resolved_beat_directive(
    simple_turn_context_turn_three,
):
    """The action section must explicitly direct the narrator to narrate THIS
    gun pass and forbid re-describing the prior scene — the precondition for the
    narrator escaping the Recency-zone sensor-sweep anchor."""
    ctx = replace(
        simple_turn_context_turn_three,
        recent_narrative_log=_dogfight_recency_log(),
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt(DOGFIGHT_REPLAY_ACTION, ctx)

    section = _section_by_name(registry, orch._narrator.name(), "player_action")
    assert section is not None
    body = section.content.lower()

    # Directs the narrator to narrate the gun pass now...
    assert "gun pass" in body, "directive must name the gun pass to narrate"
    assert "this" in body, "directive must point the narrator at THIS turn's shot"
    # ...and explicitly forbids restating the prior scene (the sensor sweep).
    assert "do not restate" in body or "not restate" in body, (
        "directive must forbid restating/continuing the prior scene — without "
        "it the narrator re-emits the Recency-zone sensor sweep"
    )

    # The raw mechanical outcome still rides into the action (the narrator needs
    # the hit/damage/hull facts to narrate honestly).
    assert "[dogfight_shot_resolved]" in body
    assert "6 dmg" in body and "bandit ace" in body


@pytest.mark.asyncio
async def test_recency_window_still_carries_maneuver_setup_for_continuity(
    simple_turn_context_turn_three,
):
    """The fix must NOT strip the Recency window: the maneuver-setup prose stays
    visible so the gun-pass narration has continuity (49-1's load-bearing
    recent_narrative_context). Only the ACTION framing changes."""
    ctx = replace(
        simple_turn_context_turn_three,
        recent_narrative_log=_dogfight_recency_log(),
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt(DOGFIGHT_REPLAY_ACTION, ctx)

    recency = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert recency is not None, "recency window must remain registered for continuity"
    assert recency.zone == AttentionZone.Recency
    assert "loop in behind the bandit" in recency.content, (
        "maneuver-setup prose dropped from recency — gun-pass narration loses its continuity anchor"
    )


@pytest.mark.asyncio
async def test_normal_player_action_framing_unchanged(
    simple_turn_context_turn_three,
):
    """Scope guard: a normal (non-replay) player action is still framed as
    ``"<PC> says: ..."``. The directive branch is keyed strictly on the
    ``[DOGFIGHT_SHOT_RESOLVED]`` marker — normal turns are untouched."""
    ctx = replace(
        simple_turn_context_turn_three,
        recent_narrative_log=_dogfight_recency_log(),
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("I bank hard to starboard.", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "player_action")
    assert section is not None
    assert f"{ctx.character_name} says: I bank hard to starboard." in section.content, (
        "normal player action framing regressed — the directive branch must only "
        "match the dogfight shot-resolved marker"
    )
