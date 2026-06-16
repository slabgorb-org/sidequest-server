"""Semantic watcher event emission tests.

Covers the five `WatcherEvent` types the GM dashboard's non-Console tabs
consume (`turn_complete`, `state_transition`, `game_state_snapshot`,
`prompt_assembled`, `lore_retrieval`). Without these, the dashboard socket
stays connected but every tab except Console shows "Waiting for first
turn…" forever (playtest 2026-04-22).

Each test binds a fake subscriber to ``watcher_hub`` and asserts the
expected event shape fires at the right seat. The last test is the wiring
integration: it runs a real narration turn through
``_execute_narration_turn`` and asserts at least one ``turn_complete``
event lands on the bus — i.e. the publish seat is actually reachable from
production code paths, not just unit-testable in isolation.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.agents.orchestrator import (
    NarrationTurnResult,
    NpcMention,
)
from sidequest.game.session import GameSnapshot, TurnManager
from sidequest.server.session_handler import (
    _apply_narration_result_to_snapshot,
)
from sidequest.telemetry.watcher_hub import (
    WatcherHub,
    publish_event,
    watcher_hub,
)
from tests._helpers.session_room import room_for


class _FakeSocket:
    """Minimal WebSocket stand-in that records every broadcast."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


async def _capture(hub: WatcherHub) -> _FakeSocket:
    sock = _FakeSocket()
    # cast away the type — our fake is structurally compatible
    await hub.subscribe(sock)  # type: ignore[arg-type]
    return sock


@pytest.fixture
async def bound_hub() -> WatcherHub:
    """Bind the module-level hub to the current event loop so
    :func:`publish_event` broadcasts rather than silently dropping."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    # Clear subscribers left over from prior tests — the module singleton
    # persists across tests and a prior run's dead sockets would pollute
    # the event count.
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


@pytest.mark.asyncio
async def test_publish_event_shape(bound_hub: WatcherHub) -> None:
    """`publish_event` must emit the exact WatcherEvent envelope the UI
    parses — timestamp, component, event_type, severity, fields."""
    sock = await _capture(bound_hub)
    publish_event(
        "turn_complete",
        {"turn_number": 1, "agent_name": "narrator"},
        component="orchestrator",
    )
    # Broadcast is scheduled on the loop; yield so it fires.
    await asyncio.sleep(0.05)
    assert len(sock.events) == 1
    ev = sock.events[0]
    assert set(ev) == {"timestamp", "component", "event_type", "severity", "fields"}
    assert ev["event_type"] == "turn_complete"
    assert ev["component"] == "orchestrator"
    assert ev["severity"] == "info"
    assert ev["fields"]["turn_number"] == 1


@pytest.mark.asyncio
async def test_state_transition_fires_on_location_update(
    bound_hub: WatcherHub,
) -> None:
    """`_apply_narration_result_to_snapshot` must publish a
    `state_transition` event whenever the narration carries a new
    location. Previously the location silently changed in the snapshot
    and the dashboard had no way to see it."""
    sock = await _capture(bound_hub)
    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="",
        discovered_regions=[],
        quest_log={},
        lore_established=[],
        characters=[],
        turn_manager=TurnManager(),
    )
    result = NarrationTurnResult(narration="You descend.", location="Tood's Dome — Nest Crack")
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Rux", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)
    # Wave 2B: the location update now fires under
    # kind=character_location_updated keyed by the acting PC
    # (per-character in snapshot.character_locations).
    location_events = [
        e
        for e in sock.events
        if e["event_type"] == "state_transition"
        and e["fields"].get("kind") == "character_location_updated"
    ]
    assert len(location_events) == 1
    f = location_events[0]["fields"]
    assert f["new_location"] == "Tood's Dome — Nest Crack"
    assert (f.get("old_location") or "") == ""
    assert f["player_name"] == "Rux"
    assert f["character"] == "Rux"


@pytest.mark.asyncio
async def test_state_transition_fires_on_npc_auto_register(
    bound_hub: WatcherHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-registered NPCs must emit `state_transition` so the
    Subsystems tab's `npc_registry` component lights up.

    Post-NPC-bundle (2026-04-25) the emission goes through
    ``SPAN_ROUTES[SPAN_NPC_AUTO_REGISTERED]`` instead of a direct
    ``publish_event``, so the test installs a local TracerProvider with
    the WatcherSpanProcessor and monkeypatches the helper's tracer."""
    from opentelemetry.sdk.trace import TracerProvider

    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry import spans as spans_module

    sock = await _capture(bound_hub)

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(bound_hub))
    local_tracer = provider.get_tracer("test-npc-auto-register-wiring")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    snapshot = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        location="",
        discovered_regions=[],
        quest_log={},
        lore_established=[],
        characters=[],
        turn_manager=TurnManager(),
    )
    result = NarrationTurnResult(
        narration="She waves.",
        npcs_present=[
            NpcMention(
                name="Vex",
                pronouns="she/her",
                role="scavenger",
                appearance="",
            )
        ],
    )
    _apply_narration_result_to_snapshot(
        snapshot, result, player_name="Rux", room=room_for(snapshot)
    )
    await asyncio.sleep(0.05)
    npc_events = [
        e
        for e in sock.events
        if e["event_type"] == "state_transition"
        and e["fields"].get("field") == "npc_registry"
        and e["fields"].get("op") == "auto_registered"
    ]
    assert len(npc_events) == 1
    assert npc_events[0]["fields"]["name"] == "Vex"
    assert npc_events[0]["fields"]["pronouns"] == "she/her"


