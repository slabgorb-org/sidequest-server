"""RED-phase tests for the per-turn universal-retrieval GM-panel dispatch (Story 75-7).

Sibling of ``tests/server/dispatch/test_entity_sync_dispatch.py`` and
``test_lore_embed.py``. The ``retrieval.universal`` OTEL **span** already ships
(75-5, ``game/retrieval_orchestration.py``) and fires every turn — but it only
reaches OTLP/Jaeger. The **GM panel** (the lie-detector Keith watches during
play) subscribes to the *WatcherHub* event stream, not the raw span pipeline, and
``retrieve_turn_context`` emits no ``publish_event``. So today the universal-
retrieval decision is invisible on the GM panel.

75-7 closes that gap by adding the *watcher-emission half*, mirroring the dual-
emission gold-standard siblings ``entity_sync.sync_for_turn``
(``server/dispatch/entity_sync.py``) and ``lore_embed.retrieve_for_turn``
(``server/dispatch/lore_embed.py``). It does **not** touch the span, the floor+
fill logic, the budget seam, or any UI component (all out of scope — 75-4/75-5/
75-6/75-8).

THE CONTRACT THIS SUITE PINS (the test IS the spec — ADR-118 §D5 + context-story-75-7):

  * ``sidequest.server.dispatch.universal_retrieval.retrieve_for_turn`` (async)
        async def retrieve_for_turn(handler, sd, action) -> RetrievedEntities
    - Calls the 75-5 ``retrieve_turn_context`` (imported into the module
      namespace so this suite can monkeypatch it) and RETURNS its
      ``RetrievedEntities`` unchanged (the caller still builds the prompt from it).
    - Publishes ONE WatcherHub event via the module-level
      ``_watcher_publish`` alias (``publish_event``):
          event_type = "state_transition"
          component  = "retrieval"        # the subsystem label 75-6 established
          fields = {
              "field": "universal_retrieval",
              "op":    <outcome>,          # the RetrievedEntities.outcome string
              "budget_total", "floor_count", "floor_token_cost",
              "fill_candidate_count", "fill_selected_count", "fill_token_cost",
              "npc_count", "location_count", "faction_count",
              "rejected_below_similarity", "dimension_mismatch_count",
              "turn_number": <snapshot.turn_manager.interaction>,
          }
          severity = "info" for success; non-"info" for ``query_failed``.
    - Emission is WRAPPED: a ``publish_event`` failure is logged and SWALLOWED;
      the result is still returned. Observing the turn must never crash the turn.

  * ``WebSocketSessionHandler._retrieve_entities_for_turn`` delegates to
    ``universal_retrieval.retrieve_for_turn`` (the live per-turn seam at
    ``websocket_session_handler.py:2500``), replacing today's direct call to
    ``retrieve_turn_context``.

INTENTIONALLY RED until 75-7 lands — ``sidequest.server.dispatch.universal_retrieval``
does not exist and ``_retrieve_entities_for_turn`` does not delegate to it yet.
Symbols are imported INSIDE each test so collection succeeds and each test fails
with a clear ImportError/AttributeError rather than collapsing the module.

Test discipline (server CLAUDE.md): "No Source-Text Wiring Tests" — the wiring
tests drive the real handler delegate / a real ``watcher_hub`` subscriber and
assert on the *published event*, never on source patterns.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sidequest.agents.npc_context import build_npc_working_set
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager

# ---------------------------------------------------------------------------
# Helpers — craft a known RetrievedEntities and a fake daemon
# ---------------------------------------------------------------------------


def _empty_floor() -> Any:
    """A real (empty) ``NpcWorkingSet`` for the result's ``floor`` field —
    built through the production factory so we never guess its shape."""
    snap = GameSnapshot(genre_slug="caverns_and_claudes", turn_manager=TurnManager(interaction=1))
    return build_npc_working_set(snap, current_turn=1)


def _cards(entity_type: str, n: int) -> list[EntityCard]:
    return [
        EntityCard.new(entity_type, f"{entity_type}_{i}", content=f"card {i}") for i in range(n)
    ]


def _make_result(
    *,
    outcome: str,
    retrieved_npcs: list[EntityCard] | None = None,
    retrieved_locations: list[EntityCard] | None = None,
    retrieved_factions: list[EntityCard] | None = None,
    budget_total: int = 4000,
    floor_count: int = 0,
    floor_token_cost: int = 0,
    fill_candidate_count: int = 0,
    fill_selected_count: int = 0,
    fill_token_cost: int = 0,
    rejected_below_similarity: int = 0,
    dimension_mismatch_count: int = 0,
) -> Any:
    """Build a ``RetrievedEntities`` with explicit, known field values so a
    mapping test can assert the published event carries exactly these numbers."""
    from sidequest.game.retrieval_orchestration import RetrievedEntities

    return RetrievedEntities(
        floor=_empty_floor(),
        retrieved_npcs=retrieved_npcs,
        retrieved_locations=retrieved_locations,
        retrieved_factions=retrieved_factions,
        budget_total=budget_total,
        floor_count=floor_count,
        floor_token_cost=floor_token_cost,
        fill_candidate_count=fill_candidate_count,
        fill_selected_count=fill_selected_count,
        fill_token_cost=fill_token_cost,
        rejected_below_similarity=rejected_below_similarity,
        dimension_mismatch_count=dimension_mismatch_count,
        outcome=outcome,
    )


class _UnavailableDaemon:
    """``DaemonClient`` stand-in whose ``is_available()`` is False — drives
    ``retrieve_turn_context`` to the deterministic ``query_failed`` outcome
    (fast, no socket) for the real-chain wiring/span tests."""

    def is_available(self) -> bool:
        return False

    async def embed(self, text: str) -> dict[str, Any]:  # pragma: no cover - never reached
        raise RuntimeError("embed should not be called when is_available() is False")


def _capture_publish(monkeypatch, module) -> list[tuple]:
    """Patch ``module._watcher_publish`` with a recorder; return the capture
    list of ``(event_type, fields, component, severity)`` tuples."""
    captured: list[tuple] = []

    def _spy(event_type, fields, component=None, severity="info", **kwargs):
        captured.append((event_type, fields, component, severity))

    monkeypatch.setattr(module, "_watcher_publish", _spy)
    return captured


def _retrieval_events(captured: list[tuple]) -> list[tuple]:
    return [c for c in captured if c[1].get("field") == "universal_retrieval"]


# ===========================================================================
# 1. Module surface + signature (import guard)
# ===========================================================================


def test_dispatch_module_exposes_retrieve_for_turn() -> None:
    """The net-new dispatch module must export ``retrieve_for_turn`` as an
    async function, mirroring ``lore_embed.retrieve_for_turn``."""
    from sidequest.server.dispatch import universal_retrieval

    assert hasattr(universal_retrieval, "retrieve_for_turn")
    assert asyncio.iscoroutinefunction(universal_retrieval.retrieve_for_turn), (
        "retrieve_for_turn awaits retrieve_turn_context (which embeds via the "
        "daemon) — it must be a coroutine; a sync def would never execute the await"
    )


# ===========================================================================
# 2. Handler delegate wiring guard (mirror lore_embed/entity_sync)
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_delegate_calls_retrieve_for_turn(
    session_handler_factory, monkeypatch
) -> None:
    """``_retrieve_entities_for_turn`` must delegate to
    ``universal_retrieval.retrieve_for_turn`` (and return its result),
    replacing today's direct ``retrieve_turn_context`` call."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sentinel = _make_result(outcome="no_candidates")
    captured: list[tuple] = []

    async def _spy(h, sd_arg, action):
        captured.append((h, sd_arg, action))
        return sentinel

    monkeypatch.setattr(universal_retrieval, "retrieve_for_turn", _spy)

    result = await handler._retrieve_entities_for_turn(sd, "look around")

    assert result is sentinel
    assert captured == [(handler, sd, "look around")]


