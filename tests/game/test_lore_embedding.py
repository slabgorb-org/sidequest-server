"""Tests for ``sidequest.game.lore_embedding`` — Story 37-33.

Exercises the embedding worker (``embed_pending_fragments``) and the
RAG retrieval helper (``retrieve_lore_context``) against a fake
``DaemonClient`` that records calls and returns programmable
responses.

The fake replaces the real Unix-socket client; the tests never hit
a daemon. Happy-path, daemon-unavailable, and structured-error paths
are covered so graceful degradation is wired correctly.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.daemon_client import DaemonRequestError, DaemonUnavailableError
from sidequest.game.lore_embedding import (
    DEFAULT_FRAGMENT_PREVIEW_CHARS,
    EmbedWorkerResult,
    embed_pending_fragments,
    retrieve_lore_context,
)
from sidequest.game.lore_store import (
    LoreCategory,
    LoreFragment,
    LoreSource,
    LoreStore,
)

# ---------------------------------------------------------------------------
# Fake DaemonClient — duck-typed; only embed() + is_available() are used
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for :class:`DaemonClient`.

    - ``available`` gates :meth:`is_available`.
    - ``responses`` maps query text → either an EmbedResponse dict or an
      Exception instance (raised on call).
    - ``default_response`` is used when the text is not in ``responses``.
    - ``calls`` records the ``text`` argument of every call for assertions.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        responses: dict[str, Any] | None = None,
        default_response: dict[str, Any] | None = None,
    ) -> None:
        self._available = available
        self.responses = responses or {}
        self.default_response = default_response or {
            "embedding": [1.0, 0.0],
            "model": "fake-model",
            "latency_ms": 5,
        }
        self.calls: list[str] = []
        self.socket_path = "/tmp/fake-sock"

    def is_available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        if text in self.responses:
            payload = self.responses[text]
            if isinstance(payload, Exception):
                raise payload
            return payload
        return self.default_response


def _frag(id_: str, content: str = "content") -> LoreFragment:
    return LoreFragment.new(
        id=id_,
        category=LoreCategory.History,
        content=content,
        source=LoreSource.GenrePack,
    )


# ---------------------------------------------------------------------------
# Worker happy path + skip paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_embeds_all_pending_fragments() -> None:
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    store.add(_frag("b", "beta"))
    client = _FakeClient(
        responses={
            "alpha": {"embedding": [0.1], "model": "m", "latency_ms": 1},
            "beta": {"embedding": [0.2], "model": "m", "latency_ms": 2},
        }
    )

    result = await embed_pending_fragments(store, client=client)

    assert result.embedded == 2
    assert result.failed == 0
    assert store.fragments["a"].embedding == [0.1]
    assert store.fragments["a"].embedding_pending is False
    assert store.fragments["b"].embedding == [0.2]
    assert sorted(client.calls) == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_worker_skips_when_daemon_unavailable() -> None:
    store = LoreStore()
    store.add(_frag("a"))
    client = _FakeClient(available=False)

    result = await embed_pending_fragments(store, client=client)

    assert result.skipped_daemon_unavailable is True
    assert result.embedded == 0
    assert client.calls == []
    assert store.fragments["a"].embedding_pending is True


@pytest.mark.asyncio
async def test_worker_returns_early_on_empty_queue() -> None:
    store = LoreStore()
    frag = _frag("a")
    store.add(frag)
    store.update_embedding("a", [1.0])
    client = _FakeClient()

    result = await embed_pending_fragments(store, client=client)

    assert result.skipped_empty_queue is True
    assert result.embedded == 0
    assert client.calls == []


# ---------------------------------------------------------------------------
# Worker error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_increments_retry_on_structured_error() -> None:
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    client = _FakeClient(responses={"alpha": DaemonRequestError("EMBED_FAILED", "model offline")})

    result = await embed_pending_fragments(store, client=client)

    assert result.failed == 1
    assert result.embedded == 0
    assert store.fragments["a"].embedding_retry_count == 1
    assert store.fragments["a"].embedding_pending is True


@pytest.mark.asyncio
async def test_worker_increments_retry_on_value_error() -> None:
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    client = _FakeClient(
        responses={"alpha": ValueError("embed() text exceeds 32768-byte UTF-8 limit")}
    )

    result = await embed_pending_fragments(store, client=client)

    assert result.failed == 1
    assert store.fragments["a"].embedding_retry_count == 1


@pytest.mark.asyncio
async def test_worker_stops_on_daemon_unavailable_mid_run() -> None:
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    store.add(_frag("b", "beta"))
    store.add(_frag("c", "gamma"))
    client = _FakeClient(
        responses={
            "alpha": {"embedding": [0.1], "model": "m", "latency_ms": 1},
            "beta": DaemonUnavailableError("socket vanished"),
            # "gamma" never reached
        }
    )

    result = await embed_pending_fragments(store, client=client)

    assert result.embedded == 1
    assert result.skipped_daemon_unavailable is True
    assert store.fragments["a"].embedding == [0.1]
    assert store.fragments["b"].embedding_pending is True
    assert store.fragments["b"].embedding_retry_count == 0
    assert store.fragments["c"].embedding_pending is True
    assert "gamma" not in client.calls


@pytest.mark.asyncio
async def test_worker_respects_max_per_run() -> None:
    store = LoreStore()
    for i in range(5):
        store.add(_frag(f"frag{i}", f"content{i}"))
    client = _FakeClient(default_response={"embedding": [1.0], "model": "m", "latency_ms": 1})

    result = await embed_pending_fragments(store, client=client, max_per_run=2)

    assert result.embedded == 2
    assert len(client.calls) == 2
    # Remaining fragments still pending.
    remaining = [fid for fid, frag in store.fragments.items() if frag.embedding_pending]
    assert len(remaining) == 3


@pytest.mark.asyncio
async def test_worker_respects_max_retries_and_skips_poisoned_fragments() -> None:
    store = LoreStore()
    store.add(_frag("poisoned"))
    store.add(_frag("fresh"))
    # Blow poisoned past the retry ceiling.
    for _ in range(5):
        store.mark_embedding_failed("poisoned")
    client = _FakeClient()

    result = await embed_pending_fragments(store, client=client, max_retries=3)

    assert result.embedded == 1
    assert client.calls == ["content"]  # fresh only
    assert store.fragments["poisoned"].embedding_pending is True


# ---------------------------------------------------------------------------
# EmbedWorkerResult telemetry shape
# ---------------------------------------------------------------------------


def test_embed_worker_result_as_dict_shape() -> None:
    # ``failed`` is a derived @property — callers construct with the
    # sub-counters and the total must equal their sum.
    r = EmbedWorkerResult(
        embedded=3,
        failed_embed_error=1,
        failed_text_too_large=1,
    )
    assert r.failed == 2
    assert r.as_dict() == {
        "embedded": 3,
        "failed": 2,
        "failed_embed_error": 1,
        "failed_text_too_large": 1,
        "skipped_daemon_unavailable": False,
        "skipped_empty_queue": False,
    }


def test_embed_worker_result_failed_property_invariant() -> None:
    """``failed`` is always the sum of the two sub-counters, never drifts."""
    r = EmbedWorkerResult()
    assert r.failed == 0
    r.failed_embed_error = 3
    assert r.failed == 3
    r.failed_text_too_large = 2
    assert r.failed == 5
    # No independent setter — the sum-of-parts is the single source of truth.
    assert r.as_dict()["failed"] == 5


# ---------------------------------------------------------------------------
# Retrieval happy path + skip paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_returns_formatted_lore_block() -> None:
    store = LoreStore()
    store.add(_frag("castle", "The ancient castle on the hill."))
    store.add(_frag("river", "A winding river through the valley."))
    store.update_embedding("castle", [1.0, 0.0])
    store.update_embedding("river", [0.0, 1.0])

    client = _FakeClient(
        responses={
            "player approaches the castle": {
                "embedding": [0.95, 0.05],
                "model": "m",
                "latency_ms": 4,
            }
        }
    )

    section = await retrieve_lore_context(
        store, "player approaches the castle", client=client, top_k=1
    )
    assert section is not None
    assert "<lore>" in section
    assert "</lore>" in section
    assert "castle" in section
    assert "ancient castle" in section
    # river was orthogonal — should not appear in top-1
    assert "river" not in section


@pytest.mark.asyncio
async def test_retrieve_publishes_lore_retrieval_event_for_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playtest 2026-04-30 #1B — the dashboard's Lore tab listens for a
    `lore_retrieval` watcher event with selected/rejected fragments and
    a budget bar. Pre-fix only the ``lore_embedding.retrieve`` OTEL
    SPAN was emitted (which the dashboard cannot consume), so the panel
    badge stuck at 0 and the dropdown was empty even though retrieval
    fired every turn.
    """
    captured: list[tuple[str, dict, str]] = []

    def fake_publish(
        event_type: str,
        payload: dict,
        *,
        component: str = "sidequest-server",
        severity: str = "info",  # noqa: ARG001
    ) -> None:
        captured.append((event_type, payload, component))

    monkeypatch.setattr(
        "sidequest.telemetry.watcher_hub.publish_event",
        fake_publish,
    )

    store = LoreStore()
    store.add(_frag("castle", "The ancient castle on the hill."))
    store.add(_frag("river", "A winding river through the valley."))
    store.update_embedding("castle", [1.0, 0.0])
    store.update_embedding("river", [0.0, 1.0])

    client = _FakeClient(
        responses={
            "player approaches the castle": {
                "embedding": [0.95, 0.05],
                "model": "m",
                "latency_ms": 4,
            }
        }
    )

    section = await retrieve_lore_context(
        store,
        "player approaches the castle",
        client=client,
        top_k=2,
        min_similarity=0.5,
    )
    assert section is not None  # narrator gets the lore block

    # And the dashboard gets the rich event.
    lore_events = [c for c in captured if c[0] == "lore_retrieval"]
    assert len(lore_events) == 1
    _, payload, component = lore_events[0]
    assert component == "lore"
    # `castle` cleared 0.5 (≈0.95), `river` did not (≈0.05) → one selected,
    # one rejected. That's the per-turn detail the dashboard renders.
    selected_ids = {f["id"] for f in payload["selected"]}
    rejected_ids = {f["id"] for f in payload["rejected"]}
    assert selected_ids == {"castle"}
    assert rejected_ids == {"river"}
    assert payload["selected_count"] == 1
    assert payload["total_fragments"] == 2
    assert payload["budget"] == 2  # top_k
    assert payload["tokens_used"] >= 1  # estimate from castle's content
    assert payload["min_similarity"] == 0.5
    assert payload["context_hint"].startswith("player approaches")
    # Each fragment payload carries the dashboard-facing fields.
    castle = next(f for f in payload["selected"] if f["id"] == "castle")
    assert castle["category"] == "history"
    assert 0.0 <= castle["similarity"] <= 1.0
    assert castle["tokens"] >= 1
    assert "preview" in castle