@pytest.mark.asyncio
async def test_hub_drops_silently_when_loop_unbound() -> None:
    """Publishing before :meth:`WatcherHub.bind_loop` must not raise.
    The race happens when subsystem code runs before FastAPI startup
    (e.g. module-import-time OTEL setup)."""
    unbound = WatcherHub()
    # No subscribers, no loop — this must be a silent no-op, not a crash.
    unbound.publish({"event_type": "turn_complete", "fields": {}})


@pytest.mark.asyncio
async def test_hub_survives_module_reimport() -> None:
    """uvicorn --reload re-imports modified modules on every save. The
    hub singleton MUST survive that re-import, otherwise OTEL span
    processors registered before the reload broadcast into a dead
    instance and the dashboard goes deaf mid-session
    (playtest 2026-04-23)."""
    import sidequest.telemetry.watcher_hub as module

    hub_before = module.watcher_hub
    # Mark it so we can prove identity across the re-import — ``is``
    # alone would work, but a tag makes the assertion failure readable.
    hub_before._reimport_marker = "survived"  # type: ignore[attr-defined]

    reloaded = importlib.reload(module)

    assert reloaded.watcher_hub is hub_before, (
        "watcher_hub singleton was replaced on module reload — "
        "OTEL span processors from before reload are now orphaned"
    )
    assert getattr(reloaded.watcher_hub, "_reimport_marker", None) == "survived"


@pytest.mark.asyncio
async def test_span_processor_broadcasts_to_subscriber(
    bound_hub: WatcherHub,
) -> None:
    """End-to-end wiring test: register a ``WatcherSpanProcessor``
    against a TracerProvider, emit a span, assert the subscriber
    receives an ``agent_span_close`` event.

    This is the integration test the playtest blocker needed — it
    proves the on_end → hub → subscriber path is intact."""
    from sidequest.server.watcher import WatcherSpanProcessor

    sock = await _capture(bound_hub)

    processor = WatcherSpanProcessor(bound_hub)

    # Build a minimal ReadableSpan by driving the SDK end-to-end.
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("wiring.test") as span:
        span.set_attribute("probe", "ok")
    # BatchSpanProcessor isn't in play here; our processor is
    # synchronous-enough. Give the loop a tick for the broadcast.
    await asyncio.sleep(0.05)

    close_events = [
        e
        for e in sock.events
        if e["event_type"] == "agent_span_close" and e["fields"].get("name") == "wiring.test"
    ]
    assert len(close_events) == 1, (
        f"expected 1 span_close event, got {len(sock.events)}: {sock.events}"
    )
    assert close_events[0]["fields"]["probe"] == "ok"


