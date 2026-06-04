"""Entity card embedding worker (Story 75-6, ADR-118 §D2/§D3).

The sibling of :func:`sidequest.game.lore_embedding.embed_pending_fragments` for
the universal-retrieval :class:`~sidequest.game.entity_store.EntityStore`. 75-6's
reproject writes cards with ``embedding_pending=True``; without a drain those
cards are never embedded and stay invisible to ``query_by_similarity`` (which
skips ``embedding is None``). This worker calls the daemon once per pending card
and writes the vector back via :meth:`EntityStore.update_embedding`, reusing the
exact MiniLM/cosine path lore already uses — no new model, no migration.

Degrades gracefully (ADR-006): a missing daemon returns early with
``skipped_daemon_unavailable=True`` and the cards stay pending for the next pass;
the narrator never blocks on entity embedding. The :class:`EmbedWorkerResult`
telemetry type is shared with the lore worker so the GM panel reads one shape.
"""

from __future__ import annotations

import logging

from opentelemetry import trace

from sidequest.daemon_client import (
    DaemonClient,
    DaemonRequestError,
    DaemonUnavailableError,
)
from sidequest.game.entity_store import EntityStore
from sidequest.game.lore_embedding import DEFAULT_MAX_RETRIES, EmbedWorkerResult

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("sidequest.game.entity_embedding")


async def embed_pending_entity_cards(
    store: EntityStore,
    client: DaemonClient | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_per_run: int | None = None,
) -> EmbedWorkerResult:
    """Embed cards with ``embedding_pending=True`` via the daemon.

    Mirrors :func:`embed_pending_fragments`: fire-and-forget after a turn,
    returns an :class:`EmbedWorkerResult` for OTEL emission by the caller. If
    ``client`` is ``None`` a fresh :class:`DaemonClient` is built; an absent
    socket returns early with ``skipped_daemon_unavailable=True`` (no connect
    attempt). ``max_retries`` caps per-card transient-failure retries so one
    poisoned card cannot burn embed budget every turn; ``max_per_run`` bounds the
    cards processed in one pass.
    """
    result = EmbedWorkerResult()
    with tracer.start_as_current_span("entity_embedding.worker") as span:
        pending = store.pending_embedding_ids(max_retries=max_retries)
        span.set_attribute("entity.pending_count", len(pending))
        if not pending:
            result.skipped_empty_queue = True
            span.set_attribute("entity.skipped", "empty_queue")
            return result

        if client is None:
            client = DaemonClient()
        if not client.is_available():
            result.skipped_daemon_unavailable = True
            span.set_attribute("entity.skipped", "daemon_unavailable")
            logger.warning(
                "entity_embedding.worker skipped reason=daemon_unavailable pending=%d",
                len(pending),
            )
            return result

        if max_per_run is not None:
            pending = pending[: max(0, max_per_run)]
            span.set_attribute("entity.max_per_run", max_per_run)

        # The first successful embed pins the session's expected dimension; a
        # later embed returning a different-dim vector (daemon hot-reloaded its
        # model mid-run) is refused at write-back, mirroring the lore worker.
        expected_dim: int | None = None
        for card_id in pending:
            card = store.cards.get(card_id)
            if card is None:
                # Dropped between pending_ids() and now — skip, counters untouched.
                continue
            try:
                response = await client.embed(card.content)
            except DaemonUnavailableError as exc:
                # Daemon went away mid-run. Stop loud; remaining cards stay pending.
                logger.warning(
                    "entity_embedding.worker daemon_unavailable mid_run card=%s error=%s",
                    card_id,
                    exc,
                )
                span.set_attribute("entity.early_exit", "daemon_unavailable_mid_run")
                result.skipped_daemon_unavailable = True
                break
            except DaemonRequestError as exc:
                store.mark_embedding_failed(card_id)
                result.failed_embed_error += 1
                span.add_event(
                    "embed_failed",
                    {
                        "card_id": card_id,
                        "reason": "daemon_error",
                        "code": exc.code,
                        "retry_count": card.embedding_retry_count,
                    },
                )
                logger.warning(
                    "entity_embedding.worker embed_failed card=%s code=%s retry_count=%d",
                    card_id,
                    exc.code,
                    card.embedding_retry_count,
                )
                continue
            except ValueError as exc:
                # MAX_EMBED_BYTES guard in the client — card content too large.
                store.mark_embedding_failed(card_id)
                result.failed_text_too_large += 1
                content_bytes = len(card.content.encode("utf-8"))
                span.add_event(
                    "embed_failed",
                    {
                        "card_id": card_id,
                        "reason": "text_too_large",
                        "content_bytes": content_bytes,
                    },
                )
                logger.warning(
                    "entity_embedding.worker text_too_large card=%s content_bytes=%d error=%s",
                    card_id,
                    content_bytes,
                    type(exc).__name__,
                )
                continue
            embedding = response["embedding"]
            if expected_dim is None:
                expected_dim = len(embedding)
            written = store.update_embedding(card_id, embedding, expected_dim=expected_dim)
            if not written:
                # Dim mismatch against the session's expected dim: the daemon
                # switched models mid-run (or a retrieve-time requeue raced this
                # embed). Keep the card pending and let the next worker pass
                # re-fetch on the new dim — never write a stale-dim vector back.
                result.failed_embed_error += 1
                span.add_event(
                    "embed_failed",
                    {
                        "card_id": card_id,
                        "reason": "dim_mismatch_writeback_refused",
                        "written_dim": len(embedding),
                        "expected_dim": expected_dim,
                    },
                )
                logger.warning(
                    "entity_embedding.worker writeback_refused card=%s "
                    "written_dim=%d expected_dim=%d",
                    card_id,
                    len(embedding),
                    expected_dim,
                )
                continue
            result.embedded += 1
        span.set_attribute("entity.embedded", result.embedded)
        span.set_attribute("entity.failed", result.failed)
    return result
