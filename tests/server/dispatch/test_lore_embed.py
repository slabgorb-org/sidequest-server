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


@pytest.mark.asyncio
async def test_dispatch_worker_spawns_on_entity_only_turn(session_handler_factory) -> None:
    """Story 75-6 regression guard (ADR-118 §D2 silent-revert risk).

    ``dispatch_worker`` MUST spawn the background embed task when a turn
    reprojects an entity card but accretes NO lore — the "entity-only turn".
    The 75-6 hardening widened the dispatch gate from ``if not pending``
    to ``if not pending and not entity_pending`` (the ``pending`` /
    ``entity_pending`` locals in ``lore_embed.dispatch_worker``). If that
    ``entity_pending`` arm is ever reverted, entity cards left
    ``embedding_pending=True`` would
    never be drained by ``run_worker`` and would stay invisible to
    ``query_by_similarity`` — a silent retrieval-coverage regression with no
    crash to flag it. This test pins the behavior so the revert fails loudly.
    """
    import asyncio
    import contextlib

    from sidequest.game.disposition import Disposition
    from sidequest.game.npc_pool import NpcPoolMember
    from sidequest.server.dispatch import entity_sync, lore_embed

    sd, handler = session_handler_factory(genre="caverns_and_claudes")

    # Build the entity-only turn: a seed pool member projected into the entity
    # store (embedding_pending=True via sync_for_turn), with NO lore accreted.
    sd.snapshot.npc_pool.append(
        NpcPoolMember(
            name="Borin",
            role="smith",
            pronouns="they/them",
            drawn_from="world_authored",
            disposition=Disposition(0),
        )
    )
    entity_sync.sync_for_turn(handler, sd)

    # Precondition — exactly the state the 75-6 gate exists to catch: the entity
    # arm has pending work, the lore arm is empty, nothing dispatched yet. Pin the
    # specific seeded card (not a bare truthy check) so a fixture that projects the
    # wrong card — or zero cards — can't masquerade as the entity-only state.
    assert "npc:borin" in sd.entity_store.pending_embedding_ids(max_retries=3), (
        "fixture must leave the seeded Borin entity card pending embedding"
    )
    assert sd.lore_store.pending_embedding_ids(max_retries=3) == [], (
        "fixture must leave NO lore pending — this is the entity-only path"
    )
    assert sd.embed_task is None

    lore_embed.dispatch_worker(handler, sd)

    # The gate must fire on entity_pending alone. A revert to lore-only gating
    # leaves embed_task unset → entity cards never embed.
    assert isinstance(sd.embed_task, asyncio.Task), (
        "dispatch_worker must spawn the embed task on an entity-only turn; "
        "a revert to lore-only gating would leave embed_task unset and entity "
        "cards permanently unembedded (ADR-118 §D2 silent-revert guard)"
    )

    # Drain the fire-and-forget worker deterministically, without coupling the
    # assertion above to daemon availability inside run_worker. Suppressing
    # CancelledError is safe here: we cancelled the task ourselves, and only
    # *after* the load-bearing assertion above already passed — the task's
    # outcome is irrelevant at this point; we await it solely so the event loop
    # settles the cancellation and leaves no dangling task for sibling tests.
    sd.embed_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sd.embed_task