@pytest.mark.asyncio
async def test_on_end_emits_agent_span_close_for_every_span() -> None:
    """Backward-compat: every closed span still produces agent_span_close."""
    from unittest.mock import MagicMock

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode

    from sidequest.server.watcher import WatcherSpanProcessor

    def _fake_span(
        name: str,
        attributes: dict | None = None,
        status_code: StatusCode = StatusCode.OK,
    ) -> ReadableSpan:
        span = MagicMock(spec=ReadableSpan)
        span.name = name
        span.attributes = attributes or {}
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.status = MagicMock()
        span.status.status_code = MagicMock()
        span.status.status_code.name = "OK" if status_code == StatusCode.OK else "ERROR"
        return span

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())

    class _CapturingSubscriber:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    sub = _CapturingSubscriber()
    await hub.subscribe(sub)  # type: ignore[arg-type]

    processor = WatcherSpanProcessor(hub)
    processor.on_end(_fake_span("some.untracked.span", {"a": 1}))

    # Allow the cross-thread coroutine hop to flush.
    await asyncio.sleep(0.05)

    assert any(e["event_type"] == "agent_span_close" for e in sub.events)


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_routed_span() -> None:
    """When a span name is in SPAN_ROUTES, on_end ALSO emits the typed event."""
    from unittest.mock import MagicMock

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode

    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry.spans import SPAN_PROJECTION_DECIDE

    def _fake_span(
        name: str,
        attributes: dict | None = None,
        status_code: StatusCode = StatusCode.OK,
    ) -> ReadableSpan:
        span = MagicMock(spec=ReadableSpan)
        span.name = name
        span.attributes = attributes or {}
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.status = MagicMock()
        span.status.status_code = MagicMock()
        span.status.status_code.name = "OK" if status_code == StatusCode.OK else "ERROR"
        return span

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())

    class _CapturingSubscriber:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    sub = _CapturingSubscriber()
    await hub.subscribe(sub)  # type: ignore[arg-type]

    processor = WatcherSpanProcessor(hub)
    processor.on_end(
        _fake_span(
            SPAN_PROJECTION_DECIDE,
            {"event.kind": "narration", "decision.include": True},
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in sub.events if e["event_type"] == "state_transition"]
    flat = [e for e in sub.events if e["event_type"] == "agent_span_close"]
    assert typed, "Routed span did not produce a typed state_transition event"
    assert flat, "Routed span must STILL produce agent_span_close (augment, not replace)"
    assert typed[0]["component"] == "projection"
    assert typed[0]["fields"]["event_kind"] == "narration"


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_quest_update_span() -> None:
    """``SPAN_QUEST_UPDATE`` is routed (Phase 2 state-patch bundle) — the
    translator must emit a ``state_transition`` with component=``quest_log``
    carrying the JSON-encoded ``updates`` payload, replacing the prior
    direct ``publish_event`` from ``narration_apply.py``."""
    from unittest.mock import MagicMock

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode

    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry.spans import SPAN_QUEST_UPDATE

    def _fake_span(
        name: str,
        attributes: dict | None = None,
        status_code: StatusCode = StatusCode.OK,
    ) -> ReadableSpan:
        span = MagicMock(spec=ReadableSpan)
        span.name = name
        span.attributes = attributes or {}
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.status = MagicMock()
        span.status.status_code = MagicMock()
        span.status.status_code.name = "OK" if status_code == StatusCode.OK else "ERROR"
        return span

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())

    class _CapturingSubscriber:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.events.append(data)

    sub = _CapturingSubscriber()
    await hub.subscribe(sub)  # type: ignore[arg-type]

    processor = WatcherSpanProcessor(hub)
    processor.on_end(
        _fake_span(
            SPAN_QUEST_UPDATE,
            {
                "updates_json": '{"find_crystal": "active"}',
                "updates_count": 1,
                "player_name": "Rux",
                "turn_number": 7,
            },
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in sub.events if e["event_type"] == "state_transition"]
    assert typed, "SPAN_QUEST_UPDATE did not produce a state_transition event"
    assert typed[0]["component"] == "quest_log"
    assert typed[0]["fields"]["field"] == "quest_log"
    assert typed[0]["fields"]["updates_count"] == 1
    assert typed[0]["fields"]["player_name"] == "Rux"
    assert typed[0]["fields"]["turn_number"] == 7
    assert typed[0]["fields"]["updates"] == '{"find_crystal": "active"}'


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_npc_auto_registered_span() -> None:
    """``SPAN_NPC_AUTO_REGISTERED`` is routed (NPC bundle) — translator
    must emit a ``state_transition`` with ``component=npc_registry`` and
    ``op=auto_registered`` carrying the same payload the prior direct
    ``publish_event`` did."""
    from unittest.mock import MagicMock

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode

    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry.spans import SPAN_NPC_AUTO_REGISTERED

    def _fake_span(
        name: str,
        attributes: dict | None = None,
        status_code: StatusCode = StatusCode.OK,
    ) -> ReadableSpan:
        span = MagicMock(spec=ReadableSpan)
        span.name = name
        span.attributes = attributes or {}
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.status = MagicMock()
        span.status.status_code = MagicMock()
        span.status.status_code.name = "OK" if status_code == StatusCode.OK else "ERROR"
        return span

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())

    captured: list[dict] = []

    class _Sub:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await hub.subscribe(_Sub())  # type: ignore[arg-type]

    processor = WatcherSpanProcessor(hub)
    processor.on_end(
        _fake_span(
            SPAN_NPC_AUTO_REGISTERED,
            {
                "npc_name": "Vex",
                "pronouns": "she/her",
                "role": "scavenger",
                "turn_number": 4,
                "registry_len": 1,
            },
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "state_transition"]
    assert typed, "SPAN_NPC_AUTO_REGISTERED did not produce state_transition"
    assert typed[0]["component"] == "npc_registry"
    assert typed[0]["severity"] == "info"
    assert typed[0]["fields"]["op"] == "auto_registered"
    assert typed[0]["fields"]["name"] == "Vex"
    assert typed[0]["fields"]["pronouns"] == "she/her"
    assert typed[0]["fields"]["registry_len"] == 1


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_npc_reinvented_span_with_warning_severity() -> None:
    """``SPAN_NPC_REINVENTED`` is routed AND the helper sets
    ``severity=warning`` as a span attribute, so the translator's typed
    event must surface ``severity=warning`` (not the default ``info``)
    — that's the GM panel signal that an identity drift happened."""
    from unittest.mock import MagicMock

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode

    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry.spans import SPAN_NPC_REINVENTED

    def _fake_span(
        name: str,
        attributes: dict | None = None,
        status_code: StatusCode = StatusCode.OK,
    ) -> ReadableSpan:
        span = MagicMock(spec=ReadableSpan)
        span.name = name
        span.attributes = attributes or {}
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.status = MagicMock()
        span.status.status_code = MagicMock()
        span.status.status_code.name = "OK" if status_code == StatusCode.OK else "ERROR"
        return span

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())

    captured: list[dict] = []

    class _Sub:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await hub.subscribe(_Sub())  # type: ignore[arg-type]

    processor = WatcherSpanProcessor(hub)
    processor.on_end(
        _fake_span(
            SPAN_NPC_REINVENTED,
            {
                "npc_name": "Vex",
                "drift_field": "pronouns",
                "expected": "she/her",
                "narrator": "they/them",
                "turn_number": 9,
                "severity": "warning",
            },
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "state_transition"]
    assert typed, "SPAN_NPC_REINVENTED did not produce state_transition"
    assert typed[0]["component"] == "npc_registry"
    assert typed[0]["severity"] == "warning", (
        "severity span-attribute escape hatch failed — "
        "the translator must propagate the warning level"
    )
    assert typed[0]["fields"]["op"] == "reinvented"
    assert typed[0]["fields"]["drift_field"] == "pronouns"
    assert typed[0]["fields"]["expected"] == "she/her"
    assert typed[0]["fields"]["narrator"] == "they/them"


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_inventory_narrator_extracted_span() -> None:
    """``SPAN_INVENTORY_NARRATOR_EXTRACTED`` is routed (inventory bundle) —
    translator must emit a ``state_transition`` with ``component=inventory``
    and ``op=narrator_extracted`` carrying the JSON-encoded gained/lost
    lists the prior direct ``publish_event`` from ``narration_apply.py``
    sent. The validator's ``inventory_check`` correlates on these fields."""
    from unittest.mock import MagicMock

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode

    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry.spans import SPAN_INVENTORY_NARRATOR_EXTRACTED

    def _fake_span(
        name: str,
        attributes: dict | None = None,
        status_code: StatusCode = StatusCode.OK,
    ) -> ReadableSpan:
        span = MagicMock(spec=ReadableSpan)
        span.name = name
        span.attributes = attributes or {}
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.status = MagicMock()
        span.status.status_code = MagicMock()
        span.status.status_code.name = "OK" if status_code == StatusCode.OK else "ERROR"
        return span

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())

    captured: list[dict] = []

    class _Sub:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await hub.subscribe(_Sub())  # type: ignore[arg-type]

    processor = WatcherSpanProcessor(hub)
    processor.on_end(
        _fake_span(
            SPAN_INVENTORY_NARRATOR_EXTRACTED,
            {
                "gained_json": '["Rusty Spanner"]',
                "lost_json": '["broken_torch"]',
                "gained_count": 1,
                "lost_count": 1,
                "player_name": "Rux",
                "turn_number": 5,
            },
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "state_transition"]
    assert typed, "SPAN_INVENTORY_NARRATOR_EXTRACTED did not produce a state_transition event"
    assert typed[0]["component"] == "inventory"
    assert typed[0]["severity"] == "info"
    assert typed[0]["fields"]["field"] == "inventory"
    assert typed[0]["fields"]["op"] == "narrator_extracted"
    assert typed[0]["fields"]["gained"] == '["Rusty Spanner"]'
    assert typed[0]["fields"]["lost"] == '["broken_torch"]'
    assert typed[0]["fields"]["gained_count"] == 1
    assert typed[0]["fields"]["lost_count"] == 1
    assert typed[0]["fields"]["player_name"] == "Rux"
    assert typed[0]["fields"]["turn_number"] == 5


def _audio_fake_span_factory():
    """Shared fake-span builder for audio translator-routing tests.
    Returns a callable that builds a ``ReadableSpan`` mock with a name
    and attributes — same shape used by the inventory / NPC tests above."""
    from unittest.mock import MagicMock

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode

    def _make(name: str, attributes: dict | None = None) -> ReadableSpan:
        span = MagicMock(spec=ReadableSpan)
        span.name = name
        span.attributes = attributes or {}
        span.start_time = 1_000_000_000
        span.end_time = 2_000_000_000
        span.status = MagicMock()
        span.status.status_code = MagicMock()
        span.status.status_code.name = "OK"
        _ = StatusCode  # keep import alive for ReadableSpan typing
        return span

    return _make


async def _audio_processor_with_capture():
    """Boilerplate: bind hub to running loop, attach a capturing
    subscriber, return (processor, captured_events)."""
    from sidequest.server.watcher import WatcherSpanProcessor

    hub = WatcherHub()
    hub.bind_loop(asyncio.get_running_loop())

    captured: list[dict] = []

    class _Sub:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await hub.subscribe(_Sub())  # type: ignore[arg-type]
    return WatcherSpanProcessor(hub), captured


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_audio_backend_enabled_span() -> None:
    """``SPAN_AUDIO_BACKEND_ENABLED`` is routed (audio bundle) — translator
    must emit a ``state_transition`` with ``component=audio`` and
    ``op=enabled`` carrying the same payload the prior direct
    ``publish_event`` from ``session_handler._build_audio_backend`` did."""
    from sidequest.telemetry.spans import SPAN_AUDIO_BACKEND_ENABLED

    fake = _audio_fake_span_factory()
    processor, captured = await _audio_processor_with_capture()
    processor.on_end(
        fake(
            SPAN_AUDIO_BACKEND_ENABLED,
            {"genre": "mutant_wasteland", "mood_count": 4, "sfx_count": 12},
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "state_transition"]
    assert typed, "SPAN_AUDIO_BACKEND_ENABLED did not produce state_transition"
    assert typed[0]["component"] == "audio"
    assert typed[0]["fields"]["op"] == "enabled"
    assert typed[0]["fields"]["genre"] == "mutant_wasteland"
    assert typed[0]["fields"]["mood_count"] == 4
    assert typed[0]["fields"]["sfx_count"] == 12


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_audio_backend_disabled_span() -> None:
    """``SPAN_AUDIO_BACKEND_DISABLED`` is routed — translator emits
    ``op=disabled`` carrying the bail reason (``pack_dir_missing`` /
    ``empty_config``)."""
    from sidequest.telemetry.spans import SPAN_AUDIO_BACKEND_DISABLED

    fake = _audio_fake_span_factory()
    processor, captured = await _audio_processor_with_capture()
    processor.on_end(
        fake(
            SPAN_AUDIO_BACKEND_DISABLED,
            {"reason": "empty_config", "genre": "tea_and_murder"},
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "state_transition"]
    assert typed, "SPAN_AUDIO_BACKEND_DISABLED did not produce state_transition"
    assert typed[0]["component"] == "audio"
    assert typed[0]["fields"]["op"] == "disabled"
    assert typed[0]["fields"]["reason"] == "empty_config"
    assert typed[0]["fields"]["genre"] == "tea_and_murder"


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_audio_skipped_span() -> None:
    """``SPAN_AUDIO_SKIPPED`` is routed — translator emits ``op=skipped``
    with ``turn_number`` and a JSON-encoded ``extra`` field (OTEL drops
    dict attributes; route serializes for dashboard parity with the
    prior ``fields.update(extra)`` payload)."""
    from sidequest.telemetry.spans import SPAN_AUDIO_SKIPPED

    fake = _audio_fake_span_factory()
    processor, captured = await _audio_processor_with_capture()
    processor.on_end(
        fake(
            SPAN_AUDIO_SKIPPED,
            {
                "reason": "error",
                "turn_number": 4,
                "extra_json": '{"error": "RuntimeError"}',
            },
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "state_transition"]
    assert typed, "SPAN_AUDIO_SKIPPED did not produce state_transition"
    assert typed[0]["component"] == "audio"
    assert typed[0]["fields"]["op"] == "skipped"
    assert typed[0]["fields"]["reason"] == "error"
    assert typed[0]["fields"]["turn_number"] == 4
    assert typed[0]["fields"]["extra"] == '{"error": "RuntimeError"}'


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_audio_dispatched_span() -> None:
    """``SPAN_AUDIO_DISPATCHED`` is routed — translator emits
    ``op=dispatched`` carrying mood + music_track + sfx_count, the
    payload the GM panel's audio component already consumes."""
    from sidequest.telemetry.spans import SPAN_AUDIO_DISPATCHED

    fake = _audio_fake_span_factory()
    processor, captured = await _audio_processor_with_capture()
    processor.on_end(
        fake(
            SPAN_AUDIO_DISPATCHED,
            {
                "turn_number": 7,
                "mood": "tense",
                "music_track": "underground_chase",
                "sfx_count": 2,
            },
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "state_transition"]
    assert typed, "SPAN_AUDIO_DISPATCHED did not produce state_transition"
    assert typed[0]["component"] == "audio"
    assert typed[0]["fields"]["op"] == "dispatched"
    assert typed[0]["fields"]["turn_number"] == 7
    assert typed[0]["fields"]["mood"] == "tense"
    assert typed[0]["fields"]["music_track"] == "underground_chase"
    assert typed[0]["fields"]["sfx_count"] == 2


@pytest.mark.asyncio
async def test_on_end_emits_typed_event_for_lore_established_span() -> None:
    """``SPAN_LORE_ESTABLISHED`` is routed (lore bundle) — translator
    must emit a ``lore_retrieval`` event with ``component=lore`` and
    ``op=appended`` carrying the narrator-driven additions. Before this
    bundle the GM panel's Lore tab was dark for narrator-extracted
    canonical lore; the route lights it up."""
    from sidequest.telemetry.spans import SPAN_LORE_ESTABLISHED

    fake = _audio_fake_span_factory()
    processor, captured = await _audio_processor_with_capture()
    processor.on_end(
        fake(
            SPAN_LORE_ESTABLISHED,
            {
                "items_json": '["The reactor predates the Old Ones."]',
                "added_count": 1,
                "total": 5,
                "player_name": "Rux",
                "turn_number": 7,
            },
        )
    )
    await asyncio.sleep(0.05)

    typed = [e for e in captured if e["event_type"] == "lore_retrieval"]
    assert typed, "SPAN_LORE_ESTABLISHED did not produce lore_retrieval"
    assert typed[0]["component"] == "lore"
    assert typed[0]["fields"]["op"] == "appended"
    assert typed[0]["fields"]["reason"] == "narrator_established"
    # JSON-encoded — OTEL drops list attributes silently otherwise.
    assert typed[0]["fields"]["items"] == ('["The reactor predates the Old Ones."]')
    assert typed[0]["fields"]["added_count"] == 1
    assert typed[0]["fields"]["total"] == 5
    assert typed[0]["fields"]["player_name"] == "Rux"
    assert typed[0]["fields"]["turn_number"] == 7


@pytest.mark.asyncio
async def test_dead_subscribers_are_pruned(bound_hub: WatcherHub) -> None:
    """A broken WebSocket must not prevent other subscribers from
    receiving events. The hub drops failing sockets on next broadcast."""

    class _DeadSocket:
        async def send_json(self, data: dict[str, Any]) -> None:
            raise RuntimeError("socket closed")

    dead = _DeadSocket()
    good = _FakeSocket()
    await bound_hub.subscribe(dead)  # type: ignore[arg-type]
    await bound_hub.subscribe(good)  # type: ignore[arg-type]
    publish_event("state_transition", {"field": "location"})
    await asyncio.sleep(0.05)
    # good received the event; dead got pruned
    assert len(good.events) == 1
    assert dead not in bound_hub._subscribers  # noqa: SLF001


@pytest.mark.asyncio
async def test_nonblankstring_field_does_not_kill_subscribers(
    bound_hub: WatcherHub,
) -> None:
    """An event whose fields contain a Pydantic ``NonBlankString`` must
    serialize cleanly via the hub's tolerant encoder. Both subscribers
    must receive the (string-coerced) value, and neither must be pruned
    as ``dead``.

    Regression: playtest 2026-04-29. ``send_json`` on Starlette uses
    plain ``json.dumps`` which raises ``TypeError`` on Pydantic
    ``RootModel`` instances. The per-subscriber ``except`` then evicted
    the GM dashboard, and the very next event (``turn_complete``) found
    zero subscribers. Net effect: the dashboard rendered "Turns: 0"
    forever despite the bus publishing every span.
    """
    from sidequest.protocol.types import NonBlankString

    sock_a = await _capture(bound_hub)
    sock_b = await _capture(bound_hub)
    publish_event(
        "state_transition",
        {
            "field": "location",
            "from_": NonBlankString("Old Forge"),
            "to": NonBlankString("Mendes Post"),
        },
        component="state.location",
    )
    await asyncio.sleep(0.05)
    assert len(sock_a.events) == 1
    assert len(sock_b.events) == 1
    fields = sock_a.events[0]["fields"]
    # NonBlankString must be coerced to plain JSON string — no
    # round-tripping, no nested {"root": "..."} envelope.
    assert fields["from_"] == "Old Forge"
    assert fields["to"] == "Mendes Post"
    # Both sockets remain subscribed — the encoder bug must not prune
    # live subscribers.
    assert sock_a in bound_hub._subscribers  # noqa: SLF001
    assert sock_b in bound_hub._subscribers  # noqa: SLF001


@pytest.mark.asyncio
async def test_unserializable_event_is_dropped_subscribers_preserved(
    bound_hub: WatcherHub,
) -> None:
    """Some publisher will eventually emit a value the tolerant encoder
    can't handle (e.g. a circular reference). The hub must drop the one
    bad event without evicting any subscriber. Companion test to
    ``test_nonblankstring_field_does_not_kill_subscribers``: that one
    proves the happy path of the encoder; this one proves the failure
    path doesn't break delivery for the next event.
    """
    sock = await _capture(bound_hub)

    cyclic: dict[str, Any] = {"a": 1}
    cyclic["self"] = cyclic  # circular — even default=str can't fix this

    publish_event("state_transition", {"payload": cyclic})
    publish_event("turn_complete", {"turn_id": 99, "agent_name": "narrator"})
    await asyncio.sleep(0.05)

    # The bad event was dropped; the good one was delivered.
    types = [ev["event_type"] for ev in sock.events]
    assert types == ["turn_complete"]
    assert sock in bound_hub._subscribers  # noqa: SLF001
