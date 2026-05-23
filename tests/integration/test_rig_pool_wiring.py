"""End-to-end wiring for Story 53-4 rig_pool OTEL spans (ADR-031).

Drives the production rig flow (``RigComposurePool`` construction →
``apply_delta`` → ``handle_rig_crash``) through a real
``TracerProvider`` + ``WatcherSpanProcessor`` and asserts the typed
``state_transition`` events with ``component="rig"`` reach a subscribed
watcher socket via ``SPAN_ROUTES``.

This is the canonical wiring test for the story: the unit tests in
``tests/game/test_rig_composure_pool.py`` and ``tests/game/test_rig_crash_handler.py``
prove the spans fire, and the route-extract unit tests in
``tests/telemetry/test_rig_pool_routes.py`` prove the extractor lambdas
return the right fields. This file ties them together — it proves the
production code (not a fake span) reaches the dashboard end-to-end.

Per CLAUDE.md "Every Test Suite Needs a Wiring Test": "at least one
integration test that verifies the component is wired into the system —
imported, called, and reachable from production code paths."

Mirrors the same ``spans_module.tracer`` monkeypatch shape that
``test_audio_wiring.py`` / ``test_state_patch_wiring.py`` /
``test_disposition_otel_wiring.py`` use — OTEL refuses to replace an
already-installed global provider mid-suite, so we patch the helper
the production emit sites call.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


def _mounted_core(
    *,
    name: str = "Mira",
    composure: int = 4,
    composure_max: int = 4,
    edge_current: int = 5,
    edge_max: int = 5,
    chassis_id: str = "rig_tier_1_prospect",
):
    """Local copy of the helper in tests/game/test_rig_crash_handler.py —
    integration tests should not cross-import from sibling test suites."""
    from sidequest.game import CreatureCore, EdgePool, Inventory, RigComposurePool

    pool = RigComposurePool(
        current=composure,
        max=composure_max,
        base_max=composure_max,
        character_id=name,
        chassis_id=chassis_id,
    )
    return CreatureCore(
        name=name,
        description="A driver.",
        personality="Watchful.",
        level=1,
        xp=0,
        inventory=Inventory(),
        statuses=[],
        edge=EdgePool(
            current=edge_current,
            max=edge_max,
            base_max=edge_max,
            recovery_triggers=["OnResolution"],
            thresholds=[],
        ),
        acquired_advancements=[],
        rig_pool=pool,
    )


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Bind hub, attach capturing subscriber, install a local
    TracerProvider with WatcherSpanProcessor, and patch
    ``spans_module.tracer`` so production rig emit sites resolve to it.
    """
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    return captured


def _rig_state_transitions(captured: list[dict]) -> list[dict]:
    return [
        e
        for e in captured
        if e.get("event_type") == "state_transition"
        and e.get("component") == "rig"
    ]


@pytest.mark.asyncio
async def test_rig_pool_created_reaches_watcher_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing a ``RigComposurePool`` opens a routed
    ``rig_pool.created`` span; the WatcherSpanProcessor must translate
    that into a typed ``state_transition`` event (component=rig,
    op=created) and the subscribed sock must receive it. Proves the
    SpanRoute extract picks the canonical fields from the production
    emit site, not a fabricated span.
    """
    captured = await _setup(monkeypatch, "test-rig-created-wiring")

    _mounted_core(
        name="Mira", composure=4, composure_max=4, chassis_id="rig_tier_1_prospect"
    )
    await asyncio.sleep(0.05)

    typed = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "created"
    ]
    assert len(typed) == 1, (
        "expected exactly one created state_transition for component=rig "
        f"(got {len(typed)}: {[e['fields'] for e in typed]})"
    )
    fields = typed[0]["fields"]
    assert fields["character_id"] == "Mira"
    assert fields["chassis_id"] == "rig_tier_1_prospect"
    assert fields["current"] == 4
    assert fields["max"] == 4


@pytest.mark.asyncio
async def test_rig_pool_delta_reaches_watcher_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A damage ``apply_delta`` call must reach the hub as a typed
    ``state_transition`` (component=rig, op=delta) carrying the signed
    delta + before/after values — the per-event payload the GM panel's
    Subsystems tab consumes for live rig telemetry.
    """
    captured = await _setup(monkeypatch, "test-rig-delta-wiring")

    core = _mounted_core(name="Mira", composure=4, chassis_id="rig_tier_1_prospect")
    # Clear the created event from the captured list so we only inspect
    # the delta emission. The created span fired during _mounted_core.
    await asyncio.sleep(0.05)
    captured.clear()

    assert core.rig_pool is not None
    core.rig_pool.apply_delta(-2)
    await asyncio.sleep(0.05)

    typed = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "delta"
    ]
    assert len(typed) == 1, (
        f"expected one delta state_transition (got {len(typed)}: "
        f"{[e['fields'] for e in typed]})"
    )
    fields = typed[0]["fields"]
    assert fields["character_id"] == "Mira"
    assert fields["chassis_id"] == "rig_tier_1_prospect"
    assert fields["delta"] == -2
    assert fields["old_current"] == 4
    assert fields["new_current"] == 2


