"""RED tests — Story 59-33: ``yield_side`` end-to-end OTEL wiring (opponent + dial).

The 49-5 [ENCOUNTER RESOLVED] narrator consumer is DORMANT (``TurnContext.
pending_resolution_signal`` is never populated — copy code deleted in 49-5; see
TEA prep finding), so a consume-side test would be vacuous. Per Keith's Option-C
ruling the LIVE consumer is the ``encounter.resolution_signal_emitted`` OTEL span
(the GM-panel lie-detector). These tests drive the REAL resolution paths through
the production entry (``_apply_narration_result_to_snapshot``) and assert the
EMITTED span carries ``yield_side`` — the CLAUDE.md "non-test consumer reachable
from production" wiring test (path 1: OTEL span assertion through the real flow).

Covers the opponent-yield and dial-threshold paths here; the player-yield path
(``handle_yield``, needs the PG harness) is in
``tests/server/test_59_33_player_yield_span.py``.

RED today: the field is absent (``snapshot.pending_resolution_signal.yield_side``
→ AttributeError) and the ``encounter.resolution_signal_emitted`` span is emitted
only at the player-yield site — NOT at the opponent-yield or dial/factory paths.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

_SIGNAL_EMITTED_SPAN = "encounter.resolution_signal_emitted"


def _encounter(
    *,
    opponents: list[EncounterActor],
    players: list[EncounterActor] | None = None,
    win_condition: str = "dial_threshold",
    player_current: int = 2,
    threshold: int = 8,
) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="standoff",
        win_condition=win_condition,  # type: ignore[arg-type]
        player_metric=EncounterMetric(
            name="resolve", current=player_current, starting=0, threshold=threshold
        ),
        opponent_metric=EncounterMetric(name="menace", current=1, starting=0, threshold=threshold),
        actors=[
            *(players or [EncounterActor(name="Dorothy", role="lead", side="player")]),
            *opponents,
        ],
    )


def _lion(withdrawn: bool = True) -> EncounterActor:
    return EncounterActor(
        name="The Cowardly Lion", role="aggressor", side="opponent", withdrawn=withdrawn
    )


def _emitted_signal_spans(otel_capture) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == _SIGNAL_EMITTED_SPAN]


# ── opponent-yield → yield_side="opponent" ────────────────────────────────────


def test_opponent_yield_emits_signal_span_with_yield_side_opponent(otel_capture) -> None:
    """Drive an opponent yield through the real apply path; assert the snapshot
    signal AND the emitted resolution OTEL span both carry yield_side='opponent'."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=True)])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(narration="The Lion backs away whimpering.", beat_selections=[]),
        player_name="Dorothy",
        room=room_for(snap),
    )

    sig = snap.pending_resolution_signal
    assert sig is not None
    assert sig.yield_side == "opponent", f"snapshot signal yield_side; got {sig.yield_side!r}"

    spans = _emitted_signal_spans(otel_capture)
    assert len(spans) == 1, (
        f"opponent-yield must emit one {_SIGNAL_EMITTED_SPAN} span (the live "
        f"GM-panel consumer); got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert dict(spans[0].attributes or {}).get("yield_side") == "opponent"


# ── dial-threshold → yield_side=None ──────────────────────────────────────────


def test_dial_threshold_emits_signal_span_with_yield_side_none(otel_capture) -> None:
    """Drive a dial-threshold (player metric at threshold, opponent still active →
    NOT a yield) through the real apply path; the snapshot signal and the emitted
    span carry yield_side=None (a dial win is not a yield)."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(
        opponents=[_lion(withdrawn=False)],  # still standing → no opponent yield
        player_current=8,
        threshold=8,  # player dial AT threshold → dial_threshold resolution
    )

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Dorothy's resolve carries the standoff.", beat_selections=[]
        ),
        player_name="Dorothy",
        room=room_for(snap),
    )

    sig = snap.pending_resolution_signal
    assert sig is not None
    assert sig.outcome == "player_victory"
    assert sig.yield_side is None, f"a dial win is not a yield; got {sig.yield_side!r}"

    spans = _emitted_signal_spans(otel_capture)
    assert len(spans) == 1, (
        f"dial-threshold resolution must emit one {_SIGNAL_EMITTED_SPAN} span; "
        f"got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert dict(spans[0].attributes or {}).get("yield_side") is None