@pytest.mark.asyncio
async def test_retrieve_for_turn_returns_orchestrator_result_unchanged(
    session_handler_factory, monkeypatch
) -> None:
    """The wrapper observes but does not transform: the ``RetrievedEntities``
    it returns is exactly the one ``retrieve_turn_context`` produced (the prompt
    builder downstream still needs the floor + fill)."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    crafted = _make_result(outcome="success", retrieved_npcs=_cards(EntityType.NPC, 2))

    async def _fake_retrieve(*args, **kwargs):
        return crafted

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    _capture_publish(monkeypatch, universal_retrieval)

    result = await universal_retrieval.retrieve_for_turn(handler, sd, "greet the smith")

    assert result is crafted


@pytest.mark.asyncio
async def test_retrieve_for_turn_passes_current_turn_to_orchestrator(
    session_handler_factory, monkeypatch
) -> None:
    """Behavior preservation: the wrapper must call ``retrieve_turn_context``
    with the snapshot's current interaction as ``current_turn`` (today's
    ``_retrieve_entities_for_turn`` contract)."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 9
    seen: dict[str, Any] = {}

    async def _fake_retrieve(entity_store, snapshot, action_text, **kwargs):
        seen["current_turn"] = kwargs.get("current_turn")
        seen["action_text"] = action_text
        return _make_result(outcome="no_candidates")

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    _capture_publish(monkeypatch, universal_retrieval)

    await universal_retrieval.retrieve_for_turn(handler, sd, "kick the door")

    assert seen["current_turn"] == 9
    assert seen["action_text"] == "kick the door"


