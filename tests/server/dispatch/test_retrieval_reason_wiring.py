"""Story 84-4 (WI-6) — production WIRING of the score decomposition (RED phase).

server CLAUDE.md "Every Test Suite Needs a Wiring Test" + "No Source-Text Wiring
Tests": drive the LIVE per-turn delegate ``handler._retrieve_entities_for_turn``
(the real seam in ``websocket_session_handler.py``) and assert on BOTH observable
surfaces WI-6 must light up:

    handler._retrieve_entities_for_turn                       (live delegate)
      → server/dispatch/universal_retrieval.retrieve_for_turn  (watcher emit ← add fields)
        → game/retrieval_orchestration.retrieve_turn_context   (span emit ← add attr)

  (1) the ``retrieval.universal`` span carries ``retrieval.card.reason`` populated
      for the selected fill card — proves the §A5 span decomposition is on the
      live path (not just a unit helper);
  (2) a REAL ``watcher_hub`` subscriber receives the ``retrieval`` event with
      ``embed_skipped`` and ``card_reasons`` in its fields — proves the GM-panel
      reader path (WatcherHub, not Jaeger) actually carries the decomposition.

Drives a deterministic offline fill (fake daemon returns a fixed vector against a
pre-seeded card) so no socket/MiniLM is touched. The #1 failure mode this avoids
(project memory): a test that calls ``retrieve_turn_context`` directly proves
nothing about production reachability — every assertion here enters through the
live ``_retrieve_entities_for_turn`` delegate.

OTEL hazard: span-attr assertion — run this file's span path with ``-n0`` if the
full server suite deadlocks (project memory note).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.entity_card import EntityCard, EntityType
from tests._helpers.doubles import FakeSocket

_RETRIEVAL_SPAN_NAME = "retrieval.universal"
_VEC: list[float] = [0.11, 0.23, 0.37, 0.41, 0.53, 0.67, 0.71, 0.83]


class _FakeDaemon:
    def __init__(self, vector: list[float]) -> None:
        self._vector = list(vector)

    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict[str, Any]:
        return {"embedding": list(self._vector)}


def _install_span_exporter(monkeypatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.tracer",
        provider.get_tracer("test-84-4-wiring"),
    )
    return exporter


def _seed_npc_fill_card(sd, entity_id: str, content: str) -> EntityCard:
    card = EntityCard.new(EntityType.NPC, entity_id, content=content)
    card.embedding = list(_VEC)
    card.embedding_pending = False
    sd.entity_store.add(card)
    return card


@pytest.mark.asyncio
async def test_live_turn_emits_card_reason_span_and_watcher_fields(
    session_handler_factory, monkeypatch
) -> None:
    """The capstone WI-6 wiring assertion: a real fill through the live delegate
    populates ``retrieval.card.reason`` on the span AND delivers ``embed_skipped``
    + ``card_reasons`` to a real watcher-hub subscriber."""
    from sidequest.telemetry import watcher_hub as wh_module

    # Offline successful fill: a fake daemon + a pre-seeded card scoring cosine 1.0.
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _FakeDaemon(_VEC),
    )
    exporter = _install_span_exporter(monkeypatch)

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 4
    _seed_npc_fill_card(
        sd, "wandering_minstrel", "A wandering minstrel who trades rumors for coin."
    )

    hub = wh_module.watcher_hub
    hub.bind_loop(asyncio.get_running_loop())
    fake = FakeSocket()
    await hub.subscribe(fake)
    try:
        # Thin action (names nobody, no present NPC) → the cosine fill runs and a
        # card is selected, so there IS a per-card reason to emit.
        result = await handler._retrieve_entities_for_turn(sd, "ask around the tavern for rumors")
        assert result.outcome == "success"
        assert result.retrieved_npcs is not None
        assert any(c.id == "npc:wandering_minstrel" for c in result.retrieved_npcs)

        # (1) The span carries the per-card decomposition for the selected card.
        spans = [s for s in exporter.get_finished_spans() if s.name == _RETRIEVAL_SPAN_NAME]
        assert len(spans) == 1, f"exactly one {_RETRIEVAL_SPAN_NAME} span; got {len(spans)}"
        attrs = spans[0].attributes or {}
        raw = attrs.get("retrieval.card.reason")
        assert raw is not None, "the live span must carry retrieval.card.reason (§A5) — WI-6 wiring"
        decoded = json.loads(raw)
        assert any(e["card_id"] == "npc:wandering_minstrel" for e in decoded), (
            "the selected fill card's decomposition must be on the live span"
        )
        # embed_skipped is False here (the thin action embedded).
        assert attrs.get("retrieval.embed_skipped") is False

        # (2) A real hub subscriber receives the decomposition fields.
        retrieval_events: list[dict[str, Any]] = []
        for _ in range(50):
            await asyncio.sleep(0.01)
            retrieval_events = [
                e for e in fake.events if e.get("fields", {}).get("field") == "universal_retrieval"
            ]
            if retrieval_events:
                break

        assert retrieval_events, (
            "the retrieval watcher event must reach a hub subscriber via the live delegate"
        )
        fields = retrieval_events[0]["fields"]
        assert "embed_skipped" in fields and fields["embed_skipped"] is False, (
            "the GM-panel watcher event must carry embed_skipped (AC-3)"
        )
        assert "card_reasons" in fields, "the watcher event must carry card_reasons (AC-4)"
        ids = {r["card_id"] for r in fields["card_reasons"]}
        assert "npc:wandering_minstrel" in ids, (
            "the selected card's reason must reach the GM-panel reader path"
        )
    finally:
        await hub.unsubscribe(fake)