@pytest.mark.asyncio
async def test_rig_pool_zero_crossing_reaches_watcher_via_span_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``apply_delta`` that brings the pool to 0 must also publish a
    typed ``state_transition`` (component=rig, op=zero_crossing). The
    delta event still fires (proven by the prior test); the
    zero_crossing event is an additional, separate dispatch so the GM
    panel can render the crash threshold without scanning every delta.
    """
    captured = await _setup(monkeypatch, "test-rig-zero-crossing-wiring")

    core = _mounted_core(name="Mira", composure=2, chassis_id="rig_tier_1_prospect")
    await asyncio.sleep(0.05)
    captured.clear()

    assert core.rig_pool is not None
    core.rig_pool.apply_delta(-2)  # crosses to 0
    await asyncio.sleep(0.05)

    crossings = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "zero_crossing"
    ]
    assert len(crossings) == 1, (
        f"expected exactly one zero_crossing state_transition (got {len(crossings)})"
    )
    fields = crossings[0]["fields"]
    assert fields["old_current"] == 2
    assert fields["new_current"] == 0
    # Delta event also fires, but is asserted in its own test — here we
    # only pin that zero_crossing is an independent channel.


@pytest.mark.asyncio
async def test_rig_pool_zero_crossing_does_not_re_fire_when_already_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second delta on a pool already at 0 publishes ``delta`` (the
    operator should see the attempted damage) but MUST NOT republish
    ``zero_crossing`` — the crossing event is edge-triggered, not
    level-triggered. Otherwise every snapshot/save round-trip on a
    wrecked rig would spam the dashboard.
    """
    captured = await _setup(monkeypatch, "test-rig-zero-crossing-idempotent")

    core = _mounted_core(name="Mira", composure=0, chassis_id="rig_tier_1_prospect")
    await asyncio.sleep(0.05)
    captured.clear()

    assert core.rig_pool is not None
    core.rig_pool.apply_delta(-3)  # already at 0
    await asyncio.sleep(0.05)

    # Premise check (anti-vacuous): the wiring fires at all. Without this,
    # the test passes trivially when nothing routes — which is the very
    # state pre-53-4 production sits in. Assert the delta event DID
    # publish so the "no re-fire of zero_crossing" claim is meaningful.
    deltas = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "delta"
    ]
    assert len(deltas) == 1, (
        "wiring premise: the delta event must publish on every apply_delta "
        f"call (got {len(deltas)}: {[e['fields'] for e in deltas]}) — if "
        "this is zero, the test of zero_crossing absence below is vacuous"
    )
    # AC1 sub-clause: the delta payload on a wrecked rig must report
    # new_current=0 (clamped) and old_current=0 (already wrecked) — not
    # the unclamped raw value. A regression that fails to clamp would
    # otherwise pass the bare "one delta arrived" premise check.
    delta_fields = deltas[0]["fields"]
    assert delta_fields["delta"] == -3
    assert delta_fields["old_current"] == 0
    assert delta_fields["new_current"] == 0

    crossings = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "zero_crossing"
    ]
    assert crossings == [], (
        f"zero_crossing must not re-fire on a wrecked rig (got {crossings})"
    )


@pytest.mark.asyncio
async def test_rig_pool_zero_crossing_re_fires_after_repair_and_re_damage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 sub-clause: a pool that crosses to 0, heals back to a positive
    value, then is damaged to 0 again MUST publish a second
    ``zero_crossing`` event. The crossing is edge-triggered on the
    downward transition; a regression that tracks "has ever crossed" as
    a one-way flag would silence the second crash and break the GM
    dashboard's repeat-encounter signal.

    The hazard this test guards against: a buggy refactor that promotes
    ``zero_crossed`` to a persistent ``has_crossed_zero`` field on the
    pool and emits only on first transition — every subsequent re-cross
    after repair would be invisible.
    """
    captured = await _setup(monkeypatch, "test-rig-re-cross-after-heal")

    core = _mounted_core(name="Mira", composure=1, chassis_id="rig_tier_1_prospect")
    assert core.rig_pool is not None
    core.rig_pool.apply_delta(-1)  # first crossing (1 → 0)
    core.rig_pool.apply_delta(+1)  # repair (0 → 1) — must NOT publish a crossing
    await asyncio.sleep(0.05)
    captured.clear()

    core.rig_pool.apply_delta(-1)  # second crossing (1 → 0)
    await asyncio.sleep(0.05)

    crossings = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "zero_crossing"
    ]
    assert len(crossings) == 1, (
        "second downward zero-crossing after repair must publish exactly "
        f"one zero_crossing event in the post-clear window (got {len(crossings)})"
    )
    fields = crossings[0]["fields"]
    assert fields["old_current"] == 1
    assert fields["new_current"] == 0


@pytest.mark.asyncio
async def test_rig_pool_zero_crossing_independent_of_crash_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 negative case: ``rig_pool.zero_crossing`` and
    ``rig_pool.crash_event`` are independent gates. Driving
    ``apply_delta`` to zero WITHOUT invoking ``handle_rig_crash`` must
    publish the zero_crossing event but NOT the crash_event — the
    crash consequences only fire when the handler is called.

    A regression that accidentally couples the two (e.g., apply_delta
    starts auto-firing handle_rig_crash on the destroyed pool) would
    inflate the GM dashboard's crash count with phantom crashes
    triggered by ordinary damage resolution.
    """
    captured = await _setup(monkeypatch, "test-rig-crossing-independent-of-crash")

    core = _mounted_core(name="Mira", composure=2, chassis_id="rig_tier_1_prospect")
    assert core.rig_pool is not None
    core.rig_pool.apply_delta(-2)  # crosses to 0; NO handle_rig_crash called
    await asyncio.sleep(0.05)

    crossings = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "zero_crossing"
    ]
    crashes = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "crash_event"
    ]
    assert len(crossings) == 1, "zero_crossing must publish on the downward crossing"
    assert crashes == [], (
        "crash_event must NOT publish unless handle_rig_crash is explicitly "
        f"invoked (got {crashes})"
    )