# ===========================================================================
# 3. The core new behavior — a GM-panel watcher event is published
# ===========================================================================


@pytest.mark.asyncio
async def test_emits_state_transition_for_retrieval_component(
    session_handler_factory, monkeypatch
) -> None:
    """AC-2: every turn that runs retrieval publishes exactly one
    ``state_transition`` event with ``component='retrieval'`` and
    ``field='universal_retrieval'`` — the dual-emission half the siblings have
    and universal retrieval lacks today."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 3

    async def _fake_retrieve(*args, **kwargs):
        return _make_result(outcome="success", retrieved_npcs=_cards(EntityType.NPC, 1))

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    captured = _capture_publish(monkeypatch, universal_retrieval)

    await universal_retrieval.retrieve_for_turn(handler, sd, "look around")

    events = _retrieval_events(captured)
    assert len(events) == 1, "exactly one universal_retrieval watcher event per turn"
    event_type, fields, component, _severity = events[0]
    assert event_type == "state_transition"
    assert component == "retrieval"
    assert fields["field"] == "universal_retrieval"
    assert fields["op"] == "success"
    assert fields["turn_number"] == 3


@pytest.mark.asyncio
async def test_event_carries_all_d5_counts_losslessly(session_handler_factory, monkeypatch) -> None:
    """AC-3: the full ADR-118 §D5 attribute set reaches the panel with the
    SAME values the span holds — floor/fill costs, candidate/selected counts,
    per-type counts, rejection + dimension-mismatch metrics, and the budget."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.snapshot.turn_manager.interaction = 5

    crafted = _make_result(
        outcome="success",
        retrieved_npcs=_cards(EntityType.NPC, 2),
        retrieved_locations=_cards(EntityType.LOCATION, 1),
        retrieved_factions=_cards(EntityType.FACTION, 1),
        budget_total=4000,
        floor_count=2,
        floor_token_cost=120,
        fill_candidate_count=6,
        fill_selected_count=4,
        fill_token_cost=300,
        rejected_below_similarity=2,
        dimension_mismatch_count=1,
    )

    async def _fake_retrieve(*args, **kwargs):
        return crafted

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    captured = _capture_publish(monkeypatch, universal_retrieval)

    await universal_retrieval.retrieve_for_turn(handler, sd, "survey the hall")

    fields = _retrieval_events(captured)[0][1]
    assert fields["budget_total"] == 4000
    assert fields["floor_count"] == 2
    assert fields["floor_token_cost"] == 120
    assert fields["fill_candidate_count"] == 6
    assert fields["fill_selected_count"] == 4
    assert fields["fill_token_cost"] == 300
    assert fields["npc_count"] == 2
    assert fields["location_count"] == 1
    assert fields["faction_count"] == 1
    assert fields["rejected_below_similarity"] == 2
    assert fields["dimension_mismatch_count"] == 1
    # AC-3 invariant: per-type fill counts sum to fill_selected_count.
    assert (
        fields["npc_count"] + fields["location_count"] + fields["faction_count"]
        == fields["fill_selected_count"]
    )


