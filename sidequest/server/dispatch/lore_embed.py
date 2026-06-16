"""Lore RAG retrieval and embed-worker dispatch.

Phase 3 of the session_handler.py decomposition (see
docs/superpowers/specs/2026-04-27-session-handler-decomposition-design.md).

Each function takes ``handler: WebSocketSessionHandler`` as its first
argument. No new abstractions introduced — this is pure extraction
with byte-identical behavior to the original methods on
WebSocketSessionHandler.

The ``embed_task`` lifecycle remains on ``_SessionData`` (created by
``dispatch_worker`` here, cancelled by ``WebSocketSessionHandler.cleanup``
in ``session_handler.py``). That asymmetry is intentional — the task
attribute is shared session state, not worker-module state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.lore_embedding import (
    embed_pending_fragments,
    retrieve_lore_context,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData

logger = logging.getLogger(__name__)


async def retrieve_for_turn(
    handler: WebSocketSessionHandler,
    sd: _SessionData,
    action: str,
) -> str | None:
    """Fetch the pre-turn lore block via semantic search.

    Always returns ``None`` on empty stores, missing daemons, or
    embed failures — the narrator will run without RAG injection,
    which is strictly better than crashing the turn. Expected failure
    modes (empty store, daemon unavailable, embed error, query too
    large) are logged inside :func:`retrieve_lore_context` and surface
    their own OTEL span attribute. The blanket ``except Exception``
    below exists precisely for paths those guards do not cover (e.g.
    a malformed daemon reply that raises ``KeyError`` from
    ``EmbedResponse`` construction) so a buggy codepath never crashes
    the turn.
    """
    try:
        return await retrieve_lore_context(sd.lore_store, action)
    except Exception as exc:  # noqa: BLE001 — RAG must never crash a turn
        logger.warning(
            "lore_retrieval.unexpected_exception action_len=%d error=%s",
            len(action),
            exc,
        )
        _watcher_publish(
            "state_transition",
            {
                "field": "lore_retrieval",
                "op": "failed",
                "reason": "unexpected_exception",
                "error": type(exc).__name__,
            },
            component="lore",
            severity="error",
        )
        return None


async def run_worker(
    handler: WebSocketSessionHandler,
    sd: _SessionData,
    pending_count: int,
    turn_number: int,
) -> None:
    """Background embed worker — never raises, always emits telemetry."""
    try:
        result = await embed_pending_fragments(sd.lore_store)
    except Exception as exc:  # noqa: BLE001 — worker cannot crash the loop
        logger.exception("lore_embedding.worker_exception")
        _watcher_publish(
            "state_transition",
            {
                "field": "lore_embedding",
                "op": "failed",
                "reason": "exception",
                "error": type(exc).__name__,
                "turn_number": turn_number,
            },
            component="lore",
            severity="error",
        )
        return
    _watcher_publish(
        "state_transition",
        {
            "field": "lore_embedding",
            "op": "completed",
            "pending_at_dispatch": pending_count,
            "turn_number": turn_number,
            **result.as_dict(),
        },
        component="lore",
    )


def dispatch_worker(handler: WebSocketSessionHandler, sd: _SessionData) -> None:
    """Spawn a background embed worker for any newly-added lore.

    Fire-and-forget, but lifecycle-tracked. The worker itself checks
    :meth:`DaemonClient.is_available` before opening any connection
    and returns early with ``skipped_daemon_unavailable=True`` when
    the sidecar is absent — matching the render-dispatch graceful
    degradation pattern.

    Double-dispatch gate: if a previous worker for this session is
    still running, skip this turn's dispatch. The next turn will pick
    up the remaining pending fragments. This prevents two concurrent
    workers from racing at the ``await client.embed()`` yield point
    and double-incrementing the retry counter on the same fragment.
    """
    tracer = trace.get_tracer("sidequest.server.session_handler")
    previous = sd.embed_task
    if previous is not None and not previous.done():
        # Emit a span for the skip so the GM panel's OTEL audit trail
        # shows it alongside the worker's own ``lore_embedding.worker``
        # span. Watcher event stays as well for the live state_transition
        # stream.
        with tracer.start_as_current_span("lore_embedding.dispatch_skipped") as skip_span:
            skip_span.set_attribute("lore.skip_reason", "worker_still_running")
            skip_span.set_attribute("lore.turn_number", sd.snapshot.turn_manager.interaction)
        _watcher_publish(
            "state_transition",
            {
                "field": "lore_embedding",
                "op": "skipped",
                "reason": "worker_still_running",
                "turn_number": sd.snapshot.turn_manager.interaction,
            },
            component="lore",
        )
        return
    pending = sd.lore_store.pending_embedding_ids(max_retries=3)
    if not pending:
        return
    turn_number = sd.snapshot.turn_manager.interaction
    sd.embed_task = asyncio.create_task(run_worker(handler, sd, len(pending), turn_number))
