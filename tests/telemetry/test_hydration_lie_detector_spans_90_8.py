"""Story 90-8 — hydration lie-detectors become routed OTEL spans.

Design decision (pinned by these tests): Path 1 from the story context —
the two scene-harness hydration lie-detectors (``wwn.magic_hydrated`` and
``magic.state_hydrated``) are re-emitted as ROUTED OTEL SPANS instead of
raw ``watcher_hub.publish_event`` calls, mirroring the wwn.py / magic.py
helper convention (``event_type=state_transition``, ``component=magic``).

Why Path 1 and not the UI-union path (Path 2):
* The story's repo scope is server-only; adding literal event_types to the
  UI ``WatcherEventType`` union expands scope into sidequest-ui.
* magic.py's module docstring documents exactly this route as the way a
  magic event reaches the GM panel "without needing a new UI tab".
* ``tests/telemetry/test_routing_completeness.py`` enforces the routing
  decision at import time for span constants — raw publish_event strings
  get no such lint, which is how this gap shipped in the first place.

Contract pinned here (the helpers + routes; the scene_harness WIRING is
pinned in tests/game/test_scene_harness_hydrator.py and
tests/server/test_scene_harness.py):

* ``sidequest.telemetry.spans`` exports ``SPAN_WWN_MAGIC_HYDRATED``
  (== "wwn.magic_hydrated") and ``SPAN_MAGIC_STATE_HYDRATED``
  (== "magic.state_hydrated"), each registered in ``SPAN_ROUTES`` with
  ``event_type="state_transition"``, ``component="magic"``.
* Helper emitters ``wwn_magic_hydrated_span(...)`` and
  ``magic_state_hydrated_span(...)`` (keyword-only, ``_tracer`` override
  per the wwn.py convention) open exactly one span each carrying the same
  payload the 90-7 publish_event calls carried — the typed-feed payload
  recovered through ``SPAN_ROUTES[name].extract`` must be field-identical.
* ``effort_sources`` crosses the boundary as a JSON array of strings
  (list/tuple — never a bare string), including the empty case: OTEL
  attribute encoding is the helper's choice (JSON-encode per the magic.py
  structured-payload convention, or a homogeneous str sequence), but the
  extract must hand the dashboard a real array.
* The helpers do NOT call ``watcher_hub.publish_event`` themselves — the
  WatcherSpanProcessor owns the typed re-emit (ADR-132/103).

RED driver: none of the constants or helpers exist yet — this module
fails at import until Dev lands them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans import (
    SPAN_MAGIC_STATE_HYDRATED,
    SPAN_ROUTES,
    SPAN_WWN_MAGIC_HYDRATED,
    magic_state_hydrated_span,
    wwn_magic_hydrated_span,
)


def _exporter() -> tuple[InMemorySpanExporter, object]:
    """Local in-memory tracer, same harness as test_wwn_lethality.py."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test-90-8")


# ── Span constants + routing registration ───────────────────────────────────


def test_span_constants_hold_the_literal_event_names() -> None:
    """The constants must equal the exact strings 90-7 emitted, so the GM
    panel's history of RAW-console events and the new typed events share one
    name — renaming the event would orphan saved dashboards and docs."""
    assert SPAN_WWN_MAGIC_HYDRATED == "wwn.magic_hydrated"
    assert SPAN_MAGIC_STATE_HYDRATED == "magic.state_hydrated"


@pytest.mark.parametrize(
    "span_name",
    ["wwn.magic_hydrated", "magic.state_hydrated"],
)
def test_hydration_spans_route_state_transition_component_magic(span_name: str) -> None:
    """Both siblings get the SAME routing decision (the story's explicit
    requirement: apply the chosen pattern to BOTH). event_type must be
    state_transition (already in the UI union — that is the whole point of
    Path 1) and component must be magic so the Subsystems tab groups them
    with wwn.spell.cast / magic.working."""
    assert span_name in SPAN_ROUTES, (
        f"{span_name} must be registered in SPAN_ROUTES — without a route the "
        f"translator emits only agent_span_close and the typed Subsystems tab "
        f"never sees it (the exact 90-7 gap this story closes)"
    )
    route = SPAN_ROUTES[span_name]
    assert route.event_type == "state_transition", (
        f"{span_name} must route as state_transition (a known WatcherEventType); "
        f"got {route.event_type!r}"
    )
    assert route.component == "magic", (
        f"{span_name} must carry component=magic for the Subsystems tab; "
        f"got {route.component!r}"
    )


# ── wwn_magic_hydrated_span helper + extract round-trip ─────────────────────


