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


def _referenced_names(code: object) -> set[str]:
    """All global/attribute names referenced by a code object, recursing into
    nested code objects (comprehensions, closures, inner defs). Reflection over
    the COMPILED code object — not a source-text grep (the allowed exception in
    CLAUDE.md 'No Source-Text Wiring Tests')."""
    names: set[str] = set(getattr(code, "co_names", ()))
    for const in getattr(code, "co_consts", ()):
        if hasattr(const, "co_names"):  # nested code object
            names |= _referenced_names(const)
    return names


def test_emitter_is_called_from_narration_turn() -> None:
    """Wiring (AC4 — the load-bearing one): prove the emitter is actually CALLED
    from the production NARRATION_END handler, not merely imported. The prior
    import-only check passed even if the call site were deleted; this fails if
    the call is removed.

    ``_maybe_emit_quests`` is invoked inside
    ``WebSocketSessionHandler._execute_narration_turn`` (the real per-turn
    handler). We introspect that method's compiled code object (recursing nested
    code objects) and assert the emitter name is referenced. Refactor-stable
    (survives line moves/renames of surrounding code), reflection-based (not a
    source grep), and catches the exact regression this story exists to prevent:
    a projection that is wired-but-never-called.
    """
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

    referenced = _referenced_names(WebSocketSessionHandler._execute_narration_turn.__code__)
    assert "_maybe_emit_quests" in referenced, (
        "_maybe_emit_quests is never called from _execute_narration_turn — the "
        "QUESTS projection is not reachable from the production NARRATION_END path"
    )
    # Sanity anchor: the relationships sibling is wired the same way; if this
    # assertion ever fails the introspection target moved, not the quests wiring.
    assert "_maybe_emit_relationships" in referenced


def test_quests_message_is_in_game_message_union() -> None:
    """REWORK (Reviewer test-analyzer): positive routability assertion. The
    transient-replay test only asserts QUESTS is ABSENT from the replay maps;
    this asserts QuestsMessage is PRESENT in the GameMessage discriminated union,
    so the message is actually routable on the wire."""
    from sidequest.protocol.messages import GameMessage, QuestsMessage

    # GameMessage is a RootModel wrapping an Annotated[Union[...], discriminator].
    union = GameMessage.model_fields["root"].annotation
    members = getattr(union, "__args__", ())
    assert QuestsMessage in members, (
        "QuestsMessage is not a member of the GameMessage union — it would be "
        "unroutable on the client side"
    )


def test_emit_fires_on_first_call_for_fresh_handler() -> None:
    """REWORK (Reviewer test-analyzer): a freshly-instantiated handler (e.g. a
    reconnect) has no cached signature, so the first call MUST emit even though
    the spine matches a prior session's last-emitted state. Guards against an
    accidental pre-seeded _SIG_ATTR suppressing the reconnect broadcast."""
    snap = _seeded()
    sent_a: list[object] = []
    sent_b: list[object] = []
    _maybe_emit_quests(type("H", (), {})(), snapshot=snap, emit_fn=lambda m, k: sent_a.append(m))
    _maybe_emit_quests(type("H", (), {})(), snapshot=snap, emit_fn=lambda m, k: sent_b.append(m))
    assert len(sent_a) == 1 and len(sent_b) == 1