@pytest.mark.asyncio
async def test_per_type_counts_are_zero_when_type_absent(
    session_handler_factory, monkeypatch
) -> None:
    """AC-3 edge: a type that retrieved nothing (``None`` fill list) maps to a
    count of 0, never a missing key or a fabricated number."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")

    async def _fake_retrieve(*args, **kwargs):
        return _make_result(
            outcome="success",
            retrieved_npcs=_cards(EntityType.NPC, 3),
            retrieved_locations=None,
            retrieved_factions=None,
            fill_selected_count=3,
        )

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    captured = _capture_publish(monkeypatch, universal_retrieval)

    await universal_retrieval.retrieve_for_turn(handler, sd, "talk")

    fields = _retrieval_events(captured)[0][1]
    assert fields["npc_count"] == 3
    assert fields["location_count"] == 0
    assert fields["faction_count"] == 0


# ===========================================================================
# 4. No Silent Fallbacks — every failure outcome reaches the panel distinctly
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "budget_exhausted", "query_failed", "no_candidates"]
)
async def test_outcome_string_reaches_event_uncoerced(
    session_handler_factory, monkeypatch, outcome
) -> None:
    """AC-4 / §D5: each distinct ``outcome`` surfaces on the event as-is —
    never coerced to ``success``, never collapsed to a generic 'retrieval ran'.
    A failure that looks like a success on the GM panel defeats the lie-detector."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")

    async def _fake_retrieve(*args, **kwargs):
        return _make_result(outcome=outcome)

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    captured = _capture_publish(monkeypatch, universal_retrieval)

    await universal_retrieval.retrieve_for_turn(handler, sd, "act")

    assert _retrieval_events(captured)[0][1]["op"] == outcome


@pytest.mark.asyncio
async def test_query_failed_event_severity_is_not_info(
    session_handler_factory, monkeypatch
) -> None:
    """AC-4: a real degradation (``query_failed`` — daemon down / embed error)
    must carry a non-``info`` severity so it stands out on the panel (mirror
    ``lore_embed`` / ``entity_sync`` failure events at ``severity='error'``).
    A success stays ``info``."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")

    async def _make(outcome):
        async def _fake_retrieve(*args, **kwargs):
            return _make_result(outcome=outcome)

        return _fake_retrieve

    # query_failed → non-info
    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", await _make("query_failed"))
    cap_fail = _capture_publish(monkeypatch, universal_retrieval)
    await universal_retrieval.retrieve_for_turn(handler, sd, "act")
    assert _retrieval_events(cap_fail)[0][3] != "info"

    # success → info
    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", await _make("success"))
    cap_ok = _capture_publish(monkeypatch, universal_retrieval)
    await universal_retrieval.retrieve_for_turn(handler, sd, "act")
    assert _retrieval_events(cap_ok)[0][3] == "info"


# ===========================================================================
# 5. Zero-byte-leak — an empty/clean-skip turn still publishes a zeroed event
# ===========================================================================


@pytest.mark.asyncio
async def test_no_candidates_publishes_zeroed_event_not_suppressed(
    session_handler_factory, monkeypatch
) -> None:
    """AC-5: a turn that retrieved nothing (``no_candidates``) still emits an
    event with zeroed per-type counts and the ``no_candidates`` outcome — the GM
    must distinguish 'retrieval ran and found nothing' from 'retrieval never
    fired'. The event is NOT suppressed on an empty fill."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")

    async def _fake_retrieve(*args, **kwargs):
        return _make_result(
            outcome="no_candidates",
            fill_candidate_count=0,
            fill_selected_count=0,
        )

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    captured = _capture_publish(monkeypatch, universal_retrieval)

    await universal_retrieval.retrieve_for_turn(handler, sd, "wander an empty hall")

    events = _retrieval_events(captured)
    assert len(events) == 1, "empty-fill turn must still publish (not suppress) the event"
    fields = events[0][1]
    assert fields["op"] == "no_candidates"
    assert fields["npc_count"] == 0
    assert fields["location_count"] == 0
    assert fields["faction_count"] == 0
    assert fields["fill_selected_count"] == 0


# ===========================================================================
# 6. Failure isolation — observing the turn must never crash the turn (ADR-006)
# ===========================================================================


@pytest.mark.asyncio
async def test_publish_failure_is_swallowed_and_result_returned(
    session_handler_factory, monkeypatch
) -> None:
    """AC-6: if ``publish_event`` raises (serialize failure, dead loop), the
    wrapper logs and SWALLOWS it and STILL returns the retrieval result. The
    emission is best-effort; a broken GM panel must not cost the player their
    narration."""
    from sidequest.server.dispatch import universal_retrieval

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    crafted = _make_result(outcome="success", retrieved_npcs=_cards(EntityType.NPC, 1))

    async def _fake_retrieve(*args, **kwargs):
        return crafted

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated watcher publish failure")

    monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
    monkeypatch.setattr(universal_retrieval, "_watcher_publish", _boom)

    # MUST NOT raise — and must still hand back the result for prompt assembly.
    result = await universal_retrieval.retrieve_for_turn(handler, sd, "look")

    assert result is crafted


