"""RED tests for Story 76-3 — ``expected_dim`` parity for the entity embed worker.

The lore embedding worker already guards against the retrieve/worker race where a
mid-session daemon model-dim change (MiniLM-384 → -768) writes a stale-dimension
vector back and silently clears the pending flag, orphaning the fragment forever
(``test_lore_embedding.py::test_worker_refuses_dim_mismatched_writeback``). The
entity worker is its sibling but lacks the guard:
:meth:`EntityStore.update_embedding` takes no ``expected_dim`` and returns ``None``,
and :func:`embed_pending_entity_cards` calls it ungated. Today the corrupt write
self-heals one turn later via :meth:`EntityStore.requeue_dimension_mismatched`;
this story prevents the corrupt write in the first place.

These tests mirror the lore guard tests over ``sidequest.game.entity_store`` and
``sidequest.game.entity_embedding``. They assert behavior (store state, worker
counters, the OTEL span event) — never source text (server CLAUDE.md: *No
Source-Text Wiring Tests*).
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.entity_embedding import embed_pending_entity_cards
from sidequest.game.entity_store import EntityStore

# ---------------------------------------------------------------------------
# Fake DaemonClient — duck-typed; only embed() + is_available() are used.
# Mirrors tests/game/test_lore_embedding.py::_FakeClient.
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for :class:`DaemonClient`.

    - ``available`` gates :meth:`is_available`.
    - ``responses`` maps embed text → an EmbedResponse dict (or an Exception to raise).
    - ``default_response`` is used when the text is not in ``responses``.
    - ``calls`` records every ``text`` argument for assertions.
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


def _dim_mismatch_events(exporter: Any) -> list[Any]:
    """Collect ``embed_failed`` span events whose reason is the dim-mismatch
    write-back refusal, across every ``entity_embedding.worker`` span."""
    events = []
    for span in exporter.get_finished_spans():
        if span.name != "entity_embedding.worker":
            continue
        for ev in span.events:
            if (
                ev.name == "embed_failed"
                and ev.attributes.get("reason") == "dim_mismatch_writeback_refused"
            ):
                events.append(ev)
    return events


# ---------------------------------------------------------------------------
# AC-1 — EntityStore.update_embedding gains the expected_dim guard + bool return
# ---------------------------------------------------------------------------


class TestUpdateEmbeddingDimGuard:
    def test_matching_dim_writes_and_returns_true(self) -> None:
        """AC-1: when the vector matches ``expected_dim`` the write succeeds,
        clears the pending flag, and returns ``True`` (mirrors
        ``LoreStore.update_embedding``, which returns ``bool``)."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))

        written = store.update_embedding("npc:borin", [0.1, 0.2], expected_dim=2)

        assert written is True
        assert store.cards["npc:borin"].embedding == [0.1, 0.2]
        assert store.cards["npc:borin"].embedding_pending is False
        assert store.pending_embedding_ids() == []

    def test_mismatched_dim_refuses_write_and_returns_false(self) -> None:
        """AC-1 / AC-5: when ``expected_dim`` is set and the vector length differs,
        the write is refused — the card keeps its prior state (no embedding) and
        stays pending so the next worker pass re-embeds it on the current model.
        Returns ``False``. This is the corrupt-write *prevention* the story adds;
        without it the stale vector lands and the pending flag is cleared."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))

        written = store.update_embedding("npc:borin", [0.1, 0.2, 0.3], expected_dim=2)

        assert written is False
        # No partial write: the card was never embedded, so it stays None + pending.
        assert store.cards["npc:borin"].embedding is None
        assert store.cards["npc:borin"].embedding_pending is True
        assert store.pending_embedding_ids() == ["npc:borin"]

    def test_mismatch_refusal_does_not_orphan_an_already_embedded_card(self) -> None:
        """AC-5: a refused write must not clobber a card that was correctly
        embedded earlier — the existing good vector survives the refusal."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        assert store.update_embedding("npc:borin", [0.4, 0.5], expected_dim=2) is True

        # A later mismatched write-back (e.g. a raced requeue) is refused...
        written = store.update_embedding("npc:borin", [0.4, 0.5, 0.6], expected_dim=2)

        assert written is False
        # ...and the good 2-d vector is untouched.
        assert store.cards["npc:borin"].embedding == [0.4, 0.5]
        assert store.cards["npc:borin"].embedding_pending is False

    def test_no_expected_dim_preserves_legacy_unconditional_write(self) -> None:
        """Back-compat: callers that do not thread ``expected_dim`` keep the
        pre-76-3 behavior — the vector is written unconditionally and the call
        still returns truthy. (The existing 75-4 callers in
        ``test_entity_store.py`` rely on this.)"""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))

        written = store.update_embedding("npc:borin", [0.1, 0.2, 0.3])

        assert written is True
        assert store.cards["npc:borin"].embedding == [0.1, 0.2, 0.3]
        assert store.cards["npc:borin"].embedding_pending is False

    def test_empty_vector_still_rejected_loudly(self) -> None:
        """Regression: the No-Silent-Fallbacks empty-vector guard (75-4) must
        survive the signature change — an empty embedding still raises."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        with pytest.raises(ValueError, match="embedding must not be empty"):
            store.update_embedding("npc:borin", [], expected_dim=2)


# ---------------------------------------------------------------------------
# AC-2 — embed_pending_entity_cards captures expected_dim and refuses mismatches
# ---------------------------------------------------------------------------


class TestWorkerExpectedDim:
    @pytest.mark.asyncio
    async def test_worker_refuses_dim_mismatched_writeback(self) -> None:
        """AC-2: the first successful embed pins ``expected_dim``; a later embed
        returning a different-dim vector (daemon switched models mid-run) is
        refused. The good card is embedded; the mismatched card stays pending
        with no vector, so the next pass re-embeds it on the new dim. Counted as
        a daemon error, exactly like the lore worker."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "a", content="alpha"))
        store.add(EntityCard.new(EntityType.NPC, "b", content="beta"))
        client = _FakeClient(
            responses={
                # First iteration pins expected_dim=2.
                "alpha": {"embedding": [1.0, 0.0], "model": "m", "latency_ms": 1},
                # Second iteration: daemon changed to a 3-d model.
                "beta": {"embedding": [0.1, 0.2, 0.3], "model": "m2", "latency_ms": 2},
            }
        )

        result = await embed_pending_entity_cards(store, client=client)

        assert result.embedded == 1
        assert result.failed_embed_error == 1  # the 3-d write-back was refused
        assert store.cards["npc:a"].embedding == [1.0, 0.0]
        assert store.cards["npc:a"].embedding_pending is False
        # The mismatched card stays pending — no stale vector written.
        assert store.cards["npc:b"].embedding is None
        assert store.cards["npc:b"].embedding_pending is True
        assert store.pending_embedding_ids() == ["npc:b"]

    @pytest.mark.asyncio
    async def test_worker_embeds_all_when_dims_consistent(self) -> None:
        """AC-2 (happy path): when every embed returns the pinned dimension, all
        pending cards embed and nothing is refused — the guard is inert when the
        model is stable."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "a", content="alpha"))
        store.add(EntityCard.new(EntityType.NPC, "b", content="beta"))
        client = _FakeClient(
            responses={
                "alpha": {"embedding": [0.1, 0.9], "model": "m", "latency_ms": 1},
                "beta": {"embedding": [0.2, 0.8], "model": "m", "latency_ms": 2},
            }
        )

        result = await embed_pending_entity_cards(store, client=client)

        assert result.embedded == 2
        assert result.failed_embed_error == 0
        assert store.pending_embedding_ids() == []


# ---------------------------------------------------------------------------
# AC-3 — dimension-mismatch OTEL span event (the GM-panel lie detector)
# ---------------------------------------------------------------------------


class TestWorkerDimMismatchOtel:
    @pytest.mark.asyncio
    async def test_worker_emits_dim_mismatch_span_event(self, otel_capture: Any) -> None:
        """AC-3: a refused write emits an ``embed_failed`` event on the
        ``entity_embedding.worker`` span with
        ``reason="dim_mismatch_writeback_refused"`` and the ``written_dim`` /
        ``expected_dim`` attributes — matching the lore telemetry so the GM panel
        reads one shape. Per the OTEL Observability Principle, the span is the
        only proof the guard actually fired rather than the worker improvising."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "a", content="alpha"))
        store.add(EntityCard.new(EntityType.NPC, "b", content="beta"))
        client = _FakeClient(
            responses={
                "alpha": {"embedding": [1.0, 0.0], "model": "m", "latency_ms": 1},
                "beta": {"embedding": [0.1, 0.2, 0.3], "model": "m2", "latency_ms": 2},
            }
        )

        await embed_pending_entity_cards(store, client=client)

        events = _dim_mismatch_events(otel_capture)
        assert len(events) == 1, (
            "expected exactly one dim_mismatch_writeback_refused event; "
            f"worker spans/events seen: "
            f"{[(s.name, [e.name for e in s.events]) for s in otel_capture.get_finished_spans()]}"
        )
        attrs = events[0].attributes
        assert attrs["written_dim"] == 3
        assert attrs["expected_dim"] == 2
        assert attrs["card_id"] == "npc:b"


