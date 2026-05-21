"""Wiring test: ``WeatherGenerator.generate`` emits the proposed span (Story 24-7).

This is the **production-path** half of the 24-7 RED suite — the pure
helper round-trip lives in
``tests/telemetry/spans/test_world_grounding_spans.py``. Per CLAUDE.md
"every test suite needs a wiring test", we exercise the real
:meth:`sidequest.game.weather.WeatherGenerator.generate` against a
tmp-yaml climate config, then verify the
``world_grounding.weather_proposed`` span lands on the live OTEL
tracer with attributes that match the returned ``WeatherState``.

If the dev wires the span anywhere other than ``generate()`` (e.g.
only in the CLI driver), the dashboard would miss every server-side
generation call — this test forces the span into the model layer
where every consumer reaches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def minimal_weather_yaml(tmp_path: Path) -> Path:
    """A two-zone, two-season climate config sufficient to exercise
    ``generate`` deterministically.

    Duplicates the fixture from ``test_weather_generator.py`` rather
    than importing it — pytest fixtures don't cross test files without
    a conftest, and adding a conftest for one helper is heavier than
    keeping the literal inline.
    """
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  testzone:
    seasons:
      winter:
        temp_range: [-5, 5]
        precipitation_chance: 0.5
        conditions: [snow, clear_cold]
        weights: [70, 30]
      summer:
        temp_range: [15, 25]
        precipitation_chance: 0.2
        conditions: [clear, overcast]
        weights: [50, 50]
""",
        encoding="utf-8",
    )
    return yaml_path


@pytest.fixture
def event_weather_yaml(tmp_path: Path) -> Path:
    """Climate config with a chance=1.0 winter special event so the
    proposed span carries non-empty event attributes."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  testzone:
    seasons:
      winter:
        temp_range: [-5, 5]
        conditions: [clear_cold]
        weights: [1]
    special_events:
      - name: forced_blizzard
        season: winter
        chance: 1.0
        duration_days: [1, 1]
        effects: [travel_blocked, visibility_zero]
""",
        encoding="utf-8",
    )
    return yaml_path


# ---------------------------------------------------------------------------
# AC: generate() emits exactly one proposed span per call, on the
#     production OTEL tracer (the otel_capture fixture installs a
#     SimpleSpanProcessor on the live singleton — same path the GM
#     dashboard reads from).
# ---------------------------------------------------------------------------


def test_generate_emits_weather_proposed_span(
    minimal_weather_yaml: Path, otel_capture
) -> None:
    """Calling ``generate(zone, season, seed)`` fires the proposed span
    on the live tracer with attributes matching the returned state."""
    from sidequest.game.weather import WeatherGenerator

    gen = WeatherGenerator(minimal_weather_yaml)
    state = gen.generate("testzone", "winter", seed=42)

    spans = otel_capture.get_finished_spans()
    proposed = [s for s in spans if s.name == "world_grounding.weather_proposed"]
    assert len(proposed) == 1, (
        f"expected exactly 1 proposed span per generate() call, got {len(proposed)} "
        f"(all span names: {[s.name for s in spans]})"
    )
    attrs = proposed[0].attributes or {}
    assert attrs["zone"] == "testzone"
    assert attrs["season"] == "winter"
    assert attrs["condition"] == state.condition
    assert abs(float(attrs["temperature_c"]) - state.temperature_c) < 1e-9
    assert attrs["precipitation"] is state.precipitation
    # No special_events on this zone — span carries the explicit empty
    # marker, per the "no silent fallbacks" rule the helper enforces.
    assert attrs["special_event"] == ""
    assert attrs["seed"] == 42


def test_generate_emits_one_span_per_call_not_per_construction(
    minimal_weather_yaml: Path, otel_capture
) -> None:
    """Constructing ``WeatherGenerator`` must NOT emit proposed spans.

    The constructor only loads + validates YAML. Each ``generate()``
    is one proposed event. If the dev accidentally moves the emit
    into ``__init__``, the dashboard would over-count proposals."""
    from sidequest.game.weather import WeatherGenerator

    gen = WeatherGenerator(minimal_weather_yaml)
    # Constructor-only — should produce zero proposed spans.
    proposed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.weather_proposed"
    ]
    assert proposed == []

    gen.generate("testzone", "summer", seed=1)
    gen.generate("testzone", "summer", seed=2)
    gen.generate("testzone", "summer", seed=3)

    proposed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.weather_proposed"
    ]
    assert len(proposed) == 3, "one proposed span per generate() call"


def test_generate_proposed_span_records_special_event(
    event_weather_yaml: Path, otel_capture
) -> None:
    """When a special event fires, the proposed span carries the event
    name AND its effects (encoded as either a tuple or a comma-joined
    string — the helper picks one but both effect ids must be present)."""
    from sidequest.game.weather import WeatherGenerator

    gen = WeatherGenerator(event_weather_yaml)
    state = gen.generate("testzone", "winter", seed=7)
    assert state.special_event == "forced_blizzard"  # sanity: setup OK

    [proposed] = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.weather_proposed"
    ]
    attrs = proposed.attributes or {}
    assert attrs["special_event"] == "forced_blizzard"
    effects_attr = attrs["effects"]
    if isinstance(effects_attr, str):
        assert "travel_blocked" in effects_attr
        assert "visibility_zero" in effects_attr
    else:
        assert tuple(effects_attr) == ("travel_blocked", "visibility_zero")


def test_generate_emits_proposed_even_when_caller_does_not_consume(
    minimal_weather_yaml: Path, otel_capture
) -> None:
    """The proposed span fires unconditionally — narrator may never
    request the WeatherState via the tool, but the GM panel still
    needs the "what would have been proposed" signal so it can
    diff against ``weather_used`` and detect "narrator improvised
    weather without calling the grounding tool" (the lie-detector
    case CLAUDE.md OTEL doctrine demands)."""
    from sidequest.game.weather import WeatherGenerator

    gen = WeatherGenerator(minimal_weather_yaml)
    # Caller discards the return — span MUST still fire.
    gen.generate("testzone", "summer", seed=99)

    proposed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.weather_proposed"
    ]
    assert len(proposed) == 1


def test_generate_raises_does_not_emit_proposed_span(
    minimal_weather_yaml: Path, otel_capture
) -> None:
    """An unknown zone raises before the state is built — the proposed
    span must NOT fire (there is nothing to propose). Counting a
    span for a failed call would over-report grounding coverage on
    the dashboard."""
    from sidequest.game.weather import UnknownWeatherZone, WeatherGenerator

    gen = WeatherGenerator(minimal_weather_yaml)
    with pytest.raises(UnknownWeatherZone):
        gen.generate("not_a_zone", "winter", seed=42)

    proposed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.weather_proposed"
    ]
    assert proposed == [], "failed generate() must not emit a proposed span"
