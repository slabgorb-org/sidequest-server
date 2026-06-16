"""End-to-end wiring for the state-patch Phase 2 bundle.

Drives ``_apply_narration_result_to_snapshot`` with a ``quest_updates``
payload through a real ``TracerProvider`` + ``WatcherSpanProcessor`` and
asserts the typed ``state_transition`` event with ``component=quest_log``
reaches the hub via ``SPAN_ROUTES[SPAN_QUEST_UPDATE]`` — i.e. the
production code path actually opens the span (not the prior direct
``publish_event`` call this PR replaced).

Per ``CLAUDE.md`` "Verify Wiring, Not Just Existence": the unit test in
``tests/server/test_watcher_events.py`` proves the route extracts the
right fields from a fake span; this proves a real narration apply opens
that span.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.session import GameSnapshot, TurnManager
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests._helpers.session_room import room_for


@pytest.mark.asyncio
async def test_quest_updates_emit_state_transition_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NarrationTurnResult with quest_updates must reach the hub as a
    routed ``state_transition`` (component=quest_log), proving
    ``narration_apply.py`` opens ``quest_update_span`` rather than
    publishing directly."""
    # 1. Bind the module-level hub to this loop and clear any leftover
    #    subscribers from prior tests in the same process.
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    # 2. Install a local TracerProvider with the WatcherSpanProcessor and
    #    monkeypatch ``spans_module.tracer`` to return its tracer. We can't
    #    use ``trace.set_tracer_provider`` because OTEL refuses to replace
    #    a provider that another test in the suite already installed
    #    ("Overriding of current TracerProvider is not allowed"). Patching
    #    the function the helper actually calls is the order-independent
    #    seam.
    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer("test-state-patch-wiring")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    # 3. Drive the production function with a quest_updates payload.
    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="Tood's Dome",
        discovered_regions=["Tood's Dome"],
        quest_log={},
        lore_established=[],
        characters=[],
        turn_manager=TurnManager(),
    )
    snapshot.turn_manager.record_interaction()  # advance interaction counter

    result = NarrationTurnResult(
        narration="Vex offers a deal.",
        quest_updates={"deal_with_vex": "active"},
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Rux", room=room_for(snapshot)
    )

    # Cross-thread coroutine hop needs a tick.
    await asyncio.sleep(0.05)

    # 4. The snapshot must have been mutated (the span context wraps the
    #    mutation; if the helper short-circuits, the assertion below fails
    #    and the test catches it before checking events).
    assert snapshot.quest_log == {"deal_with_vex": "active"}

    # 5. The typed event must have arrived via SPAN_ROUTES, not via a
    #    direct publish_event from narration_apply.
    typed = [
        e
        for e in captured
        if e["event_type"] == "state_transition" and e["component"] == "quest_log"
    ]
    assert typed, (
        "quest_update span never reached the hub as state_transition — "
        "narration_apply may still be using direct _watcher_publish "
        "or the SPAN_ROUTES entry for SPAN_QUEST_UPDATE is missing"
    )
    fields = typed[0]["fields"]
    assert fields["field"] == "quest_log"
    assert fields["updates_count"] == 1
    assert fields["player_name"] == "Rux"
    assert fields["turn_number"] == snapshot.turn_manager.interaction
    # updates is JSON-encoded so OTEL doesn't drop the dict attribute.
    assert fields["updates"] == '{"deal_with_vex": "active"}'

    # 6. And the prior direct publish_event from this code block must
    #    NOT also fire — otherwise the dashboard double-counts. The route
    #    is the single source.
    flat_quest = [
        e
        for e in captured
        if e["event_type"] == "state_transition" and e["component"] == "quest_log"
    ]
    assert len(flat_quest) == 1, (
        f"expected exactly one state_transition for quest_log (got {len(flat_quest)}: {flat_quest})"
    )