# ---------------------------------------------------------------------------
# AC-4 — WIRING TEST: the guard fires in the production worker path and the
# stale vector never reaches query_by_similarity (behavior, not source grep).
# ---------------------------------------------------------------------------


class TestDimGuardWiring:
    @pytest.mark.asyncio
    async def test_mid_session_model_change_does_not_orphan_card_via_real_worker(
        self,
    ) -> None:
        """AC-4 / AC-5 (server CLAUDE.md *Every Test Suite Needs a Wiring Test*):
        drive the real :func:`embed_pending_entity_cards` worker — the only
        production caller of ``EntityStore.update_embedding`` — through a
        mid-session daemon model change, and prove the stale-dim vector is refused
        end-to-end. The refused card must remain pending (so the next worker pass
        re-embeds it on the current model) and must NOT surface from
        ``query_by_similarity`` carrying a corrupt vector."""
        store = EntityStore()
        store.add(EntityCard.new(EntityType.NPC, "borin", content="smith"))
        store.add(EntityCard.new(EntityType.NPC, "skarl", content="enforcer"))

        # The daemon serves a 2-d model for the first card, then hot-reloads to a
        # 3-d model for the second — exactly the mid-session race this story fixes.
        client = _FakeClient(
            responses={
                "smith": {"embedding": [1.0, 0.0], "model": "minilm-2d", "latency_ms": 1},
                "enforcer": {"embedding": [0.0, 1.0, 0.0], "model": "minilm-3d", "latency_ms": 2},
            }
        )

        result = await embed_pending_entity_cards(store, client=client)

        # borin embedded on the pinned dim; skarl refused and left pending.
        assert result.embedded == 1
        assert store.cards["npc:skarl"].embedding is None
        assert store.cards["npc:skarl"].embedding_pending is True
        assert "npc:skarl" in store.pending_embedding_ids()

        # A query on the pinned 2-d model returns borin and never the refused,
        # un-embedded skarl — no orphan slipped into retrieval.
        ranked = store.query_by_similarity([1.0, 0.0], top_k=5, entity_type=EntityType.NPC)
        ranked_ids = {card.id for _, card in ranked}
        assert "npc:borin" in ranked_ids
        assert "npc:skarl" not in ranked_ids
