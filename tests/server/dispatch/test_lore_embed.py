"""Unit + wiring tests for sidequest/server/dispatch/lore_embed.py.

Phase 3 of session_handler decomposition. These tests verify:
1. Each extracted function exists with the expected signature.
2. The thin delegate methods on WebSocketSessionHandler still call
   into lore_embed.py (wiring guard per CLAUDE.md).
3. Behavior is preserved (functional parity with the pre-extraction
   methods) — supplemented by the canonical end-to-end wiring guard
   in tests/server/test_lore_rag_wiring.py which continues to
   exercise the full pipeline through the delegates.
"""

from __future__ import annotations

import pytest


def test_lore_embed_module_exposes_required_functions() -> None:
    """Wiring guard — the three required functions must be importable
    from sidequest.server.dispatch.lore_embed by their canonical names.

    DO NOT MODIFY this test until the last extraction (Task 4) lands.
    It is INTENTIONALLY RED until then — the epic-level RED→GREEN gate
    that proves all three moves completed.
    """
    from sidequest.server.dispatch import lore_embed

    assert hasattr(lore_embed, "retrieve_for_turn")
    assert hasattr(lore_embed, "dispatch_worker")
    assert hasattr(lore_embed, "run_worker")


@pytest.mark.asyncio
async def test_retrieve_for_turn_delegate_calls_module_function(
    monkeypatch, session_handler_factory
) -> None:
    """Wiring guard — WebSocketSessionHandler._retrieve_lore_for_turn
    must delegate to lore_embed.retrieve_for_turn."""
    from sidequest.server.dispatch import lore_embed

    sd, handler = session_handler_factory()
    captured: list[tuple] = []
    sentinel: str | None = "<lore-block-sentinel>"

    async def _spy(h, sd_arg, action):
        captured.append((h, sd_arg, action))
        return sentinel

    monkeypatch.setattr(lore_embed, "retrieve_for_turn", _spy)

    result = await handler._retrieve_lore_for_turn(sd, "look around")

    assert result == sentinel
    assert captured == [(handler, sd, "look around")]


@pytest.mark.asyncio
async def test_retrieve_for_turn_returns_none_on_unexpected_exception(
    monkeypatch, session_handler_factory
) -> None:
    """Behavioral test — when retrieve_lore_context raises an unexpected
    exception, retrieve_for_turn must swallow it, log a warning, emit a
    failure watcher event, and return None. The turn must never crash
    on RAG failure (CLAUDE.md "No Silent Fallbacks" carve-out: the
    fallback is loud-via-OTEL, silent-to-the-caller-by-design)."""
    from sidequest.server.dispatch import lore_embed

    sd, handler = session_handler_factory()

    async def _boom(*args, **kwargs):
        raise KeyError("simulated malformed embed response")

    # Patch the caller's namespace, not sidequest.game.lore_embedding —
    # see "Critical note on monkeypatch target" above.
    monkeypatch.setattr(lore_embed, "retrieve_lore_context", _boom)

    captured_events: list[tuple] = []

    def _capture(event_kind, payload, component=None, severity=None):
        captured_events.append((event_kind, payload, component, severity))

    monkeypatch.setattr(lore_embed, "_watcher_publish", _capture)

    result = await lore_embed.retrieve_for_turn(handler, sd, "look around")

    assert result is None
    assert len(captured_events) == 1
    kind, payload, component, severity = captured_events[0]
    assert kind == "state_transition"
    assert payload["field"] == "lore_retrieval"
    assert payload["op"] == "failed"
    assert payload["reason"] == "unexpected_exception"
    assert payload["error"] == "KeyError"
    assert component == "lore"
    assert severity == "error"


@pytest.mark.asyncio
async def test_run_worker_delegate_calls_module_function(
    monkeypatch, session_handler_factory
) -> None:
    """Wiring guard — WebSocketSessionHandler._run_embed_worker
    must delegate to lore_embed.run_worker."""
    from sidequest.server.dispatch import lore_embed

    sd, handler = session_handler_factory()
    captured: list[tuple] = []

    async def _spy(h, sd_arg, pending_count, turn_number):
        captured.append((h, sd_arg, pending_count, turn_number))

    monkeypatch.setattr(lore_embed, "run_worker", _spy)

    await handler._run_embed_worker(sd, 7, 42)

    assert captured == [(handler, sd, 7, 42)]


def test_dispatch_worker_delegate_calls_module_function(
    monkeypatch, session_handler_factory
) -> None:
    """Wiring guard — WebSocketSessionHandler._dispatch_embed_worker
    must delegate to lore_embed.dispatch_worker."""
    from sidequest.server.dispatch import lore_embed

    sd, handler = session_handler_factory()
    captured: list[tuple] = []

    def _spy(h, sd_arg):
        captured.append((h, sd_arg))

    monkeypatch.setattr(lore_embed, "dispatch_worker", _spy)

    handler._dispatch_embed_worker(sd)

    assert captured == [(handler, sd)]
