"""Dispatch tests for narration_apply intent-validator branches.

Covers warn / soft_suggest / reprompt severities and the classified_intent
single-exit invariant. Spec 2026-05-20 confrontation-intent-validator step 5.

Task 8 update: monkeypatch target is now the real span context manager on the
spans module (sidequest.telemetry.spans.confrontation_intent_mismatch_span).
The dispatch site does a lazy `from sidequest.telemetry.spans import ...`
inside the function body so the monkeypatched attribute is evaluated each
call.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.agents.orchestrator import ActionRewrite, NarrationTurnResult
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "intent_test_pack"


@pytest.fixture
def pack():
    return load_genre_pack(_FIXTURE_PACK)


def _result(intent: str, confrontation: str | None) -> NarrationTurnResult:
    return NarrationTurnResult(
        narration="prose",
        action_rewrite=ActionRewrite(intent=intent),
        confrontation=confrontation,
        npcs_present=[],
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot(genre_slug="intent_test_pack", encounter=None)


def _capture(spans_sink: list[dict]):
    """Return a fake context manager that records kwargs in spans_sink."""

    @contextmanager
    def fake_span(**kwargs):
        spans_sink.append(kwargs)
        yield None

    return fake_span


def test_no_mismatch_classifies_from_action_rewrite_intent(pack, monkeypatch) -> None:
    import sidequest.telemetry.spans as spans_mod

    snap = _snapshot()
    result = _result(intent="look around quietly", confrontation=None)
    room = MagicMock()

    spans: list[dict] = []
    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", _capture(spans))

    outcome = _apply(snap, result, "Player1", room=room, pack=pack)

    assert outcome.classified_intent == "look around quietly"
    assert outcome.reprompt_request is None
    assert spans == []
    assert snap.next_turn_directives == []


def test_warn_severity_emits_span_classifies_matched_type(pack, monkeypatch) -> None:
    import sidequest.telemetry.spans as spans_mod

    snap = _snapshot()
    result = _result(intent="bargain hard for the price", confrontation=None)
    room = MagicMock()
    spans: list[dict] = []
    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", _capture(spans))

    outcome = _apply(snap, result, "Player1", room=room, pack=pack)

    assert len(spans) == 1
    assert spans[0]["severity"] == "warn"
    assert spans[0]["matched_type"] == "negotiation_warn"
    assert outcome.reprompt_request is None
    assert outcome.classified_intent == "negotiation_warn"
    assert snap.next_turn_directives == []  # warn does not enqueue


def test_soft_suggest_severity_enqueues_directive(pack, monkeypatch) -> None:
    import sidequest.telemetry.spans as spans_mod

    snap = _snapshot()
    result = _result(intent="persuade the magistrate", confrontation=None)
    room = MagicMock()
    spans: list[dict] = []
    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", _capture(spans))

    outcome = _apply(snap, result, "Player1", room=room, pack=pack)

    assert outcome.reprompt_request is None
    assert outcome.classified_intent == "negotiation_soft"
    assert len(snap.next_turn_directives) == 1
    assert "negotiation_soft" in snap.next_turn_directives[0]
    assert spans[0]["severity"] == "soft_suggest"


def test_reprompt_severity_returns_request_does_not_apply_narration(pack, monkeypatch) -> None:
    import sidequest.telemetry.spans as spans_mod

    snap = _snapshot()
    result = _result(intent="strike the bandit dead", confrontation=None)
    room = MagicMock()
    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", _capture([]))

    outcome = _apply(snap, result, "Player1", room=room, pack=pack)

    assert outcome.reprompt_request is not None
    assert outcome.reprompt_request.matched_type == "combat_reprompt"
    assert "combat_reprompt" in outcome.reprompt_request.directive
    assert outcome.classified_intent == "combat_reprompt"


def test_already_reprompted_degrades_reprompt_to_warn(pack, monkeypatch) -> None:
    import sidequest.telemetry.spans as spans_mod

    snap = _snapshot()
    result = _result(intent="strike again", confrontation=None)
    room = MagicMock()
    spans: list[dict] = []
    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", _capture(spans))

    outcome = _apply(snap, result, "Player1", room=room, pack=pack, already_reprompted=True)

    assert outcome.reprompt_request is None  # degraded
    assert spans[0]["severity"] == "warn"
    assert spans[0]["reprompt_attempted"] is True
    assert outcome.classified_intent == "combat_reprompt"


def test_active_encounter_short_circuits_validator(pack, monkeypatch) -> None:
    """When an encounter is live the validator returns None; no span fires."""
    import sidequest.telemetry.spans as spans_mod

    snap = _snapshot()
    # Use a real StructuredEncounter if convenient, or MagicMock that
    # answers `resolved=False`. The validator only reads truthiness +
    # `.resolved`, so a stub is fine.
    snap.encounter = MagicMock(resolved=False)
    result = _result(intent="strike the bandit", confrontation=None)
    room = MagicMock()
    spans: list[dict] = []
    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", _capture(spans))

    outcome = _apply(snap, result, "Player1", room=room, pack=pack)

    assert spans == []
    assert outcome.reprompt_request is None
    # When the validator short-circuits, intent comes from action_rewrite verbatim.
    assert outcome.classified_intent == "strike the bandit"


def test_empty_intent_classifies_as_unspecified(pack, monkeypatch) -> None:
    import sidequest.telemetry.spans as spans_mod

    snap = _snapshot()
    result = _result(intent="", confrontation=None)
    room = MagicMock()
    monkeypatch.setattr(spans_mod, "confrontation_intent_mismatch_span", _capture([]))

    outcome = _apply(snap, result, "Player1", room=room, pack=pack)

    assert outcome.classified_intent == "unspecified"
    assert outcome.classified_intent != "unknown"


def test_non_narration_turn_result_classifies_as_unspecified(pack) -> None:
    """Early-return path (when result isn't a NarrationTurnResult)."""
    from sidequest.server import narration_apply

    snap = _snapshot()
    room = MagicMock()
    outcome = narration_apply._apply_narration_result_to_snapshot(
        snap, "not a turn result", "Player1", room=room, pack=pack
    )
    assert outcome.classified_intent == "unspecified"
    assert outcome.reprompt_request is None


# ---------------------------------------------------------------------------
# Internal helper — keeps test bodies terse
# ---------------------------------------------------------------------------


def _apply(snap, result, player, **kwargs):
    from sidequest.server import narration_apply

    return narration_apply._apply_narration_result_to_snapshot(snap, result, player, **kwargs)
