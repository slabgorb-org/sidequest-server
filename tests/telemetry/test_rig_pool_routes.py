"""Unit tests for the ``rig_pool.*`` SpanRoute extract lambdas (Story 53-4).

These tests inspect ``SPAN_ROUTES`` directly with synthetic span objects
— they prove that, given a ReadableSpan with the canonical attribute
set, the route's extract lambda returns the field dict the dashboard
expects. They are deliberately *not* about emission (covered by
``tests/game/test_rig_composure_pool.py``, ``test_rig_crash_handler.py``)
and *not* about end-to-end wiring (covered by
``tests/integration/test_rig_pool_wiring.py``). The split mirrors the
audio bundle's three-layer test triangle.

ADR-031 Layer-2 contract: each routed span produces a typed
``state_transition`` with ``component="rig"`` and a ``fields`` dict whose
``op`` discriminates created / delta / zero_crossing / crash_event,
matching the audio/chargen/cavern_room precedent.

The fake-span shape mirrors ``tests/server/test_watcher_events.py``'s
``_audio_fake_span_factory`` — just enough surface for the route
extract to read ``.attributes``.
"""

from __future__ import annotations

from typing import Any


class _FakeSpan:
    """Minimal stand-in for ``opentelemetry.sdk.trace.ReadableSpan``.

    The SpanRoute extract lambdas read only ``.attributes`` and possibly
    ``.name`` — they never touch timing / status. Keeping the stub
    smaller than ``MagicMock(spec=ReadableSpan)`` makes the failure
    surface narrower. ``attributes`` is typed as the protocol's union
    (``dict | None``) so pyright accepts this as a structural match for
    ``_SpanLike`` — the protocol declares the field invariant because
    it's mutable, and a narrower ``dict[str, Any]`` would not satisfy it.
    """

    name: str
    attributes: dict[str, Any] | None

    def __init__(self, name: str, attributes: dict[str, Any]):
        self.name = name
        self.attributes = attributes


def test_rig_pool_created_route_registered() -> None:
    """``rig_pool.created`` is routed; route metadata follows the
    state_transition / component=rig contract."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CREATED, SPAN_ROUTES

    assert SPAN_RIG_POOL_CREATED in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_RIG_POOL_CREATED]
    assert route.event_type == "state_transition"
    assert route.component == "rig"


def test_rig_pool_created_route_extracts_canonical_fields() -> None:
    """The extract lambda for ``rig_pool.created`` returns
    ``{field='rig_pool', op='created', character_id, chassis_id,
    current, max}`` — the dashboard's pool-initialized payload."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CREATED, SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_RIG_POOL_CREATED]
    span = _FakeSpan(
        SPAN_RIG_POOL_CREATED,
        {
            "character_id": "Mira",
            "chassis_id": "rig_tier_1_prospect",
            "current": 4,
            "max": 4,
        },
    )
    fields = route.extract(span)

    assert fields["field"] == "rig_pool"
    assert fields["op"] == "created"
    assert fields["character_id"] == "Mira"
    assert fields["chassis_id"] == "rig_tier_1_prospect"
    assert fields["current"] == 4
    assert fields["max"] == 4


def test_rig_pool_delta_route_registered() -> None:
    """``rig_pool.delta`` is routed under the same contract."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_DELTA, SPAN_ROUTES

    assert SPAN_RIG_POOL_DELTA in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_RIG_POOL_DELTA]
    assert route.event_type == "state_transition"
    assert route.component == "rig"


def test_rig_pool_delta_route_extracts_canonical_fields() -> None:
    """The extract lambda for ``rig_pool.delta`` returns
    ``{field='rig_pool', op='delta', character_id, chassis_id, delta,
    old_current, new_current}`` — the dashboard's per-damage payload."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_DELTA, SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_RIG_POOL_DELTA]
    span = _FakeSpan(
        SPAN_RIG_POOL_DELTA,
        {
            "character_id": "Mira",
            "chassis_id": "rig_tier_1_prospect",
            "delta": -2,
            "old_current": 4,
            "new_current": 2,
        },
    )
    fields = route.extract(span)

    assert fields["field"] == "rig_pool"
    assert fields["op"] == "delta"
    assert fields["character_id"] == "Mira"
    assert fields["chassis_id"] == "rig_tier_1_prospect"
    assert fields["delta"] == -2
    assert fields["old_current"] == 4
    assert fields["new_current"] == 2