def test_wwn_magic_hydrated_helper_emits_named_span_with_payload() -> None:
    """Helper → span → route.extract round-trip: the typed payload the
    dashboard receives must be field-identical to the 90-7 publish_event
    payload (actor, has_spellcasting, prepared, casts_per_day,
    effort_sources)."""
    exporter, tracer = _exporter()

    wwn_magic_hydrated_span(
        actor="Practitioner",
        has_spellcasting=True,
        prepared=2,
        casts_per_day=2,
        effort_sources=["channeler", "high_mage"],
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"helper must open exactly one span; got {len(spans)}"
    assert spans[0].name == "wwn.magic_hydrated"

    payload = SPAN_ROUTES["wwn.magic_hydrated"].extract(spans[0])
    assert payload["actor"] == "Practitioner"
    assert payload["has_spellcasting"] is True
    assert payload["prepared"] == 2
    assert payload["casts_per_day"] == 2
    sources = payload["effort_sources"]
    assert not isinstance(sources, str), (
        f"effort_sources must reach the dashboard as a JSON array, not a "
        f"string; got {sources!r}"
    )
    assert list(sources) == ["channeler", "high_mage"], (
        f"effort_sources must round-trip in sorted order; got {sources!r}"
    )


def test_wwn_magic_hydrated_empty_effort_sources_round_trips() -> None:
    """The spellcasting-only shape (effort_sources=[]) is the #787 path and
    must survive the OTEL attribute boundary — OTEL drops dict attributes
    and empty sequences are a known footgun, so the empty list is the case
    most likely to silently vanish."""
    exporter, tracer = _exporter()

    wwn_magic_hydrated_span(
        actor="Practitioner",
        has_spellcasting=True,
        prepared=1,
        casts_per_day=1,
        effort_sources=[],
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    payload = SPAN_ROUTES["wwn.magic_hydrated"].extract(spans[0])
    sources = payload["effort_sources"]
    assert not isinstance(sources, str)
    assert list(sources) == [], (
        f"an empty effort_sources must extract as an empty array, not vanish "
        f"or default oddly; got {sources!r}"
    )


def test_wwn_magic_hydrated_extract_defaults_on_bare_span() -> None:
    """Extract must not KeyError on a span with no attributes (the routing
    translator runs on whatever closes under this name — defensive defaults
    are the SPAN_ROUTES house style: every existing extract uses .get)."""
    payload = SPAN_ROUTES["wwn.magic_hydrated"].extract(
        SimpleNamespace(name="wwn.magic_hydrated", attributes=None)
    )
    assert not isinstance(payload.get("effort_sources"), str)
    assert list(payload.get("effort_sources") or []) == []


# ── magic_state_hydrated_span helper + extract round-trip ───────────────────


def test_magic_state_hydrated_helper_emits_named_span_with_payload() -> None:
    """Sibling #2: the magic_state fixture lie-detector carries the hydrated
    identity (fixture, world_slug, genre_slug) and the staged-state counts —
    field-identical to the 90-7 publish_event payload."""
    exporter, tracer = _exporter()

    magic_state_hydrated_span(
        fixture="magic_otel",
        world_slug="coyote_star",
        genre_slug="space_opera",
        ledger_bars=2,
        confrontations=1,
        control_tier_actors=1,
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"helper must open exactly one span; got {len(spans)}"
    assert spans[0].name == "magic.state_hydrated"

    payload = SPAN_ROUTES["magic.state_hydrated"].extract(spans[0])
    assert payload["fixture"] == "magic_otel"
    assert payload["world_slug"] == "coyote_star"
    assert payload["genre_slug"] == "space_opera"
    assert payload["ledger_bars"] == 2
    assert payload["confrontations"] == 1
    assert payload["control_tier_actors"] == 1


def test_magic_state_hydrated_extract_defaults_on_bare_span() -> None:
    """Defensive-defaults check for sibling #2 (same rationale as above)."""
    payload = SPAN_ROUTES["magic.state_hydrated"].extract(
        SimpleNamespace(name="magic.state_hydrated", attributes=None)
    )
    assert payload.get("world_slug", "") == ""
    assert payload.get("control_tier_actors", 0) == 0


# ── The helpers must not bypass the span pipeline ───────────────────────────


def test_helpers_do_not_call_publish_event_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of Path 1 is that the WatcherSpanProcessor owns the
    typed re-emit. A helper that ALSO calls watcher_hub.publish_event would
    double-emit (one raw + one typed event per hydration) and resurrect the
    un-linted raw-string event_type this story retires."""
    import sidequest.telemetry.watcher_hub as _hub

    calls: list[str] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        calls.append(event_type)

    monkeypatch.setattr(_hub, "publish_event", fake_publish)
    _exporter_unused, tracer = _exporter()

    wwn_magic_hydrated_span(
        actor="Practitioner",
        has_spellcasting=False,
        prepared=0,
        casts_per_day=0,
        effort_sources=["high_mage"],
        _tracer=tracer,
    )
    magic_state_hydrated_span(
        fixture="magic_otel",
        world_slug="coyote_star",
        genre_slug="space_opera",
        ledger_bars=0,
        confrontations=0,
        control_tier_actors=1,
        _tracer=tracer,
    )

    assert calls == [], (
        f"span helpers must not call publish_event directly — the span "
        f"processor owns the typed re-emit; got publish_event calls: {calls!r}"
    )
