"""Lore embedding worker and RAG retrieval — Story 37-33.

Wires :class:`sidequest.daemon_client.DaemonClient.embed` into the
lore pipeline:

- :func:`embed_pending_fragments` drains fragments whose
  ``embedding_pending`` flag is set, calling the daemon once per
  fragment and writing back the resulting vector via
  :meth:`LoreStore.update_embedding`.
- :func:`retrieve_lore_context` embeds the player's action (or any
  query text) and returns the top-k most similar fragments formatted
  as a prompt section for the narrator.

Both helpers degrade gracefully when the daemon is unavailable or
returns a structured error — the narrator always runs, it just runs
without the RAG context injection on that turn. CLAUDE.md's "No
Silent Fallbacks" rule applies to the behavioural contract, not to
optional-sidecar unavailability: daemon absence is logged loudly and
surfaced through OTEL attributes, never masked.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from opentelemetry import trace

from sidequest.daemon_client import (
    DaemonClient,
    DaemonRequestError,
    DaemonUnavailableError,
)
from sidequest.game.lore_store import LoreFragment, LoreStore

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("sidequest.game.lore_embedding")

DEFAULT_MAX_RETRIES = 3
"""Fragments past this retry count are left alone until a future
worker run raises the ceiling or a human intervenes. Keeps a single
poisoned fragment from burning through embed budget every turn."""

DEFAULT_RETRIEVAL_TOP_K = 3
"""Conservative default — RAG results land in the narrator prompt's
Valley zone; beyond ~3 fragments the narrator starts hallucinating
connections that weren't in the query."""

DEFAULT_RETRIEVAL_MIN_SIMILARITY = 0.15
"""Cosine similarity floor for retrieval. MiniLM produces positive
cosine values even for unrelated strings; 0.15 drops obvious noise
while keeping legitimately tangential fragments."""

DEFAULT_FRAGMENT_PREVIEW_CHARS = 240
"""How much of each retrieved fragment to inline into the prompt.
Full fragments would blow the Valley budget — the narrator gets a
preview and can ask the player for more via the story beat."""


# ---------------------------------------------------------------------------
# Embedding worker
# ---------------------------------------------------------------------------


@dataclass
class EmbedWorkerResult:
    """Telemetry for one :func:`embed_pending_fragments` run.

    ``failed_embed_error`` counts daemon-side structured failures
    (:class:`DaemonRequestError`); ``failed_text_too_large`` counts
    client-side byte-cap rejections (:class:`ValueError`). ``failed``
    is exposed as a read-only property returning the sum of the two,
    so the watcher payload's total can never drift from its parts.
    """

    embedded: int = 0
    failed_embed_error: int = 0
    failed_text_too_large: int = 0
    skipped_daemon_unavailable: bool = False
    skipped_empty_queue: bool = False
    # Story 75-15: the embedding model the daemon reported on the first
    # successful reply this run. Surfaced so the GM panel can confirm the SAME
    # real model embedded the corpus that retrieval queries with — a
    # hash-fallback embedder would show a different (or missing) model name.
    embedding_model: str | None = None

    @property
    def failed(self) -> int:
        """Total failure count. Derived — always equals the sum of the
        two sub-counters so the OTEL / watcher telemetry cannot report
        contradictory numbers."""
        return self.failed_embed_error + self.failed_text_too_large

    def as_dict(self) -> dict[str, object]:
        """Watcher payload contract — keys land on the OTEL span and
        the GM panel ``state_transition`` event. The four failure /
        skip fields are the lie-detector signal when the worker reports
        zero embeddings written on a non-empty pending queue.
        """
        return {
            "embedded": self.embedded,
            "failed": self.failed,
            "failed_embed_error": self.failed_embed_error,
            "failed_text_too_large": self.failed_text_too_large,
            "skipped_daemon_unavailable": self.skipped_daemon_unavailable,
            "skipped_empty_queue": self.skipped_empty_queue,
            "embedding_model": self.embedding_model,
        }


