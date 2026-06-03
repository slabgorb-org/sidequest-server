"""Capstone end-to-end wiring test for the Universal Retrieval Layer (Story 75-8, ADR-118).

Stories 75-1..75-7 built and merged the universal-retrieval pipeline. This suite is the
mandated wiring test (server CLAUDE.md "Every Test Suite Needs a Wiring Test"): it proves the
pipeline is reachable AND that its output actually lands in the narrator prompt — driven from
the REAL per-turn production delegate, not a synthetic fixture.

THE NET-NEW COVERAGE OVER 75-7
------------------------------
``tests/server/dispatch/test_universal_retrieval_dispatch.py`` (75-7) already proves the
``retrieval.universal`` span + the GM-panel watcher event. What it does NOT cover — and what
this capstone pins — is the INJECTION half of ADR-118 §D4:

    player action
      → handler._retrieve_entities_for_turn        (the live delegate, websocket_session_handler.py)
        → universal_retrieval.retrieve_for_turn     (75-7 dispatch wrapper)
          → retrieve_turn_context                   (75-5 floor+fill, emits the span)
      → _build_turn_context                         (session_helpers.py, renders typed sections)
        → Orchestrator.build_narrator_prompt        (registers them into AttentionZone.Valley)

THE #1 FAILURE MODE THIS SUITE AVOIDS (project memory / Architect note)
-----------------------------------------------------------------------
A wiring test that calls ``retrieve_turn_context(...)`` directly, or hand-rolls a
``RetrievedEntities`` and asserts on it, proves NOTHING about production reachability. Every
test below enters through ``handler._retrieve_entities_for_turn`` — the live seam.

Test discipline (server CLAUDE.md "No Source-Text Wiring Tests"): assertions are on observed
behaviour — the returned ``RetrievedEntities``, the ``retrieval.universal`` span, the
registered Valley ``PromptSection``, and a real ``watcher_hub`` subscriber — never on source
patterns.

GREEN-ON-ARRIVAL NOTE
---------------------
Because 75-4..75-7 already shipped, these tests pass against the current tree. That is the
point of a capstone verification story: a FAILURE here is a real 75-5/75-6 integration
regression (route back to SM as a blocking Delivery Finding), not an unimplemented feature.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.orchestrator import Orchestrator
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import AttentionZone
from sidequest.game.creature_core import CreatureCore
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.retrieval_orchestration import RetrievedEntities
from sidequest.game.session import Npc
from sidequest.server.session_helpers import _build_turn_context

# A fixed, non-degenerate query/card embedding. A card seeded with this exact
# vector scores cosine 1.0 against a daemon that returns the same vector, so the
# fill is deterministic and offline — no socket, no MiniLM (Architect gotcha:
# query_by_similarity is pure-Python cosine once a card carries an embedding).
_VEC: list[float] = [0.11, 0.23, 0.37, 0.41, 0.53, 0.67, 0.71, 0.83]


# ---------------------------------------------------------------------------
# Offline daemon seams
# ---------------------------------------------------------------------------


class _FakeDaemon:
    """``DaemonClient`` stand-in that is available and returns a fixed embedding
    vector — drives ``retrieve_turn_context`` down a REAL successful fill against
    a pre-seeded ``entity_store`` with no daemon process."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = list(vector)

    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict[str, Any]:
        return {"embedding": list(self._vector)}


