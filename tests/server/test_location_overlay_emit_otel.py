"""Overlay activate/deactivate fires the dedicated ``location.overlay.*``
OTEL spans (Story 54-8).

Sibling to ``test_location_overlay_emit.py`` (Story 54-7), which covers
the ``LOCATION_OVERLAY_CHANGED`` WebSocket payload. 54-8 replaces the
bare ``_watcher_publish("location_overlay_changed.emitted", ...)`` from
54-7 with proper dedicated spans — the dedicated span carries the same
fields through the SpanRoute fan-out, so the dual publish is redundant.

Wiring tests: each test calls ``_maybe_emit_location_overlay_changed``
through its real signature, proving the dedicated span fires from the
production emit site (the narration turn loop).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.protocol.models import (
    EncounterLocationOverlay,
    LocationEntity,
)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


def _enc(*, resolved: bool) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="tavern_brawl",
        player_metric=EncounterMetric(name="composure", current=10, starting=10, threshold=20),
        opponent_metric=EncounterMetric(name="brawl_energy", current=10, starting=10, threshold=20),
        resolved=resolved,
        location_overlay=EncounterLocationOverlay(
            bound_room_id="glenross_pub",
            entity_delta=[
                LocationEntity(
                    id="overturned_table",
                    label="an overturned table",
                    tier="yes_and",
                ),
            ],
            prose_suffix="A chair lies in splinters by the door.",
        ),
    )


def _only(exporter: InMemorySpanExporter, name: str):
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, (
        f"expected exactly one {name!r} span, got {len(matches)}: "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
    return matches[0]


def test_activate_fires_overlay_activate_span(
    exporter: InMemorySpanExporter,
) -> None:
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    snapshot = MagicMock()
    snapshot.encounter = _enc(resolved=False)

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="activate",
        emit_fn=emit_fn,
    )

    span = _only(exporter, "location.overlay.activate")
    attrs = span.attributes or {}
    assert attrs["region_id"] == "glenross_pub"
    assert attrs["encounter_id"] == "tavern_brawl@glenross_pub"
    assert attrs["delta_count"] == 1
    assert attrs["suffix_chars"] == len("A chair lies in splinters by the door.")


def test_deactivate_fires_overlay_deactivate_span(
    exporter: InMemorySpanExporter,
) -> None:
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "tea_and_murder"
    sd.world_slug = "glenross"
    sd.player_id = ""
    snapshot = MagicMock()
    prior_overlay = EncounterLocationOverlay(
        bound_room_id="glenross_pub",
        entity_delta=[],
        prose_suffix="A chair lies in splinters by the door.",
    )

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="deactivate",
        emit_fn=emit_fn,
        prior_overlay=prior_overlay,
    )

    span = _only(exporter, "location.overlay.deactivate")
    attrs = span.attributes or {}
    assert attrs["region_id"] == "glenross_pub"
    # On deactivate the overlay count is 0 (post-transition state) per
    # 54-7's deactivate branch shape.
    assert attrs["delta_count"] == 0
    assert attrs["suffix_chars"] == len("A chair lies in splinters by the door.")


def test_activate_no_op_when_encounter_resolved_does_not_emit_span(
    exporter: InMemorySpanExporter,
) -> None:
    """The 54-7 guard ``enc is None or enc.resolved`` short-circuits BEFORE
    the message emit. The dedicated span must short-circuit at the same
    seam — no span fired on the no-op path."""
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_overlay_changed,
    )

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.player_id = ""
    snapshot = MagicMock()
    snapshot.encounter = _enc(resolved=True)  # already resolved → no-op

    _maybe_emit_location_overlay_changed(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        transition="activate",
        emit_fn=emit_fn,
    )

    assert emit_fn.call_count == 0
    assert not any(s.name.startswith("location.overlay.") for s in exporter.get_finished_spans())