def test_rig_pool_zero_crossing_route_registered() -> None:
    """``rig_pool.zero_crossing`` is routed under the same contract."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_ZERO_CROSSING, SPAN_ROUTES

    assert SPAN_RIG_POOL_ZERO_CROSSING in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_RIG_POOL_ZERO_CROSSING]
    assert route.event_type == "state_transition"
    assert route.component == "rig"


def test_rig_pool_zero_crossing_route_extracts_canonical_fields() -> None:
    """The extract lambda for ``rig_pool.zero_crossing`` returns
    ``{field='rig_pool', op='zero_crossing', character_id, chassis_id,
    old_current, new_current}``. ``new_current`` is always 0 in
    production (the span only fires on downward zero-cross) but the
    extract MUST return whatever the attr carries — not synthesize a
    constant — so a future contract change to ``new_current`` is visible
    in the test rather than silenced by a hardcoded ``0``."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_ZERO_CROSSING, SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_RIG_POOL_ZERO_CROSSING]
    span = _FakeSpan(
        SPAN_RIG_POOL_ZERO_CROSSING,
        {
            "character_id": "Mira",
            "chassis_id": "rig_tier_1_prospect",
            "old_current": 2,
            "new_current": 0,
        },
    )
    fields = route.extract(span)

    assert fields["field"] == "rig_pool"
    assert fields["op"] == "zero_crossing"
    assert fields["character_id"] == "Mira"
    assert fields["chassis_id"] == "rig_tier_1_prospect"
    assert fields["old_current"] == 2
    assert fields["new_current"] == 0


def test_rig_pool_crash_event_route_registered() -> None:
    """``rig_pool.crash_event`` is routed under the same contract."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT, SPAN_ROUTES

    assert SPAN_RIG_POOL_CRASH_EVENT in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_RIG_POOL_CRASH_EVENT]
    assert route.event_type == "state_transition"
    assert route.component == "rig"


def test_rig_pool_crash_event_route_extracts_inputs_and_consequences() -> None:
    """The extract lambda for ``rig_pool.crash_event`` returns the
    inputs (character_id, chassis_id, location, attacker) AND the three
    consequence outcomes (hp_delta, hp_after, injury_status_text,
    dismounted_status_text) — the ADR-031 Layer-2 contract: capture
    what was decided, not just the inputs.

    Pre-53-4 the span carried only the inputs; this test pins the new
    contract."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT, SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_RIG_POOL_CRASH_EVENT]
    span = _FakeSpan(
        SPAN_RIG_POOL_CRASH_EVENT,
        {
            "character_id": "Mira",
            "chassis_id": "rig_tier_1_prospect",
            "location": "dust_canyon",
            "attacker": "raider_chief",
            "hp_delta": -1,
            "hp_after": 4,
            "injury_status_text": "Hurt in the crash",
            "dismounted_status_text": "dismounted",
        },
    )
    fields = route.extract(span)

    assert fields["field"] == "rig_pool"
    assert fields["op"] == "crash_event"
    # Inputs (pre-53-4 contract — preserved)
    assert fields["character_id"] == "Mira"
    assert fields["chassis_id"] == "rig_tier_1_prospect"
    assert fields["location"] == "dust_canyon"
    assert fields["attacker"] == "raider_chief"
    # Consequences (53-4 contract — new)
    assert fields["hp_delta"] == -1
    assert fields["hp_after"] == 4
    assert fields["injury_status_text"] == "Hurt in the crash"
    assert fields["dismounted_status_text"] == "dismounted"


def test_rig_pool_crash_event_route_handles_missing_optional_attrs() -> None:
    """``handle_rig_crash`` coerces ``None`` for ``location`` / ``attacker``
    to empty string at the emit site (OTEL drops None attrs). The route
    extract MUST tolerate a span whose attrs dict has those keys set to
    ``""`` and return them as-is — empty-string is the dashboard-safe
    sentinel the audio + magic precedents already use."""
    from sidequest.telemetry.spans import SPAN_RIG_POOL_CRASH_EVENT, SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_RIG_POOL_CRASH_EVENT]
    span = _FakeSpan(
        SPAN_RIG_POOL_CRASH_EVENT,
        {
            "character_id": "Mira",
            "chassis_id": "rig_tier_1_prospect",
            "location": "",
            "attacker": "",
            "hp_delta": -1,
            "hp_after": 4,
            "injury_status_text": "Hurt in the crash",
            "dismounted_status_text": "dismounted",
        },
    )
    fields = route.extract(span)

    assert fields["location"] == ""
    assert fields["attacker"] == ""