async def embed_pending_fragments(
    lore_store: LoreStore,
    client: DaemonClient | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_per_run: int | None = None,
) -> EmbedWorkerResult:
    """Embed fragments with ``embedding_pending=True`` via the daemon.

    Intended to run as a fire-and-forget background task after a
    narration turn. Returns an :class:`EmbedWorkerResult` for OTEL
    emission by the caller.

    If ``client`` is ``None`` a fresh :class:`DaemonClient` is built
    with defaults. If the socket is absent the run returns early with
    ``skipped_daemon_unavailable=True`` — no attempt is made to
    connect, matching the render dispatch pattern.

    :param max_retries: ceiling on per-fragment transient-failure
        retries. Fragments whose ``embedding_retry_count`` has reached
        this value are skipped so a single poisoned fragment cannot
        burn through embed budget every turn. ``None`` removes the
        ceiling — every pending fragment is eligible regardless of
        prior retry count (used by unit tests or manual recovery).
    :param max_per_run: upper bound on fragments processed in this
        run. ``None`` drains the full pending queue. Callers that want
        to amortise embed cost across turns (e.g. chargen seeds 20+
        fragments at once) can set this to a small integer.
    """
    result = EmbedWorkerResult()
    # Capture the daemon's embedding dimension on the first successful
    # reply and pass it to update_embedding() thereafter so the
    # retrieve/worker race (retrieve flips embedding_pending True
    # mid-embed; worker resumes and writes old-dim vector back) cannot
    # silently re-orphan a re-queued fragment.
    expected_dim: int | None = None
    with tracer.start_as_current_span("lore_embedding.worker") as span:
        pending = lore_store.pending_embedding_ids(max_retries=max_retries)
        span.set_attribute("lore.pending_count", len(pending))
        if not pending:
            result.skipped_empty_queue = True
            span.set_attribute("lore.skipped", "empty_queue")
            return result

        if client is None:
            client = DaemonClient()
        if not client.is_available():
            result.skipped_daemon_unavailable = True
            span.set_attribute("lore.skipped", "daemon_unavailable")
            logger.warning(
                "lore_embedding.worker skipped reason=daemon_unavailable pending=%d socket=%s",
                len(pending),
                client.socket_path,
            )
            return result

        if max_per_run is not None:
            pending = pending[: max(0, max_per_run)]
            span.set_attribute("lore.max_per_run", max_per_run)

        for frag_id in pending:
            frag = lore_store.fragments.get(frag_id)
            if frag is None:
                # Dropped between pending_ids() and now — skip silently,
                # counter stays untouched.
                continue
            try:
                response = await client.embed(frag.content)
            except DaemonUnavailableError as exc:
                # Daemon went away mid-run. Stop the loop loudly — the
                # remaining fragments stay pending for the next turn.
                logger.warning(
                    "lore_embedding.worker daemon_unavailable mid_run "
                    "fragment=%s remaining=%d error=%s",
                    frag_id,
                    len(pending) - result.embedded - result.failed,
                    exc,
                )
                span.set_attribute("lore.early_exit", "daemon_unavailable_mid_run")
                result.skipped_daemon_unavailable = True
                break
            except DaemonRequestError as exc:
                # Daemon structured error, or INVALID_RESPONSE raised by
                # DaemonClient.embed's runtime validation of the reply.
                # Both cases charge the retry budget; the structured code
                # lands on the span event for GM-panel triage.
                lore_store.mark_embedding_failed(frag_id)
                result.failed_embed_error += 1
                span.add_event(
                    "embed_failed",
                    {
                        "fragment_id": frag_id,
                        "reason": "daemon_error",
                        "code": exc.code,
                        "retry_count": frag.embedding_retry_count,
                    },
                )
                logger.warning(
                    "lore_embedding.worker embed_failed fragment=%s "
                    "code=%s message=%s retry_count=%d",
                    frag_id,
                    exc.code,
                    exc.message,
                    frag.embedding_retry_count,
                )
                continue
            except ValueError as exc:
                # MAX_EMBED_BYTES guard in the client — the fragment is
                # too large to embed. Mark as failed so we stop trying.
                # (Empty content can't reach this path: LoreFragment.content
                # enforces min_length=1 at Pydantic construction.)
                lore_store.mark_embedding_failed(frag_id)
                result.failed_text_too_large += 1
                span.add_event(
                    "embed_failed",
                    {
                        "fragment_id": frag_id,
                        "reason": "text_too_large",
                        "content_bytes": len(frag.content.encode("utf-8")),
                    },
                )
                logger.warning(
                    "lore_embedding.worker text_too_large fragment=%s content_bytes=%d error=%s",
                    frag_id,
                    len(frag.content.encode("utf-8")),
                    type(exc).__name__,
                )
                continue

            embedding = response["embedding"]
            if result.embedding_model is None:
                # Story 75-15: record the model the daemon reported so the
                # worker's telemetry can prove which embedder filled the corpus.
                result.embedding_model = response.get("model")
            if expected_dim is None:
                expected_dim = len(embedding)
            written = lore_store.update_embedding(frag_id, embedding, expected_dim=expected_dim)
            if not written:
                # Dim mismatch against our session's expected dim. Either
                # the daemon switched models mid-run (server-side hot
                # reload) or a retrieve-time requeue raced with our
                # embed — in either case, keep the fragment pending and
                # let the next worker pass re-fetch on the new dim.
                result.failed_embed_error += 1
                span.add_event(
                    "embed_failed",
                    {
                        "fragment_id": frag_id,
                        "reason": "dim_mismatch_writeback_refused",
                        "written_dim": len(embedding),
                        "expected_dim": expected_dim,
                    },
                )
                logger.warning(
                    "lore_embedding.worker writeback_refused fragment=%s "
                    "written_dim=%d expected_dim=%d",
                    frag_id,
                    len(embedding),
                    expected_dim,
                )
                continue
            result.embedded += 1

        span.set_attribute("lore.embedded", result.embedded)
        span.set_attribute("lore.failed", result.failed)
        span.set_attribute("lore.failed_embed_error", result.failed_embed_error)
        span.set_attribute("lore.failed_text_too_large", result.failed_text_too_large)
        return result