@pytest.mark.asyncio
async def test_retrieve_returns_none_on_empty_store() -> None:
    store = LoreStore()
    client = _FakeClient()

    section = await retrieve_lore_context(store, "query", client=client)
    assert section is None
    # No embed call was wasted on an empty store.
    assert client.calls == []


@pytest.mark.asyncio
async def test_retrieve_returns_none_when_daemon_unavailable() -> None:
    store = LoreStore()
    store.add(_frag("a"))
    store.update_embedding("a", [1.0])
    client = _FakeClient(available=False)

    section = await retrieve_lore_context(store, "query", client=client)
    assert section is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_retrieve_returns_none_on_embed_request_error() -> None:
    store = LoreStore()
    store.add(_frag("a"))
    store.update_embedding("a", [1.0])
    client = _FakeClient(responses={"query": DaemonRequestError("EMBED_FAILED", "oops")})

    section = await retrieve_lore_context(store, "query", client=client)
    assert section is None


@pytest.mark.asyncio
async def test_retrieve_returns_none_on_embed_unavailable_error() -> None:
    store = LoreStore()
    store.add(_frag("a"))
    store.update_embedding("a", [1.0])
    client = _FakeClient(responses={"query": DaemonUnavailableError("socket vanished")})

    section = await retrieve_lore_context(store, "query", client=client)
    assert section is None