@pytest.mark.asyncio
async def test_rig_pool_crash_event_reaches_watcher_with_consequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``handle_rig_crash`` must reach the hub as a typed
    ``state_transition`` (component=rig, op=crash_event) carrying the
    three consequence outcomes:

    - ``edge_delta`` — the realized Edge change
    - ``edge_after`` — the post-crash Edge value
    - ``injury_status_text`` — the appended injury text
    - ``dismounted_status_text`` — the appended dismount text

    This is the ADR-031 Layer-2 contract: capture what was DECIDED, not
    just the inputs. Pre-53-4 the crash span only carried the inputs
    (character_id, chassis_id, location, attacker); the dashboard now
    sees the outcome too.
    """
    from sidequest.game import handle_rig_crash
    from sidequest.game.rig_crash import (
        DISMOUNTED_STATUS_TEXT,
        DRIVER_EDGE_HIT,
        INJURY_STATUS_TEXT,
    )

    captured = await _setup(monkeypatch, "test-rig-crash-wiring")

    core = _mounted_core(
        name="Mira",
        composure=0,
        edge_current=5,
        edge_max=5,
        chassis_id="rig_tier_1_prospect",
    )
    await asyncio.sleep(0.05)
    captured.clear()

    handle_rig_crash(core, location="dust_canyon", attacker="raider_chief")
    await asyncio.sleep(0.05)

    crashes = [
        e
        for e in _rig_state_transitions(captured)
        if e["fields"].get("op") == "crash_event"
    ]
    assert len(crashes) == 1, (
        f"expected exactly one crash_event state_transition (got {len(crashes)}: "
        f"{[e['fields'] for e in crashes]})"
    )
    fields = crashes[0]["fields"]
    assert fields["character_id"] == "Mira"
    assert fields["chassis_id"] == "rig_tier_1_prospect"
    assert fields["location"] == "dust_canyon"
    assert fields["attacker"] == "raider_chief"
    assert fields["edge_delta"] == DRIVER_EDGE_HIT
    assert fields["edge_after"] == 5 + DRIVER_EDGE_HIT
    assert fields["injury_status_text"] == INJURY_STATUS_TEXT
    assert fields["dismounted_status_text"] == DISMOUNTED_STATUS_TEXT


@pytest.mark.asyncio
async def test_rig_crash_full_sequence_publishes_ordered_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a fresh rig damaged to zero and then crashed must
    produce four typed events in publish order on the hub:

      1. ``state_transition`` component=rig op=created       (pool construction)
      2. ``state_transition`` component=rig op=delta         (damage to 0)
      3. ``state_transition`` component=rig op=zero_crossing (downward edge trigger)
      4. ``state_transition`` component=rig op=crash_event   (consequence layer)

    This is the canonical GM-panel sequence for a single fatal hit. If
    the order changes or any event is skipped, the dashboard's
    rig-timeline narrative breaks.

    No ``agent_span_close`` deduplication is asserted here — that
    behavior is shared across all routed spans and is owned by the
    translator tests.
    """
    from sidequest.game import handle_rig_crash

    captured = await _setup(monkeypatch, "test-rig-full-sequence")

    core = _mounted_core(name="Mira", composure=3, chassis_id="rig_tier_1_prospect")
    assert core.rig_pool is not None
    core.rig_pool.apply_delta(-3)  # zero crossing
    handle_rig_crash(core, location="dust_canyon", attacker="raider_chief")
    await asyncio.sleep(0.05)

    ops = [
        e["fields"].get("op")
        for e in _rig_state_transitions(captured)
        # Only inspect events whose fields carry an op marker. Other
        # rig.* events without an op (e.g., synthetic) are out of scope.
        if e["fields"].get("op") is not None
    ]
    expected = ["created", "delta", "zero_crossing", "crash_event"]
    assert ops == expected, (
        f"rig event sequence must be {expected}, got {ops}"
    )
