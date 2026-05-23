"""OTEL spans for the world-grounding subsystem (Story 24-7).

Three state-transition spans under ``component="world_grounding"`` close
the lie-detector loop the GM panel needs to grade story 24-8's playtest
validation:

* ``world_grounding.weather_proposed`` — emitted inside
  :meth:`sidequest.game.weather.WeatherGenerator.generate` after the
  ``WeatherState`` is built. Carries every field of the sampled state
  (zone, season, condition, temperature_c, precipitation, special_event,
  seed) so the dashboard can show *what the generator produced*
  regardless of whether the narrator ever asks for it.

* ``world_grounding.weather_used`` — emitted inside the
  ``get_world_grounding`` tool handler when the narrator's ``include``
  list contains ``"weather"`` **and** the session-bound
  ``WeatherState`` is not ``None``. Pairs with ``weather_proposed`` so
  the GM panel can render the "proposed vs used" diff that proves
  whether the narrator's prose is grounded in the generated state or
  improvised over it (per CLAUDE.md OTEL doctrine).

* ``world_grounding.demographics_injected`` — emitted inside the
  ``get_world_grounding`` tool handler when the narrator's ``include``
  list contains ``"demographics"`` **and** the session-bound
  demographics dict is not ``None``. One-sided signal — demographics
  are authored YAML loaded at session bootstrap, not procedurally
  generated, so there is no "proposed" counterpart to diff against.

All three route as ``event_type="state_transition"`` so the GM panel's
typed-event tabs surface them alongside ``namegen.*`` and ``location.*``.
The new constants must NOT live in ``FLAT_ONLY_SPANS`` — that would
hide them from the dashboard, defeating the entire point of the story.

This file is the routing-and-shape RED gate. The two follow-up RED
files (``tests/game/test_weather_proposed_span.py`` and
``tests/agents/tools/test_grounding_otel_used_injection.py``) prove
the spans fire from real production code paths, satisfying the
CLAUDE.md "every test suite needs a wiring test" rule.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a per-test in-memory exporter on the shared spans module.

    Mirrors ``tests/telemetry/spans/test_location_spans.py``: we
    monkeypatch ``sidequest.telemetry.spans.tracer`` because
    :meth:`sidequest.telemetry.spans.span.Span.open` reads the tracer
    callable from that module at span-open time. Replacing the
    *function* (not just the global TracerProvider) is the only way
    to keep span emission scoped to this test.
    """
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


# ---------------------------------------------------------------------------
# Constants + route registration
# ---------------------------------------------------------------------------


def test_constants_have_canonical_names() -> None:
    """The three SPAN_* constants must expose canonical dotted names.

    The watcher-side translator (``sidequest.telemetry.watcher``) and the
    GM dashboard (``sidequest-ui``) both key off these literal strings.
    Renaming after merge breaks the dashboard silently — pin the
    names in a test so any rename forces a synchronized dashboard
    update."""
    from sidequest.telemetry.spans import (
        SPAN_WORLD_GROUNDING_DEMOGRAPHICS_INJECTED,
        SPAN_WORLD_GROUNDING_WEATHER_PROPOSED,
        SPAN_WORLD_GROUNDING_WEATHER_USED,
    )

    assert SPAN_WORLD_GROUNDING_WEATHER_PROPOSED == "world_grounding.weather_proposed"
    assert SPAN_WORLD_GROUNDING_WEATHER_USED == "world_grounding.weather_used"
    assert SPAN_WORLD_GROUNDING_DEMOGRAPHICS_INJECTED == "world_grounding.demographics_injected"