# ---------------------------------------------------------------------------
# RAG retrieval
# ---------------------------------------------------------------------------


async def retrieve_lore_context(
    lore_store: LoreStore,
    query_text: str,
    client: DaemonClient | None = None,
    *,
    top_k: int = DEFAULT_RETRIEVAL_TOP_K,
    min_similarity: float = DEFAULT_RETRIEVAL_MIN_SIMILARITY,
    preview_chars: int = DEFAULT_FRAGMENT_PREVIEW_CHARS,
) -> str | None:
    """Embed ``query_text``, find top-k similar fragments, format them
    for injection into the narrator prompt's Valley zone.

    Returns ``None`` when there is nothing useful to inject (empty
    store, daemon unavailable, embed failure, no fragments above the
    similarity floor). The caller passes ``None`` straight through
    to :class:`TurnContext.lore_context` so no prompt section is
    registered — keeps the prompt zone-clean instead of leaking an
    empty ``<lore>`` block.

    Never raises. All failure paths are logged and return ``None``.
    """
    with tracer.start_as_current_span("lore_embedding.retrieve") as span:
        span.set_attribute("lore.query_len", len(query_text))
        span.set_attribute("lore.store_size", len(lore_store))

        if not query_text.strip() or lore_store.is_empty():
            span.set_attribute("lore.outcome", "empty_query_or_store")
            # Story 75-15 (AC5): emit a panel-visible event for the empty case
            # too, so the GM panel distinguishes "store empty" from "retrieval
            # never ran" (pre-fix this returned before any watcher publish).
            from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

            _watcher_publish(
                "lore_retrieval",
                {
                    "selected": [],
                    "rejected": [],
                    "selected_count": 0,
                    "total_fragments": len(lore_store),
                    "budget": int(top_k),
                    "tokens_used": 0,
                    "min_similarity": float(min_similarity),
                    "outcome": "empty_query_or_store",
                    "peak_similarity": None,
                    "embedding_model": None,
                    "context_hint": (
                        query_text[:80] + "…" if len(query_text) > 80 else query_text
                    ),
                },
                component="lore",
            )
            return None

        if client is None:
            client = DaemonClient()
        if not client.is_available():
            span.set_attribute("lore.outcome", "daemon_unavailable")
            logger.warning("lore_embedding.retrieve skipped reason=daemon_unavailable")
            return None

        try:
            response = await client.embed(query_text)
        except (DaemonUnavailableError, DaemonRequestError) as exc:
            # DaemonRequestError covers both daemon-side structured
            # errors and the client-side runtime validation that
            # converts malformed replies to INVALID_RESPONSE. Any
            # residual KeyError / TypeError from result[...] access
            # cannot occur after round-5's client-side validation
            # (see DaemonClient.embed), so no separate handler here.
            span.set_attribute("lore.outcome", "embed_failed")
            span.set_attribute("lore.error_type", type(exc).__name__)
            span.set_attribute(
                "lore.error_code",
                exc.code if isinstance(exc, DaemonRequestError) else "unavailable",
            )
            logger.warning("lore_embedding.retrieve embed_failed error=%s", exc)
            return None
        except ValueError as exc:
            # Query text exceeded MAX_EMBED_BYTES — truncate? No: the
            # caller should trim before calling. Log and bail.
            span.set_attribute("lore.outcome", "query_too_large")
            logger.warning(
                "lore_embedding.retrieve query_too_large len=%d error=%s",
                len(query_text),
                exc,
            )
            return None

        query_embedding = response["embedding"]
        # Story 75-15 (AC2): the daemon reports which model produced the
        # embedding. Carry it onto the watcher event so a degenerate /
        # hash-fallback embedder is detectable on the GM panel instead of
        # silently starving retrieval.
        embedding_model = response.get("model")
        span.set_attribute("lore.embedding_model", str(embedding_model))

        # Story 75-15 (AC2, No Silent Fallbacks): a zero-magnitude query
        # embedding is the degenerate / hash-fallback smell — cosine scores 0.0
        # against every fragment, indistinguishable from "nothing relevant".
        # Surface it loudly and bail rather than masking a broken embedder.
        # `math.isfinite` guard catches NaN/Inf elements too — `== 0.0` alone
        # would NOT (NaN != 0.0), letting a corrupt embedding poison the cosine
        # math + peak_similarity. Treat non-finite and zero-magnitude alike.
        sum_sq = math.fsum(float(v) * float(v) for v in query_embedding)
        query_magnitude = math.sqrt(sum_sq) if math.isfinite(sum_sq) else float("nan")
        if not math.isfinite(query_magnitude) or query_magnitude == 0.0:
            span.set_attribute("lore.outcome", "degenerate_embedding")
            logger.warning(
                "lore_embedding.retrieve degenerate_embedding model=%s query_len=%d",
                embedding_model,
                len(query_text),
            )
            from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

            _watcher_publish(
                "lore_retrieval",
                {
                    "selected": [],
                    "rejected": [],
                    "selected_count": 0,
                    "total_fragments": len(lore_store),
                    "budget": int(top_k),
                    "tokens_used": 0,
                    "min_similarity": float(min_similarity),
                    "outcome": "degenerate_embedding",
                    "peak_similarity": 0.0,
                    "embedding_model": embedding_model,
                    "context_hint": (
                        query_text[:80] + "…" if len(query_text) > 80 else query_text
                    ),
                },
                component="lore",
            )
            return None

        # Re-queue any fragments whose stored embedding dimension differs
        # from the current model's. Without this, cosine_similarity would
        # silently return 0.0 for every mismatched fragment forever —
        # see :meth:`LoreStore.requeue_dimension_mismatched` for the
        # anti-silent-orphan contract. The method's own current_dim<=0
        # guard is belt-and-braces; DaemonClient.embed already refuses
        # zero-length embeddings at the boundary.
        requeued_count = lore_store.requeue_dimension_mismatched(len(query_embedding))
        span.set_attribute("lore.dimension_mismatch_count", requeued_count)
        if requeued_count:
            logger.warning(
                "lore_embedding.retrieve dimension_mismatch requeued=%d current_dim=%d",
                requeued_count,
                len(query_embedding),
            )

        all_hits = lore_store.query_by_similarity(query_embedding, top_k=top_k)
        hits = [(sim, frag) for sim, frag in all_hits if sim >= min_similarity]
        rejected = [(sim, frag) for sim, frag in all_hits if sim < min_similarity]
        span.set_attribute("lore.hit_count", len(hits))

        # Dashboard Lore tab consumer (playtest 2026-04-30 #1B). Pre-fix
        # the panel listened for a `lore_retrieval` watcher event that
        # was never published — only the OTEL `lore_embedding.retrieve`
        # SPAN was emitted, which the dashboard cannot consume. Publish
        # a structured event with selected/rejected fragments + budget
        # so the panel can render the per-turn budget bar and fragment
        # list. Counted token estimates use the prompt-zone heuristic
        # (1 token ≈ 4 chars) — exact counts aren't available without
        # a tokenizer round-trip.
        from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

        def _frag_payload(sim: float, frag: LoreFragment) -> dict:
            content = frag.content or ""
            return {
                "id": frag.id,
                "category": frag.category,
                "similarity": float(sim),
                "tokens": max(1, len(content) // 4),
                "preview": (content[:120] + "…" if len(content) > 120 else content),
            }

        selected_payload = [_frag_payload(s, f) for s, f in hits]
        rejected_payload = [_frag_payload(s, f) for s, f in rejected]
        tokens_used = sum(f["tokens"] for f in selected_payload)
        # Story 75-15 (AC5): the panel needs the peak similarity vs the floor to
        # diagnose a sub-floor starve (gulliver peak 0.1499 vs floor 0.15), and
        # an explicit outcome to tell "nothing cleared the floor" from "ok".
        # `all_hits` is sorted descending, so [0] is the best candidate the
        # store could offer — selected or not.
        peak_similarity = float(all_hits[0][0]) if all_hits else None
        outcome = "ok" if hits else "no_hits_above_threshold"
        _watcher_publish(
            "lore_retrieval",
            {
                "selected": selected_payload,
                "rejected": rejected_payload,
                "selected_count": len(selected_payload),
                "total_fragments": len(lore_store),
                # `top_k` is the retrieval budget — number of fragments
                # the store is allowed to return before similarity
                # thresholding. Surfaced as `budget` so the dashboard
                # can render a percent-used bar.
                "budget": int(top_k),
                "tokens_used": int(tokens_used),
                "min_similarity": float(min_similarity),
                "outcome": outcome,
                "peak_similarity": peak_similarity,
                "embedding_model": embedding_model,
                "context_hint": (query_text[:80] + "…" if len(query_text) > 80 else query_text),
            },
            component="lore",
        )

        if peak_similarity is not None:
            span.set_attribute("lore.peak_similarity", peak_similarity)
        if not hits:
            span.set_attribute("lore.outcome", "no_hits_above_threshold")
            return None

        span.set_attribute("lore.top_similarity", hits[0][0])
        span.set_attribute("lore.outcome", "ok")
        return _format_lore_section(hits, preview_chars=preview_chars)


def _format_lore_section(
    hits: list[tuple[float, LoreFragment]],
    *,
    preview_chars: int,
) -> str:
    """Render the top-k hits as a ``<lore>`` prompt block.

    Format is intentionally plain markdown-ish — the narrator sees
    category, similarity score, and a content preview. The score is
    included so the narrator can weight confidence when weaving the
    fragment into the prose (high-score fragment = canon reference,
    low-score = tangential nudge).
    """
    lines = ["<lore>", "# Relevant lore retrieved for this turn"]
    for sim, frag in hits:
        preview = frag.content.strip().replace("\n", " ")
        if len(preview) > preview_chars:
            preview = preview[: preview_chars - 1].rstrip() + "…"
        lines.append(f"- [{frag.category} · id={frag.id} · similarity={sim:.2f}] {preview}")
    lines.append("</lore>")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_FRAGMENT_PREVIEW_CHARS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRIEVAL_MIN_SIMILARITY",
    "DEFAULT_RETRIEVAL_TOP_K",
    "EmbedWorkerResult",
    "embed_pending_fragments",
    "retrieve_lore_context",
]
