"""Wiring + OTEL + replay-invariant for the QUESTS projection (Story 77-8).

RED-phase contract (TEA). Three load-bearing guards beyond the unit behavior in
``test_quests_emit.py``:

  1. OTEL (the GM-panel lie-detector, per CLAUDE.md): emitting a roster fires a
     ``quests.emitted`` span so the panel can prove the projection engaged rather
     than the narrator improvising the spine.
  2. Wiring (CLAUDE.md "Verify Wiring, Not Just Existence" — no source-text grep):
     the emitter is imported into the real ``websocket_session_handler`` module
     namespace, so a projection that exists but is never called from the
     production NARRATION_END path fails this test.
  3. Replay safety: QUESTS is a TRANSIENT broadcast (like LOCATION_DESCRIPTION /
     RELATIONSHIPS) — never event-sourced, so it must be absent from BOTH the
     replay constructor map and the replay-skip set (No Silent Fallbacks: a stray
     persisted row fails loud, it is not silently reconstructed).

Imports symbols that do not exist yet — FAIL until Dev (77-8 GREEN) lands them.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as _spans_module
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.server.websocket_handlers.quests_emit import _maybe_emit_quests


def _seeded() -> GameSnapshot:
    return GameSnapshot(
        quest_log={
            "q1": QuestEntry(
                title="Go Home",
                objective="Return to Kansas",
                status="active",
                anchor_id="emerald_city",
            )
        },
        quest_anchors=["emerald_city"],
        active_stakes="The witch hunts you",
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
    local = provider.get_tracer("test-quests")
    monkeypatch.setattr(_spans_module, "tracer", lambda: local)
    return _SpanCapture(exporter)


def test_emit_fires_quests_emitted_span(capture_spans) -> None:
    """OTEL: a real emit lights the quests.emitted span (GM-panel lie-detector)."""
    handler = type("H", (), {})()
    _maybe_emit_quests(handler, snapshot=_seeded(), emit_fn=lambda m, k: None)
    assert any(s.name == "quests.emitted" for s in capture_spans.spans), (
        "expected a 'quests.emitted' span to fire on projection emit"
    )


def test_emitter_is_wired_into_session_handler() -> None:
    """Wiring (AC4): the production handler module imported the emitter, so the
    projection is reachable from the real shared-world-frame emit path — exactly
    as ``_maybe_emit_relationships`` is wired at websocket_session_handler.py."""
    import sidequest.server.websocket_session_handler as wsh

    wired = getattr(wsh, "_maybe_emit_quests", None)
    assert wired is _maybe_emit_quests, (
        "_maybe_emit_quests is not imported into websocket_session_handler — "
        "the projection exists but is not wired into the production emit path"
    )


def test_quests_is_transient_like_location_description() -> None:
    """Replay safety (AC4): QUESTS is a transient shared-world frame, neither
    replayed nor skipped — identical to its sibling LOCATION_DESCRIPTION."""
    from sidequest.server.session_handler import (
        _KIND_TO_MESSAGE_CLS,
        _REPLAY_SKIP_KINDS,
    )

    # Canonical transient sibling — the invariant we mirror.
    assert "LOCATION_DESCRIPTION" not in _KIND_TO_MESSAGE_CLS
    assert "LOCATION_DESCRIPTION" not in _REPLAY_SKIP_KINDS
    # QUESTS must match it: not event-sourced, so not reconstructable.
    assert "QUESTS" not in _KIND_TO_MESSAGE_CLS
    assert "QUESTS" not in _REPLAY_SKIP_KINDS