def test_every_world_grounding_span_is_routed_as_state_transition() -> None:
    """All three constants are routed under ``component="world_grounding"``.

    Membership in ``FLAT_ONLY_SPANS`` would mean the GM panel never
    receives a typed watcher event for the span — that is the wrong
    choice for a lie-detector surface, so the route MUST exist and
    the flat-only set MUST be clear. Per the routing-completeness
    invariant (``tests/telemetry/test_routing_completeness.py``), a
    new SPAN_* constant has to be in one of the two sets; this test
    locks it to the routed side.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_ROUTES,
        SPAN_WORLD_GROUNDING_DEMOGRAPHICS_INJECTED,
        SPAN_WORLD_GROUNDING_WEATHER_PROPOSED,
        SPAN_WORLD_GROUNDING_WEATHER_USED,
    )

    for span_name in (
        SPAN_WORLD_GROUNDING_WEATHER_PROPOSED,
        SPAN_WORLD_GROUNDING_WEATHER_USED,
        SPAN_WORLD_GROUNDING_DEMOGRAPHICS_INJECTED,
    ):
        assert span_name in SPAN_ROUTES, f"{span_name} missing from SPAN_ROUTES"
        assert span_name not in FLAT_ONLY_SPANS, (
            f"{span_name} must not be flat-only — the GM dashboard needs the typed watcher event"
        )
        route = SPAN_ROUTES[span_name]
        assert route.component == "world_grounding"
        assert route.event_type == "state_transition"


# ---------------------------------------------------------------------------
# weather_proposed helper
# ---------------------------------------------------------------------------


def test_weather_proposed_helper_records_all_state_fields(
    exporter: InMemorySpanExporter,
) -> None:
    """The proposed-side span must round-trip every WeatherState field.

    The dashboard's "proposed vs used" view diffs these attributes
    against the ``weather_used`` span's attributes — every missing
    field is a column the dashboard cannot render, so the helper has
    to surface the full state.
    """
    from sidequest.game.weather import WeatherState
    from sidequest.telemetry.spans import emit_weather_proposed_span

    state = WeatherState(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        temperature_c=11.5,
        precipitation=True,
        special_event=None,
        effects=[],
        seed=42,
    )
    emit_weather_proposed_span(state)

    [span] = exporter.get_finished_spans()
    assert span.name == "world_grounding.weather_proposed"
    attrs = span.attributes or {}
    assert attrs["zone"] == "glen_floor"
    assert attrs["season"] == "autumn"
    assert attrs["condition"] == "smirr"
    # OTEL attribute types are restricted; floats survive verbatim.
    assert abs(float(attrs["temperature_c"]) - 11.5) < 1e-9
    assert attrs["precipitation"] is True
    # Per CLAUDE.md "no silent fallbacks": absent special_event MUST be
    # explicitly encoded as the empty string, not omitted. The dashboard
    # tests for `special_event == ""` to render "no event"; an absent
    # key would silently look identical to a misconfigured exporter.
    assert attrs["special_event"] == ""
    assert attrs["seed"] == 42


def test_weather_proposed_helper_records_special_event_when_fired(
    exporter: InMemorySpanExporter,
) -> None:
    """When a special event fires, both its name and effect ids surface.

    ``effects`` is a list, so we serialise it as a JSON string for the
    OTEL attribute (OTEL allows list[str] but the watcher translator
    in this repo prefers stable scalar attrs). The helper is
    responsible for the encoding; the test asserts the round-trip.
    """
    from sidequest.game.weather import WeatherState
    from sidequest.telemetry.spans import emit_weather_proposed_span

    state = WeatherState(
        zone="glen_floor",
        season="winter",
        condition="blizzard",
        temperature_c=-4.0,
        precipitation=True,
        special_event="winter_storm",
        effects=["impair_visibility", "ground_travel_halved"],
        seed=99,
    )
    emit_weather_proposed_span(state)

    [span] = exporter.get_finished_spans()
    attrs = span.attributes or {}
    assert attrs["special_event"] == "winter_storm"
    # The watcher route expects ``effects`` as a single attr the
    # dashboard can render in one column — implementation may choose
    # tuple/list (OTEL native) or comma-joined string, but both
    # effect names MUST be recoverable.
    effects_attr = attrs["effects"]
    if isinstance(effects_attr, str):
        assert "impair_visibility" in effects_attr
        assert "ground_travel_halved" in effects_attr
    else:
        assert tuple(effects_attr) == ("impair_visibility", "ground_travel_halved")


# ---------------------------------------------------------------------------
# weather_used helper
# ---------------------------------------------------------------------------


def test_weather_used_helper_records_join_keys(
    exporter: InMemorySpanExporter,
) -> None:
    """The used-side span must carry enough state to join against proposed.

    Dashboard pivots on ``seed`` (unique per (zone, season, generator
    seed) call) to match a used row to its proposed row. ``zone``,
    ``season``, and ``condition`` are repeated so the dashboard does
    not need to perform a JOIN to render a single-row used-only view.
    """
    from sidequest.telemetry.spans import emit_weather_used_span

    emit_weather_used_span(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        seed=42,
        world_id="glenross",
        perspective_pc="Alex",
    )

    [span] = exporter.get_finished_spans()
    assert span.name == "world_grounding.weather_used"
    attrs = span.attributes or {}
    assert attrs["zone"] == "glen_floor"
    assert attrs["season"] == "autumn"
    assert attrs["condition"] == "smirr"
    assert attrs["seed"] == 42
    assert attrs["world_id"] == "glenross"
    assert attrs["perspective_pc"] == "Alex"


def test_weather_used_helper_encodes_absent_perspective_explicitly(
    exporter: InMemorySpanExporter,
) -> None:
    """Single-player sessions have ``perspective_pc=None``.

    Per CLAUDE.md, the helper MUST encode that explicitly (empty
    string) rather than omitting the attribute, so the dashboard's
    column-presence check is reliable. ``world_id`` is always
    present — the session has a world by definition.
    """
    from sidequest.telemetry.spans import emit_weather_used_span

    emit_weather_used_span(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        seed=42,
        world_id="glenross",
        perspective_pc=None,
    )

    [span] = exporter.get_finished_spans()
    attrs = span.attributes or {}
    assert attrs["perspective_pc"] == ""


# ---------------------------------------------------------------------------
# demographics_injected helper
# ---------------------------------------------------------------------------


def test_demographics_injected_helper_records_world_and_cast(
    exporter: InMemorySpanExporter,
) -> None:
    """The demographics-injection span carries the world id plus a
    coarse fingerprint of what landed in the narrator's context.

    Three attributes the GM panel renders:
      * ``world_id`` — which world's demographics
      * ``total_population`` — integer summary (parish total if
        present, else 0). 0 is acceptable for worlds that don't
        author a population number; the GM panel renders "n/a".
      * ``recurring_cast_count`` — len of ``recurring_cast`` list,
        or 0 if absent. Sebastien's mechanical-first lens cares
        about this number.
    """
    from sidequest.telemetry.spans import emit_demographics_injected_span

    demographics = {
        "version": "0.1.0",
        "world": "glenross",
        "parish": {"name": "Glenross", "total_population": 412},
        "recurring_cast": [
            {"id": "minister", "name": "Rev. Aulay"},
            {"id": "postmistress", "name": "Margaret"},
            {"id": "publican", "name": "Iain"},
        ],
    }
    emit_demographics_injected_span(
        world_id="glenross",
        demographics=demographics,
        perspective_pc="Alex",
    )

    [span] = exporter.get_finished_spans()
    assert span.name == "world_grounding.demographics_injected"
    attrs = span.attributes or {}
    assert attrs["world_id"] == "glenross"
    assert attrs["total_population"] == 412
    assert attrs["recurring_cast_count"] == 3
    assert attrs["perspective_pc"] == "Alex"


def test_demographics_injected_helper_tolerates_minimal_dict(
    exporter: InMemorySpanExporter,
) -> None:
    """A demographics dict missing optional sections still produces a
    well-formed span — the helper substitutes 0 (not None, not
    absent) so the dashboard always sees integer columns.

    Per CLAUDE.md "no silent fallbacks": this is NOT a fallback for a
    missing required field, it is the contract for an authored YAML
    that legitimately has no parish/cast sections (e.g. a pack still
    in workshopping). The dashboard distinguishes "0 cast" from
    "demographics not wired" via the **presence** of this span — if
    the span fires at all, demographics were wired; the counts are
    informational.
    """
    from sidequest.telemetry.spans import emit_demographics_injected_span

    emit_demographics_injected_span(
        world_id="empty_world",
        demographics={"version": "0.1.0", "world": "empty_world"},
        perspective_pc=None,
    )

    [span] = exporter.get_finished_spans()
    attrs = span.attributes or {}
    assert attrs["world_id"] == "empty_world"
    assert attrs["total_population"] == 0
    assert attrs["recurring_cast_count"] == 0
    assert attrs["perspective_pc"] == ""


# ---------------------------------------------------------------------------
# Route extractors — fields the watcher translator surfaces to the UI
# ---------------------------------------------------------------------------


def test_weather_proposed_route_extract_exposes_state_columns(
    exporter: InMemorySpanExporter,
) -> None:
    """The route extractor must surface the same fields the helper sets.

    The watcher translator runs ``extract(span)`` to build a dict the
    UI consumes. Missing fields here = blank columns in the dashboard.
    """
    from sidequest.game.weather import WeatherState
    from sidequest.telemetry.spans import SPAN_ROUTES, emit_weather_proposed_span

    state = WeatherState(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        temperature_c=11.5,
        precipitation=True,
        special_event=None,
        effects=[],
        seed=42,
    )
    emit_weather_proposed_span(state)

    [span] = exporter.get_finished_spans()
    fields = SPAN_ROUTES["world_grounding.weather_proposed"].extract(span)
    assert fields["field"] == "weather"
    assert fields["op"] == "proposed"
    assert fields["zone"] == "glen_floor"
    assert fields["season"] == "autumn"
    assert fields["condition"] == "smirr"
    assert fields["seed"] == 42


def test_weather_used_route_extract_exposes_join_columns(
    exporter: InMemorySpanExporter,
) -> None:
    """``weather_used`` extracts must carry the seed + zone + season +
    condition so the dashboard can join used→proposed without re-querying."""
    from sidequest.telemetry.spans import SPAN_ROUTES, emit_weather_used_span

    emit_weather_used_span(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        seed=42,
        world_id="glenross",
        perspective_pc="Alex",
    )

    [span] = exporter.get_finished_spans()
    fields = SPAN_ROUTES["world_grounding.weather_used"].extract(span)
    assert fields["field"] == "weather"
    assert fields["op"] == "used"
    assert fields["zone"] == "glen_floor"
    assert fields["season"] == "autumn"
    assert fields["condition"] == "smirr"
    assert fields["seed"] == 42
    assert fields["world_id"] == "glenross"


def test_demographics_injected_route_extract_exposes_cast_column(
    exporter: InMemorySpanExporter,
) -> None:
    """The demographics extractor surfaces ``recurring_cast_count`` and
    ``total_population`` so the dashboard renders a one-row summary."""
    from sidequest.telemetry.spans import (
        SPAN_ROUTES,
        emit_demographics_injected_span,
    )

    emit_demographics_injected_span(
        world_id="glenross",
        demographics={
            "parish": {"total_population": 412},
            "recurring_cast": [{"id": "minister"}, {"id": "postmistress"}],
        },
        perspective_pc="Alex",
    )

    [span] = exporter.get_finished_spans()
    fields = SPAN_ROUTES["world_grounding.demographics_injected"].extract(span)
    assert fields["field"] == "demographics"
    assert fields["op"] == "injected"
    assert fields["world_id"] == "glenross"
    assert fields["total_population"] == 412
    assert fields["recurring_cast_count"] == 2
