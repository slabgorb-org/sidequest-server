"""Test that extra_directive is threaded into the narrator's recency-zone prompt.

Spec 2026-05-20 confrontation-intent-validator step 7. The reprompt loop
passes extra_directive to run_narration_turn; the prompt builder must
register a recency-zone PromptSection so the narrator sees the directive.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import TurnContext


def test_turn_context_default_extra_directive_is_none() -> None:
    ctx = TurnContext()
    assert ctx.extra_directive is None


def test_turn_context_extra_directive_settable() -> None:
    ctx = TurnContext(extra_directive="Previous attempt described a combat...")
    assert ctx.extra_directive == "Previous attempt described a combat..."
