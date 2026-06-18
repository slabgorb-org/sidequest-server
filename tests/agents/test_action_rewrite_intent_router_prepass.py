"""RED tests for Story 151-3 — action_rewrite → IntentRouter pre-pass (ADR-150 step 3).

``action_rewrite`` (``you``/``named``/``intent``) is a mechanical transform of the
player's OWN submitted action — it needs nothing from the narrator's prose.
ADR-150 §1 moves it OFF the narrator's post-narration ``game_patch`` sidecar and
ONTO the pre-narrator ``IntentRouter`` (which already reads the action and resolves
referents), closing a current ordering hazard: today the field is emitted by the
very turn whose visibility (``visibility_classifier``) and confrontation-intent it
is meant to gate.

These tests are RED until:
  - ``DispatchPackage`` carries ``action_rewrite`` (you/named/intent), produced by
    the IntentRouter's existing single Haiku ``emit_tool`` call (no new model
    call — the schema the router already forces IS the DispatchPackage schema).
  - ``decompose`` emits an ``intent_router.action_rewrite`` OTEL span (the
    GM-panel lie-detector for the new producer; ``emitted`` true/false BOTH
    recorded — the loud net during transition, per CLAUDE.md OTEL principle).
  - ``output_only.md`` no longer instructs the narrator to emit ``action_rewrite``.

Fixture-based only (epic-151 testing discipline; project memory
``feedback_no_content_coupled_tests``) — synthetic Haiku tool inputs drive the
REAL IntentRouter; we assert the produced field AND the span, never a
source-text grep of production code.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Fixtures — synthetic SDK-Haiku tool inputs (ADR-102 returns a structured
# dict, not free-text JSON). Mirrors tests/agents/test_intent_router.py.
# ---------------------------------------------------------------------------


def _mock_router_llm(response: dict) -> AsyncMock:
    mock = AsyncMock()
    mock.emit_tool = AsyncMock(return_value=response)
    return mock


def _package_dict(*, action_rewrite: dict | None) -> dict:
    """A schema-valid DispatchPackage tool input, optionally carrying the new
    pre-pass ``action_rewrite`` field (you/named/intent)."""
    pkg: dict = {
        "turn_id": "turn-ar-1",
        "per_player": [],
        "cross_player": [],
        "confidence_global": 1.0,
    }
    if action_rewrite is not None:
        pkg["action_rewrite"] = action_rewrite
    return pkg


# ---------------------------------------------------------------------------
# AC1 — the pre-pass IntentRouter PRODUCES action_rewrite from the raw action.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_emits_action_rewrite_from_player_action() -> None:
    """AC1: ``decompose`` returns a DispatchPackage carrying ``action_rewrite``
    (you/named/intent) — the player-input transform now rides the pre-narrator
    pass, not the narrator sidecar (ADR-150 §1)."""
    from sidequest.agents.intent_router import IntentRouter

    llm = _mock_router_llm(
        _package_dict(
            action_rewrite={
                "you": "You draw your sword",
                "named": "Kael draws their sword",
                "intent": "draw sword",
            }
        )
    )
    router = IntentRouter(llm=llm)
    pkg = await router.decompose(action="I draw my sword", state_summary={})

    assert pkg.action_rewrite is not None, (
        "the pre-pass IntentRouter must produce action_rewrite (ADR-150 §1) — "
        "the player-input transform no longer rides the narrator game_patch sidecar"
    )
    assert pkg.action_rewrite.you == "You draw your sword"
    assert pkg.action_rewrite.named == "Kael draws their sword"
    assert pkg.action_rewrite.intent == "draw sword"


@pytest.mark.asyncio
async def test_decompose_action_rewrite_absent_is_none_not_invented() -> None:
    """No silent fallbacks: when the producer omits ``action_rewrite`` the
    package carries ``None`` (the downstream omitted→default fallback is the
    loud net during transition), never an invented rewrite."""
    from sidequest.agents.intent_router import IntentRouter

    llm = _mock_router_llm(_package_dict(action_rewrite=None))
    router = IntentRouter(llm=llm)
    pkg = await router.decompose(action="wait", state_summary={})

    assert pkg.action_rewrite is None


# ---------------------------------------------------------------------------
# AC3 — intent_router.action_rewrite OTEL span (GM-panel lie-detector).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_emits_action_rewrite_span_when_present(otel_capture) -> None:
    """AC3: a successful decompose that produced a rewrite emits exactly one
    ``intent_router.action_rewrite`` span with ``emitted=True`` so the GM panel
    can audit that the new producer engaged."""
    from sidequest.agents.intent_router import IntentRouter

    llm = _mock_router_llm(
        _package_dict(
            action_rewrite={"you": "You wait", "named": "Kael waits", "intent": "wait"}
        )
    )
    router = IntentRouter(llm=llm)
    await router.decompose(action="I wait", state_summary={})

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.action_rewrite"
    ]
    assert len(spans) == 1, (
        f"expected exactly one intent_router.action_rewrite span; got {len(spans)}"
    )
    assert dict(spans[0].attributes or {}).get("emitted") is True


@pytest.mark.asyncio
async def test_decompose_action_rewrite_span_marks_absent(otel_capture) -> None:
    """AC3: the GM panel must SEE the absence (loud net) — the span still fires
    with ``emitted=False`` when the producer omitted the rewrite, never a silent
    skip (CLAUDE.md OTEL Observability Principle)."""
    from sidequest.agents.intent_router import IntentRouter

    llm = _mock_router_llm(_package_dict(action_rewrite=None))
    router = IntentRouter(llm=llm)
    await router.decompose(action="wait", state_summary={})

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.action_rewrite"
    ]
    assert len(spans) == 1, (
        f"expected exactly one intent_router.action_rewrite span; got {len(spans)}"
    )
    assert dict(spans[0].attributes or {}).get("emitted") is False


# ---------------------------------------------------------------------------
# AC4 — the narrator output contract no longer teaches action_rewrite.
# ---------------------------------------------------------------------------


def test_output_only_md_no_longer_instructs_action_rewrite() -> None:
    """AC4: ``output_only.md`` no longer instructs the narrator to emit
    ``action_rewrite`` — it is produced pre-narrator now. This asserts on the
    CONTRACT ARTIFACT (the prompt template that IS this AC's deliverable), not a
    wiring grep of production source. Plain substring, no regex (no catastrophic
    backtracking risk per CLAUDE.md 'No Source-Text Wiring Tests')."""
    import sidequest.agents as agents_pkg

    output_only = Path(agents_pkg.__file__).parent / "narrator_prompts" / "output_only.md"
    text = output_only.read_text(encoding="utf-8")
    assert "action_rewrite" not in text, (
        "output_only.md still instructs the narrator to emit action_rewrite; "
        "ADR-150 step 3 retires it from PART 2 (now produced by the IntentRouter)"
    )