@pytest.mark.asyncio
async def test_narration_turn_survives_emit_failure(session_handler_factory, monkeypatch) -> None:
    """AC-6 production path: a raise in the emit step does not propagate out of
    the live per-turn pipeline — the player's narration is delivered anyway.
    The strongest failure-isolation guard (drives the real turn)."""
    from unittest.mock import AsyncMock

    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.dispatch import universal_retrieval
    from sidequest.server.session_helpers import _build_turn_context

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You survey the room.")
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated watcher publish failure")

    monkeypatch.setattr(universal_retrieval, "_watcher_publish", _boom)

    # MUST NOT raise — the turn completes despite a broken emit.
    await handler._execute_narration_turn(sd, "look around", _build_turn_context(sd))


# ===========================================================================
# 7. Wiring test (mandatory) — the event reaches the hub via the production path
# ===========================================================================


@pytest.mark.asyncio
async def test_event_reaches_watcher_hub_subscriber_via_handler(
    session_handler_factory, monkeypatch
) -> None:
    """AC-7 (CLAUDE.md "Every Test Suite Needs a Wiring Test"): driving the real
    handler delegate ``_retrieve_entities_for_turn`` (the live seam at
    websocket_session_handler.py:2500) results in a ``retrieval``-component
    watcher event DELIVERED to a real ``watcher_hub`` subscriber.

    Behavior-driven, not source-grep: subscribe a fake ``_Sendable``, bind the
    hub loop, run the production delegate, and assert the event landed. The
    daemon is forced unavailable so the real ``retrieve_turn_context`` returns a
    deterministic ``query_failed`` outcome (fast, no socket) — but the wiring
    we are proving is identical for every outcome: handler → dispatch module →
    publish_event → watcher_hub → subscriber."""
    from sidequest.telemetry import watcher_hub as wh_module

    # Force the real orchestrator down its deterministic degraded path.
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

        # publish() schedules the broadcast on the loop; let it run.
        retrieval_events: list[dict[str, Any]] = []
        for _ in range(50):
            await asyncio.sleep(0.01)
            retrieval_events = [
                e for e in received if e.get("fields", {}).get("field") == "universal_retrieval"
            ]
            if retrieval_events:
                break

        assert retrieval_events, (
            "a universal_retrieval watcher event must reach a hub subscriber when "
            "the production retrieval delegate runs — the GM panel sees nothing otherwise"
        )
        event = retrieval_events[0]
        assert event["component"] == "retrieval"
        assert event["event_type"] == "state_transition"
        assert event["fields"]["op"] == "query_failed"
        assert event["fields"]["turn_number"] == 2
    finally:
        await hub.unsubscribe(fake)


# ===========================================================================
# 8. AC-1 regression guard — the wrapper does not perturb the 75-5 span
# ===========================================================================


@pytest.mark.asyncio
async def test_wrapper_does_not_double_emit_the_universal_span(
    session_handler_factory, monkeypatch
) -> None:
    """AC-1: 75-7's added watcher emission must not disturb the 75-5
    ``retrieval.universal`` OTEL span — exactly ONE span per turn, still
    carrying its ``retrieval.outcome`` attribute. Guards against a regression
    where the wrapper accidentally re-enters or wraps the span a second time."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.server.dispatch import universal_retrieval

    # Force the real retrieve_turn_context down a deterministic path (it still
    # emits its span). Daemon unavailable → query_failed, span fires once.
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.DaemonClient",
        lambda *a, **kw: _UnavailableDaemon(),
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "sidequest.game.retrieval_orchestration.tracer",
        provider.get_tracer("test-universal-retrieval"),
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _capture_publish(monkeypatch, universal_retrieval)

    await universal_retrieval.retrieve_for_turn(handler, sd, "peer around")

    spans = [s for s in exporter.get_finished_spans() if s.name == "retrieval.universal"]
    assert len(spans) == 1, (
        f"exactly one retrieval.universal span per turn; got {len(spans)} "
        f"(all spans: {[s.name for s in exporter.get_finished_spans()]})"
    )
    assert spans[0].attributes.get("retrieval.outcome") == "query_failed"
