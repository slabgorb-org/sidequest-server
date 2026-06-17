"""RED tests for Story 118-1 (F3a) — wiring + OTEL + replay-invariant for FATE_STATE.

Three load-bearing guards beyond the unit behavior in ``test_fate_state_emit.py``
(mirrors ``test_quests_emit_wiring.py``):

  1. OTEL (the GM-panel lie-detector, CLAUDE.md): a real emit fires a
     ``fate.projection.emitted`` span so the panel can prove the projection engaged
     rather than the narrator improvising a Fate surface.
  2. Wiring (CLAUDE.md "Verify Wiring, Not Just Existence" — reflection, not a
     source-text grep): the emitter is imported into the real
     ``websocket_session_handler`` module AND actually CALLED from
     ``_execute_narration_turn`` (the per-turn NARRATION_END path).
  3. Replay safety: FATE_STATE is a TRANSIENT broadcast (like LOCATION_DESCRIPTION /
     RELATIONSHIPS / QUESTS) — never event-sourced, so it must be absent from BOTH
     the replay constructor map and the replay-skip set.

Imports symbols that do not exist yet — FAIL until Dev (118-1 GREEN) lands them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as _spans_module
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot


def _sd(ruleset: str = "fate"):
    return SimpleNamespace(genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset=ruleset)))


def _snapshot() -> GameSnapshot:
    sheet = FateSheet(skills={"Fight": 3}, fate_points=3)
    sheet.aspects.append(Aspect(text="Last Honest Cop in Vega", kind="high_concept"))
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[EncounterActor(name="Vance", role="lead", side="player")],
    )
    return GameSnapshot(
        genre_slug="pulp_noir",
        characters=[
            Character(
                core=CreatureCore(name="Vance", description="d", personality="p", fate_sheet=sheet),
                char_class="Agent",
                race="Human",
                backstory="b",
            )
        ],
        encounter=enc,
    )


class _SpanCapture:
    def __init__(self, exporter: InMemorySpanExporter):
        self._exporter = exporter

    @property
    def spans(self):
        return list(self._exporter.get_finished_spans())


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-fate-state")
    monkeypatch.setattr(_spans_module, "tracer", lambda: local)
    return _SpanCapture(exporter)


# ---------------------------------------------------------------------------
# 1 — OTEL: the GM-panel lie-detector span.
# ---------------------------------------------------------------------------


def test_emit_fires_fate_projection_emitted_span(capture_spans) -> None:
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    handler = type("H", (), {})()
    _maybe_emit_fate_state(handler, sd=_sd("fate"), snapshot=_snapshot(), emit_fn=lambda m, k: None)
    assert any(s.name == "fate.projection.emitted" for s in capture_spans.spans), (
        "expected a 'fate.projection.emitted' span to fire on projection emit"
    )


def test_no_span_when_gated_off(capture_spans) -> None:
    """The gate is upstream of the span: a native pack emits neither a frame nor a
    span (no phantom GM-panel evidence for a surface that never rendered)."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    handler = type("H", (), {})()
    _maybe_emit_fate_state(handler, sd=_sd("dial"), snapshot=_snapshot(), emit_fn=lambda m, k: None)
    assert not any(s.name == "fate.projection.emitted" for s in capture_spans.spans)


# ---------------------------------------------------------------------------
# 2 — Wiring: imported into the production handler AND called per turn.
# ---------------------------------------------------------------------------


def test_emitter_is_wired_into_session_handler() -> None:
    import sidequest.server.websocket_session_handler as wsh
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    wired = getattr(wsh, "_maybe_emit_fate_state", None)
    assert wired is _maybe_emit_fate_state, (
        "_maybe_emit_fate_state is not imported into websocket_session_handler — "
        "the projection exists but is not wired into the production emit path"
    )


def _referenced_names(code: object) -> set[str]:
    """All names referenced by a compiled code object, recursing nested code
    objects (the reflection-based wiring check allowed by CLAUDE.md — NOT a
    source-text grep)."""
    names: set[str] = set(getattr(code, "co_names", ()))
    for const in getattr(code, "co_consts", ()):
        if hasattr(const, "co_names"):
            names |= _referenced_names(const)
    return names


def test_emitter_is_called_from_narration_turn() -> None:
    """The load-bearing wiring guard: prove the emitter is CALLED from the per-turn
    handler, not merely imported. Refactor-stable, reflection-based."""
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

    referenced = _referenced_names(WebSocketSessionHandler._execute_narration_turn.__code__)
    assert "_maybe_emit_fate_state" in referenced, (
        "_maybe_emit_fate_state is never called from _execute_narration_turn — the "
        "FATE_STATE projection is not reachable from the production NARRATION_END path"
    )
    # Sanity anchor: the quests sibling is wired the same way; if this fails the
    # introspection target moved, not the fate wiring.
    assert "_maybe_emit_quests" in referenced


# ---------------------------------------------------------------------------
# 3 — Replay safety: FATE_STATE is transient (neither replayed nor skipped).
# ---------------------------------------------------------------------------


def test_fate_state_is_transient_like_location_description() -> None:
    from sidequest.server.session_handler import _KIND_TO_MESSAGE_CLS, _REPLAY_SKIP_KINDS

    # Canonical transient sibling — the invariant we mirror.
    assert "LOCATION_DESCRIPTION" not in _KIND_TO_MESSAGE_CLS
    assert "LOCATION_DESCRIPTION" not in _REPLAY_SKIP_KINDS
    # FATE_STATE must match it: not event-sourced, so not reconstructable.
    assert "FATE_STATE" not in _KIND_TO_MESSAGE_CLS
    assert "FATE_STATE" not in _REPLAY_SKIP_KINDS