@pytest.mark.asyncio
async def test_retrieve_returns_none_on_query_too_large() -> None:
    store = LoreStore()
    store.add(_frag("a"))
    store.update_embedding("a", [1.0])
    client = _FakeClient(
        responses={"query": ValueError("embed() text exceeds 32768-byte UTF-8 limit")}
    )

    section = await retrieve_lore_context(store, "query", client=client)
    assert section is None


@pytest.mark.asyncio
async def test_retrieve_filters_below_min_similarity() -> None:
    store = LoreStore()
    store.add(_frag("noise", "unrelated"))
    store.update_embedding("noise", [0.0, 1.0])
    client = _FakeClient(
        responses={"query": {"embedding": [1.0, 0.0], "model": "m", "latency_ms": 1}}
    )

    # Orthogonal → similarity 0 < floor 0.5 → no hits → None.
    section = await retrieve_lore_context(store, "query", client=client, min_similarity=0.5)
    assert section is None


@pytest.mark.asyncio
async def test_retrieve_truncates_long_fragments_to_preview_chars() -> None:
    long_content = "x" * (DEFAULT_FRAGMENT_PREVIEW_CHARS + 100)
    store = LoreStore()
    store.add(_frag("big", long_content))
    store.update_embedding("big", [1.0, 0.0])
    client = _FakeClient(
        responses={"query": {"embedding": [1.0, 0.0], "model": "m", "latency_ms": 1}}
    )

    section = await retrieve_lore_context(store, "query", client=client, top_k=1)
    assert section is not None
    # The rendered preview should be at most preview_chars + the truncation
    # marker + the surrounding template. Just check the original long
    # content was not written out verbatim.
    assert "x" * (DEFAULT_FRAGMENT_PREVIEW_CHARS + 100) not in section
    assert "…" in section