class _UnavailableDaemon:
    """``DaemonClient`` stand-in whose ``is_available()`` is False — drives the
    real orchestrator to the deterministic ``query_failed`` outcome (fast, no
    socket) for pure reachability/span proofs."""

    def is_available(self) -> bool:
        return False

    async def embed(self, text: str) -> dict[str, Any]:  # pragma: no cover - never reached
        raise RuntimeError("embed must not be called when is_available() is False")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_span_exporter(monkeypatch) -> InMemorySpanExporter:
    """Redirect the orchestrator's tracer to an in-memory exporter so a test can
    assert the ``retrieval.universal`` span fired with its attributes."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.tracer",
        provider.get_tracer("test-75-8-e2e"),
    )
    return exporter


def _seed_npc_fill_card(sd, entity_id: str, content: str) -> EntityCard:
    """Add a non-scene-present NPC card to the session entity store with a ready
    embedding so the fill can select it deterministically."""
    card = EntityCard.new(EntityType.NPC, entity_id, content=content)
    card.embedding = list(_VEC)
    card.embedding_pending = False
    sd.entity_store.add(card)
    return card


def _scene_present_npc(name: str, *, turn: int) -> Npc:
    """A stateful NPC stamped scene-present at ``turn`` — lands in the 75-2 floor."""
    return Npc(
        core=CreatureCore(name=name, description=f"{name} is here.", personality="stoic"),
        last_seen_turn=turn,
    )


def _make_orchestrator() -> Orchestrator:
    """A real ``Orchestrator`` for the prompt-assembly half of the arc.

    The ``session_handler_factory`` deliberately gives ``sd.orchestrator`` as a
    ``MagicMock(spec=Orchestrator)`` (it isolates the handler from the narrator),
    so the Valley-registration assertion must drive a real Orchestrator —
    ``build_narrator_prompt`` is backend-agnostic for the entity-section
    registrations under test (same pattern as ``tests/agents/test_seed_valley_injection.py``).
    The retrieval half of the chain still runs through the production handler
    delegate; only the final prompt assembly uses this fresh orchestrator.
    """
    return Orchestrator()


def _valley_section_names(registry: PromptRegistry, agent_name: str) -> set[str]:
    return {s.name for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)}


def _retrieval_span_attrs(exporter: InMemorySpanExporter) -> Mapping[str, Any]:
    """Return the single ``retrieval.universal`` span's attributes (asserts exactly
    one span fired, and that it carries attributes — so callers read a narrowed,
    non-Optional mapping)."""
    spans = [s for s in exporter.get_finished_spans() if s.name == "retrieval.universal"]
    assert len(spans) == 1, (
        f"exactly one retrieval.universal span per turn; got {len(spans)} "
        f"(all spans: {[s.name for s in exporter.get_finished_spans()]})"
    )
    attrs = spans[0].attributes
    assert attrs is not None, "retrieval.universal span must carry attributes"
    return attrs


# ===========================================================================
# 1. Production reachability — the delegate reaches the real orchestrator
#    (the no-op-trap guard) and the span fires
# ===========================================================================


@pytest.mark.asyncio
async def test_production_delegate_reaches_orchestrator_and_fires_span(
    session_handler_factory, monkeypatch
) -> None:
    """Driving the live ``_retrieve_entities_for_turn`` delegate runs the real
    75-5 orchestrator and emits the ``retrieval.universal`` span. Daemon forced
    down → deterministic ``query_failed`` (the wiring proven is identical for any
    outcome: handler → dispatch → retrieve_turn_context → span)."""
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _UnavailableDaemon(),
    )
    exporter = _install_span_exporter(monkeypatch)

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 2

    result = await handler._retrieve_entities_for_turn(sd, "shout into the dark")

    assert isinstance(result, RetrievedEntities)
    assert result.outcome == "query_failed"
    attrs = _retrieval_span_attrs(exporter)
    assert attrs.get("retrieval.outcome") == "query_failed"


# ===========================================================================
# 2. THE CAPSTONE — action → floor+fill → typed Valley injection
# ===========================================================================


@pytest.mark.asyncio
async def test_capstone_action_to_fill_to_valley_npc_section(
    session_handler_factory, monkeypatch
) -> None:
    """The headline end-to-end arc (the half 75-7 does not cover): a player action
    drives the real delegate to a successful semantic fill, and that fill is
    INJECTED into the assembled narrator prompt as a typed ``retrieved_npcs``
    section in ``AttentionZone.Valley``."""
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _FakeDaemon(_VEC),
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 4
    _seed_npc_fill_card(
        sd, "wandering_minstrel", "A wandering minstrel who trades rumors for coin."
    )

    action = "ask around the tavern for rumors"
    result = await handler._retrieve_entities_for_turn(sd, action)

    # Fill happened on the real path.
    assert result.outcome == "success"
    assert result.retrieved_npcs is not None
    assert any(c.id == "npc:wandering_minstrel" for c in result.retrieved_npcs)

    # Render seam (session_helpers): the fill becomes a typed TurnContext field.
    context = _build_turn_context(sd, entity_retrieval=result)
    assert context.retrieved_entity_npcs is not None
    assert "wandering minstrel" in context.retrieved_entity_npcs

    # Injection seam (orchestrator): exactly one retrieved_npcs section, in Valley,
    # carrying the card content.
    orch = _make_orchestrator()
    _prompt, registry = await orch.build_narrator_prompt(action, context)
    agent_name = orch._narrator.name()
    npc_sections = [
        s
        for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)
        if s.name == "retrieved_npcs"
    ]
    assert len(npc_sections) == 1, (
        "the fill must register exactly one retrieved_npcs Valley section"
    )
    assert "wandering minstrel" in npc_sections[0].content


@pytest.mark.asyncio
async def test_capstone_span_attributes_reflect_successful_fill(
    session_handler_factory, monkeypatch
) -> None:
    """The same successful-fill arc is observable on the GM panel: the
    ``retrieval.universal`` span carries ``outcome=success`` and a per-type
    ``npc_count`` matching the single card that was filled and injected."""
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _FakeDaemon(_VEC),
    )
    exporter = _install_span_exporter(monkeypatch)

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 4
    _seed_npc_fill_card(sd, "wandering_minstrel", "A wandering minstrel with rumors to sell.")

    result = await handler._retrieve_entities_for_turn(sd, "listen for gossip")

    assert result.outcome == "success"
    attrs = _retrieval_span_attrs(exporter)
    assert attrs.get("retrieval.outcome") == "success"
    assert attrs.get("retrieval.npc_count") == 1
    assert attrs.get("retrieval.fill_selected_count") == 1


# ===========================================================================
# 3. Zero-byte-leak — a type that retrieved nothing registers NO Valley section
# ===========================================================================


@pytest.mark.asyncio
async def test_zero_byte_leak_absent_types_register_no_valley_section(
    session_handler_factory, monkeypatch
) -> None:
    """ADR-118 §D4 None→no-section contract, end to end: with only an NPC fill,
    the assembled prompt registers NO ``retrieved_locations`` / ``retrieved_factions``
    section at all — absence, not an empty ``<retrieved_factions></...>`` tag."""
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _FakeDaemon(_VEC),
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 4
    _seed_npc_fill_card(sd, "wandering_minstrel", "A wandering minstrel, locations unknown.")

    action = "ask around for rumors"
    result = await handler._retrieve_entities_for_turn(sd, action)
    assert result.retrieved_locations is None
    assert result.retrieved_factions is None

    context = _build_turn_context(sd, entity_retrieval=result)
    assert context.retrieved_entity_locations is None
    assert context.retrieved_entity_factions is None

    orch = _make_orchestrator()
    _prompt, registry = await orch.build_narrator_prompt(action, context)
    agent_name = orch._narrator.name()
    valley_names = _valley_section_names(registry, agent_name)
    assert "retrieved_npcs" in valley_names, "sanity: the NPC fill DID register"
    assert "retrieved_locations" not in valley_names
    assert "retrieved_factions" not in valley_names


# ===========================================================================
# 4. Floor-vs-fill dedup — a scene-present (floor) NPC is not double-injected
#    into the semantic fill, even when it is the top cosine hit
# ===========================================================================


@pytest.mark.asyncio
async def test_floor_npc_not_double_injected_into_fill(
    session_handler_factory, monkeypatch
) -> None:
    """ADR-118 §D4 / Architect trap #5: the scene-present floor NPC already rides
    the npc-roster path; its card must be deduped OUT of the fill so it is not
    injected twice — while a genuinely off-stage card still fills. Driven through
    the real delegate so the dedup is proven in production, not in isolation."""
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _FakeDaemon(_VEC),
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 5
    # Borin is scene-present (floor); his projected card id is "npc:borin".
    sd.snapshot.npcs.append(_scene_present_npc("Borin", turn=5))
    # Seed BOTH a card colliding with the floor id and a genuinely off-stage card.
    _seed_npc_fill_card(sd, "borin", "Borin the smith, scene-present.")
    _seed_npc_fill_card(sd, "wandering_minstrel", "A wandering minstrel, off-stage.")

    result = await handler._retrieve_entities_for_turn(sd, "look for help")

    assert result.outcome == "success"
    assert result.retrieved_npcs is not None
    filled_ids = {c.id for c in result.retrieved_npcs}
    assert "npc:borin" not in filled_ids, "the scene-present floor NPC must not appear in the fill"
    assert "npc:wandering_minstrel" in filled_ids, "the off-stage card must still fill"


# ===========================================================================
# 5. Wiring (mandatory) — the GM-panel watcher event reaches a real hub
#    subscriber via the production delegate (the third leg of the arc)
# ===========================================================================


@pytest.mark.asyncio
async def test_event_reaches_watcher_hub_via_production_delegate(
    session_handler_factory, monkeypatch
) -> None:
    """Completes the capstone triangle: the same production delegate that fires the
    span and injects the Valley section also delivers a ``retrieval``-component
    watcher event to a real ``watcher_hub`` subscriber — so the GM panel sees the
    retrieval engaged. Behaviour-driven (real subscriber), not source-grep."""
    from sidequest.telemetry import watcher_hub as wh_module

    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _UnavailableDaemon(),
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 2

    received: list[dict[str, Any]] = []

    class _FakeSocket:
        async def send_json(self, data: dict[str, Any]) -> None:
            received.append(data)

    hub = wh_module.watcher_hub
    hub.bind_loop(asyncio.get_running_loop())
    fake = _FakeSocket()
    await hub.subscribe(fake)
    try:
        await handler._retrieve_entities_for_turn(sd, "shout into the dark")

        retrieval_events: list[dict[str, Any]] = []
        for _ in range(50):
            await asyncio.sleep(0.01)
            retrieval_events = [
                e for e in received if e.get("fields", {}).get("field") == "universal_retrieval"
            ]
            if retrieval_events:
                break

        assert retrieval_events, (
            "a universal_retrieval watcher event must reach a hub subscriber when the "
            "production retrieval delegate runs — the GM panel sees nothing otherwise"
        )
        event = retrieval_events[0]
        assert event["component"] == "retrieval"
        assert event["event_type"] == "state_transition"
        assert event["fields"]["turn_number"] == 2
    finally:
        await hub.unsubscribe(fake)