@pytest.mark.asyncio
async def test_retrieve_returns_none_on_blank_query() -> None:
    store = LoreStore()
    store.add(_frag("a"))
    store.update_embedding("a", [1.0])
    client = _FakeClient()

    section = await retrieve_lore_context(store, "   ", client=client)
    assert section is None
    assert client.calls == []


# ---------------------------------------------------------------------------
# Round-5: sub-counter accounting, retrieve INVALID_RESPONSE, dim-race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_failed_embed_error_counter_matches_daemon_error_path() -> None:
    """Sub-counter accounting: DaemonRequestError increments failed_embed_error only."""
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    client = _FakeClient(responses={"alpha": DaemonRequestError("EMBED_FAILED", "model offline")})
    result = await embed_pending_fragments(store, client=client)
    assert result.failed == 1
    assert result.failed_embed_error == 1
    assert result.failed_text_too_large == 0


@pytest.mark.asyncio
async def test_worker_failed_text_too_large_counter_matches_value_error_path() -> None:
    """Sub-counter accounting: ValueError increments failed_text_too_large only."""
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    client = _FakeClient(
        responses={"alpha": ValueError("embed() text exceeds 32768-byte UTF-8 limit")}
    )
    result = await embed_pending_fragments(store, client=client)
    assert result.failed == 1
    assert result.failed_embed_error == 0
    assert result.failed_text_too_large == 1


@pytest.mark.asyncio
async def test_worker_mixed_error_types_split_counters_correctly() -> None:
    """Two distinct error types across the same run produce a consistent total."""
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    store.add(_frag("b", "beta"))
    client = _FakeClient(
        responses={
            "alpha": DaemonRequestError("EMBED_FAILED", "model offline"),
            "beta": ValueError("too large"),
        }
    )
    result = await embed_pending_fragments(store, client=client)
    assert result.failed == 2
    assert result.failed_embed_error == 1
    assert result.failed_text_too_large == 1
    # Watcher payload invariant: total always equals sum of parts.
    payload = result.as_dict()
    assert payload["failed"] == (payload["failed_embed_error"] + payload["failed_text_too_large"])


@pytest.mark.asyncio
async def test_retrieve_returns_none_on_daemon_invalid_response() -> None:
    """Round-5: INVALID_RESPONSE from client-side validation surfaces as
    DaemonRequestError and is caught by the (DaemonUnavailableError, DaemonRequestError)
    handler — no need for a separate (KeyError, TypeError) catch."""
    store = LoreStore()
    store.add(_frag("x", "content"))
    store.update_embedding("x", [1.0, 0.0])
    client = _FakeClient(
        responses={
            "query": DaemonRequestError(
                "INVALID_RESPONSE", "embed reply 'embedding' is zero-length"
            )
        }
    )
    section = await retrieve_lore_context(store, "query", client=client)
    assert section is None


@pytest.mark.asyncio
async def test_worker_refuses_dim_mismatched_writeback() -> None:
    """Round-5 retrieve/worker race guard: first successful embed pins
    ``expected_dim``; a subsequent embed returning a different-dim vector
    is refused (write_back=False) and counted as a daemon error."""
    store = LoreStore()
    store.add(_frag("a", "alpha"))
    store.add(_frag("b", "beta"))
    client = _FakeClient(
        responses={
            # First iteration: 2-d vector — pins expected_dim=2.
            "alpha": {"embedding": [1.0, 0.0], "model": "m", "latency_ms": 1},
            # Second iteration: daemon changed to a 3-d model.
            "beta": {"embedding": [0.1, 0.2, 0.3], "model": "m2", "latency_ms": 2},
        }
    )
    result = await embed_pending_fragments(store, client=client)
    assert result.embedded == 1
    assert result.failed_embed_error == 1  # the 3-d write-back was refused
    assert store.fragments["a"].embedding == [1.0, 0.0]
    # The mismatched fragment stays pending — the next worker pass will
    # pick it up with the new expected_dim=3.
    assert store.fragments["b"].embedding is None
    assert store.fragments["b"].embedding_pending is True


@pytest.mark.asyncio
async def test_retrieve_emits_dimension_mismatch_count_span_attr() -> None:
    """Regression: the round-4 fix emits lore.dimension_mismatch_count on retrieve."""
    store = LoreStore()
    store.add(_frag("stale", "content"))
    # Stale 3-d embedding; current model is 2-d.
    store.update_embedding("stale", [1.0, 0.0, 0.5])
    client = _FakeClient()  # default returns 2-d [1.0, 0.0]
    section = await retrieve_lore_context(store, "query", client=client)
    # The stale fragment got re-queued, so query_by_similarity finds no hits.
    assert section is None
    assert store.fragments["stale"].embedding is None
    assert store.fragments["stale"].embedding_pending is True
