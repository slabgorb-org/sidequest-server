"""Tests for ``sidequest.game.weather`` and ``sidequest.cli.weathergen`` (Story 24-5).

Covers AC1–AC7 of the procedural weather generator: WeatherState model,
ClimateRulesFile YAML validation, deterministic generation, fail-loud
behavior, special-event sampling, and CLI subprocess wiring against the
real ``tea_and_murder`` pack.

WeatherState fields are ``zone, season, condition, temperature_c,
precipitation, special_event, effects, seed`` — matching the authored
JSON Schema at ``docs/schemas/world-grounding/weather.schema.json`` and
the pack-level YAML at ``sidequest-content/genre_packs/<pack>/weather.yaml``.
See ``sprint/context/24-5-story-context.md`` for the acceptance criteria
this suite enforces.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sidequest.game.weather import (
    ClimateRulesFile,
    ClimateZone,
    SeasonPalette,
    SpecialEvent,
    UnknownWeatherSeason,
    UnknownWeatherZone,
    WeatherGenerator,
    WeatherState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_weather_yaml(content_dir: Path) -> Path:
    """Path to the real authored tea_and_murder/weather.yaml (story 24-2)."""
    return content_dir / "genre_packs" / "tea_and_murder" / "weather.yaml"


@pytest.fixture
def minimal_weather_yaml(tmp_path: Path) -> Path:
    """A small but schema-valid climate config with two conditions per season."""
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
def forced_event_yaml(tmp_path: Path) -> Path:
    """Climate config where a winter special event fires with chance=1.0."""
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
      summer:
        temp_range: [15, 25]
        conditions: [clear]
        weights: [1]
    special_events:
      - name: forced_winter_blizzard
        season: winter
        chance: 1.0
        duration_days: [1, 1]
        effects: [travel_blocked, visibility_zero]
""",
        encoding="utf-8",
    )
    return yaml_path


# ---------------------------------------------------------------------------
# AC1 — WeatherState model
# ---------------------------------------------------------------------------


def test_weather_state_round_trip_serialization() -> None:
    """WeatherState dumps to JSON and reloads identically."""
    state = WeatherState(
        zone="glen_floor",
        season="winter",
        condition="snow",
        temperature_c=-2.0,
        precipitation=True,
        special_event=None,
        effects=[],
        seed=12345,
    )
    raw = state.model_dump_json()
    decoded = json.loads(raw)
    assert decoded["zone"] == "glen_floor"
    assert decoded["season"] == "winter"
    assert decoded["condition"] == "snow"
    assert decoded["temperature_c"] == -2.0
    assert decoded["precipitation"] is True
    assert decoded["special_event"] is None
    assert decoded["effects"] == []
    assert decoded["seed"] == 12345
    # Round-trip back through the model.
    reloaded = WeatherState.model_validate_json(raw)
    assert reloaded == state


def test_weather_state_rejects_extra_fields() -> None:
    """Pydantic ``extra='forbid'`` keeps narrator-side accidental fields out."""
    with pytest.raises(ValidationError):
        WeatherState(
            zone="z",
            season="s",
            condition="c",
            temperature_c=0.0,
            precipitation=False,
            special_event=None,
            effects=[],
            seed=1,
            narrative_flourish="this field is not in the schema",  # type: ignore[call-arg]
        )


def test_weather_state_requires_seed_field() -> None:
    """The seed field is required for reproducibility/audit (no default)."""
    with pytest.raises(ValidationError):
        WeatherState(  # type: ignore[call-arg]
            zone="z",
            season="s",
            condition="c",
            temperature_c=0.0,
            precipitation=False,
            special_event=None,
            effects=[],
        )


# ---------------------------------------------------------------------------
# AC3 — ClimateRulesFile schema validation
# ---------------------------------------------------------------------------


def test_climate_rules_loads_real_tea_and_murder_yaml(real_weather_yaml: Path) -> None:
    """The real authored pack-level weather.yaml parses against the pydantic schema.

    Tests the structure, not the content. Zone/season names are content-side
    concerns and may be renamed by content authors without breaking the schema
    contract (see memory: feedback_tests_not_point_at_content).
    """
    raw = yaml.safe_load(real_weather_yaml.read_text(encoding="utf-8"))
    rules = ClimateRulesFile.model_validate(raw)
    # At least one zone; every zone has at least one season; every season is a
    # well-formed SeasonPalette; every special event is a SpecialEvent.
    assert len(rules.climate_zones) >= 1
    for zone in rules.climate_zones.values():
        assert isinstance(zone, ClimateZone)
        assert len(zone.seasons) >= 1
        for palette in zone.seasons.values():
            assert isinstance(palette, SeasonPalette)
            assert len(palette.conditions) == len(palette.weights)
        assert all(isinstance(e, SpecialEvent) for e in zone.special_events)


def test_climate_rules_rejects_empty_climate_zones() -> None:
    """Top-level ``climate_zones`` must have at least one zone (minProperties:1)."""
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate({"climate_zones": {}})


def test_climate_rules_rejects_mismatched_weights_and_conditions() -> None:
    """The schema requires len(weights) == len(conditions) — generator can't sample otherwise."""
    raw = {
        "climate_zones": {
            "z": {
                "seasons": {
                    "winter": {
                        "temp_range": [-5, 5],
                        "conditions": ["snow", "clear_cold", "fog"],
                        "weights": [1, 1],  # one short
                    }
                }
            }
        }
    }
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate(raw)


def test_climate_rules_rejects_unknown_top_level_field() -> None:
    """Pydantic ``extra='forbid'`` catches typos like ``climates`` (without _zones)."""
    raw = {"climates": {"z": {"seasons": {}}}}
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate(raw)


def test_climate_rules_rejects_empty_seasons() -> None:
    """A zone with no seasons is malformed — generator has nothing to sample."""
    raw = {"climate_zones": {"z": {"seasons": {}}}}
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate(raw)


def test_climate_rules_rejects_temp_range_with_three_values() -> None:
    """temp_range is strictly [min, max] — additional values mask authoring mistakes."""
    raw = {
        "climate_zones": {
            "z": {
                "seasons": {
                    "winter": {
                        "temp_range": [-5, 0, 5],
                        "conditions": ["snow"],
                        "weights": [1],
                    }
                }
            }
        }
    }
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate(raw)


# ---------------------------------------------------------------------------
# AC2 — WeatherGenerator core behavior
# ---------------------------------------------------------------------------


def test_generator_determinism_same_seed_same_output(minimal_weather_yaml: Path) -> None:
    """Same (zone, season, seed) tuple must produce identical WeatherState."""
    gen = WeatherGenerator(minimal_weather_yaml)
    a = gen.generate(zone="testzone", season="winter", seed=42)
    b = gen.generate(zone="testzone", season="winter", seed=42)
    assert a == b


def test_generator_different_seeds_eventually_diverge(minimal_weather_yaml: Path) -> None:
    """Different seeds across a 50-sample sweep must produce >1 distinct condition."""
    gen = WeatherGenerator(minimal_weather_yaml)
    conditions = {
        gen.generate(zone="testzone", season="winter", seed=s).condition for s in range(50)
    }
    # The winter palette has two conditions with 70/30 weights — both must appear in 50 draws.
    assert len(conditions) == 2, f"expected both conditions across 50 seeds, got {conditions}"


def test_generator_temperature_within_season_range(minimal_weather_yaml: Path) -> None:
    """Sampled temperature_c must fall within the season's temp_range."""
    gen = WeatherGenerator(minimal_weather_yaml)
    for seed in range(20):
        state = gen.generate(zone="testzone", season="winter", seed=seed)
        assert -5.0 <= state.temperature_c <= 5.0, (
            f"seed={seed}: temperature_c={state.temperature_c} outside [-5,5]"
        )


def test_generator_condition_from_palette(minimal_weather_yaml: Path) -> None:
    """Sampled condition is always one of the season's palette entries."""
    gen = WeatherGenerator(minimal_weather_yaml)
    palette = {"snow", "clear_cold"}
    for seed in range(20):
        state = gen.generate(zone="testzone", season="winter", seed=seed)
        assert state.condition in palette


def test_generator_weights_respected_in_aggregate(minimal_weather_yaml: Path) -> None:
    """Over 1000 seeded draws, the higher-weight condition dominates.

    winter palette: snow (weight 70), clear_cold (weight 30) — snow must be the
    majority sample by a clear margin. Tolerance: snow ≥ 60% of draws.
    """
    gen = WeatherGenerator(minimal_weather_yaml)
    counts: Counter[str] = Counter()
    for seed in range(1000):
        counts[gen.generate(zone="testzone", season="winter", seed=seed).condition] += 1
    assert counts["snow"] >= 600, f"expected snow≥600/1000 with weight 70, got {counts}"
    assert counts["clear_cold"] >= 200, f"expected clear_cold≥200/1000, got {counts}"


def test_generator_seed_round_trips_into_weather_state(minimal_weather_yaml: Path) -> None:
    """The seed field on WeatherState records the seed used — required for audit/replay."""
    gen = WeatherGenerator(minimal_weather_yaml)
    state = gen.generate(zone="testzone", season="summer", seed=7777)
    assert state.seed == 7777


def test_generator_records_zone_and_season(minimal_weather_yaml: Path) -> None:
    """WeatherState carries the zone and season it was generated for."""
    gen = WeatherGenerator(minimal_weather_yaml)
    state = gen.generate(zone="testzone", season="summer", seed=1)
    assert state.zone == "testzone"
    assert state.season == "summer"


# ---------------------------------------------------------------------------
# AC6 — No silent fallbacks: fail loud on missing/invalid inputs
# ---------------------------------------------------------------------------


def test_generator_raises_on_missing_yaml_path(tmp_path: Path) -> None:
    """Constructor must raise loudly when the YAML file does not exist."""
    with pytest.raises((FileNotFoundError, OSError)):
        WeatherGenerator(tmp_path / "no-such-file.yaml")


def test_generator_raises_on_unknown_zone(minimal_weather_yaml: Path) -> None:
    """Asking for an unknown zone must raise UnknownWeatherZone with a specific message."""
    gen = WeatherGenerator(minimal_weather_yaml)
    with pytest.raises(UnknownWeatherZone, match="atlantis") as exc_info:
        gen.generate(zone="atlantis", season="winter", seed=1)
    assert exc_info.value.zone == "atlantis"
    assert exc_info.value.available == ("testzone",)
    # KeyError compatibility: existing callers that catch KeyError still work.
    assert isinstance(exc_info.value, KeyError)


def test_generator_raises_on_unknown_season(minimal_weather_yaml: Path) -> None:
    """Asking for an unknown season must raise UnknownWeatherSeason."""
    gen = WeatherGenerator(minimal_weather_yaml)
    with pytest.raises(UnknownWeatherSeason, match="firefall") as exc_info:
        gen.generate(zone="testzone", season="firefall", seed=1)
    assert exc_info.value.season == "firefall"
    assert exc_info.value.zone == "testzone"
    assert exc_info.value.available == ("summer", "winter")
    assert isinstance(exc_info.value, KeyError)


# ---------------------------------------------------------------------------
# Lang-rule coverage — Python review checklist
# ---------------------------------------------------------------------------


def test_generator_uses_yaml_safe_load_not_full_load(tmp_path: Path) -> None:
    """Lang-rule #8 (unsafe deserialization): generator uses yaml.safe_load, not FullLoader.

    The ``!!python/tuple`` tag is the canonical safe-vs-full discriminator:
    yaml.safe_load raises ConstructorError on it, but yaml.FullLoader happily
    constructs a Python tuple. If the implementation regresses to FullLoader,
    the tuple would parse successfully and then fail Pydantic validation
    downstream — masking the safety regression. This test asserts the
    construction stage itself rejects the tag.
    """
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        "climate_zones:\n  z: !!python/tuple [1, 2, 3]\n",
        encoding="utf-8",
    )
    with pytest.raises(yaml.YAMLError):
        WeatherGenerator(yaml_path)


def test_generator_does_not_swallow_yaml_parse_errors(tmp_path: Path) -> None:
    """Lang-rule #1 (silent exception swallowing): malformed YAML must surface, not silently default."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text("climate_zones: [unterminated\n", encoding="utf-8")
    with pytest.raises((yaml.YAMLError, ValidationError, ValueError)):
        WeatherGenerator(yaml_path)


def test_generator_reads_yaml_with_explicit_encoding(tmp_path: Path) -> None:
    """Lang-rule #5 (path handling): UTF-8 content with non-ASCII chars must load.

    Confirms the YAML reader opens with encoding='utf-8' (CWE-838 — platform default
    drift on Windows would silently mangle these characters).
    """
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        # smirr is a Scots word; haar is the east-coast sea fog — both in the real
        # tea_and_murder content. The é tests that non-ASCII survives unmangled.
        """
climate_zones:
  café:
    seasons:
      winter:
        temp_range: [0, 5]
        conditions: [smirr, haar]
        weights: [1, 1]
""",
        encoding="utf-8",
    )
    gen = WeatherGenerator(yaml_path)
    state = gen.generate(zone="café", season="winter", seed=1)
    assert state.zone == "café"
    assert state.condition in {"smirr", "haar"}


# ---------------------------------------------------------------------------
# Special events
# ---------------------------------------------------------------------------


def test_generator_special_event_fires_when_chance_is_one(forced_event_yaml: Path) -> None:
    """A special event with chance=1.0 in the eligible season must always fire."""
    gen = WeatherGenerator(forced_event_yaml)
    for seed in range(10):
        state = gen.generate(zone="testzone", season="winter", seed=seed)
        assert state.special_event == "forced_winter_blizzard", (
            f"seed={seed}: expected forced_winter_blizzard, got {state.special_event!r}"
        )
        assert state.effects == ["travel_blocked", "visibility_zero"]


def test_generator_special_event_does_not_fire_outside_season(forced_event_yaml: Path) -> None:
    """A winter-only event must never fire in summer, regardless of seed."""
    gen = WeatherGenerator(forced_event_yaml)
    for seed in range(30):
        state = gen.generate(zone="testzone", season="summer", seed=seed)
        assert state.special_event is None, (
            f"seed={seed}: winter-only event fired in summer ({state.special_event!r})"
        )
        assert state.effects == []


def test_generator_no_event_yields_none_and_empty_effects(minimal_weather_yaml: Path) -> None:
    """When no special events are authored, special_event is None and effects is empty."""
    gen = WeatherGenerator(minimal_weather_yaml)
    state = gen.generate(zone="testzone", season="winter", seed=1)
    assert state.special_event is None
    assert state.effects == []


# ---------------------------------------------------------------------------
# AC4 + AC5 — CLI wiring against the real pack
# ---------------------------------------------------------------------------


def _run_cli(content_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m sidequest.cli.weathergen`` as a subprocess (real production path).

    Inherits the parent environment so the subprocess finds ``sidequest`` via
    the venv's editable install / PYTHONPATH / SSL certs / Linux LD_LIBRARY_PATH
    that CI runners depend on. Only ``SIDEQUEST_CONTENT_PATH`` is overridden.
    """
    return subprocess.run(
        [sys.executable, "-m", "sidequest.cli.weathergen", *args],
        env={**os.environ, "SIDEQUEST_CONTENT_PATH": str(content_dir / "genre_packs")},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_cli_wiring_real_pack_produces_valid_weather_state(content_dir: Path) -> None:
    """End-to-end wiring: CLI subprocess against real tea_and_murder/weather.yaml.

    This is the AC5 wiring test — the CLI is a real (non-test) consumer of
    WeatherGenerator. Story 24-6 owns the narrator-dispatch wiring.
    """
    result = _run_cli(
        content_dir,
        "--genre",
        "tea_and_murder",
        "--zone",
        "glen_floor",
        "--season",
        "winter",
        "--seed",
        "12345",
    )
    assert result.returncode == 0, (
        f"CLI failed (exit {result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    payload = json.loads(result.stdout)
    state = WeatherState.model_validate(payload)
    assert state.zone == "glen_floor"
    assert state.season == "winter"
    assert state.seed == 12345


def test_cli_determinism_across_subprocess_invocations(content_dir: Path) -> None:
    """Two CLI runs with the same seed must produce byte-identical JSON output."""
    args = [
        "--genre",
        "tea_and_murder",
        "--zone",
        "highland_pass",
        "--season",
        "autumn",
        "--seed",
        "99",
    ]
    a = _run_cli(content_dir, *args)
    b = _run_cli(content_dir, *args)
    assert a.returncode == 0, f"first CLI run failed: {a.stderr}"
    assert b.returncode == 0, f"second CLI run failed: {b.stderr}"
    assert json.loads(a.stdout) == json.loads(b.stdout)


def test_cli_unknown_zone_exits_nonzero_with_named_zone(content_dir: Path) -> None:
    """Lang-rule #11 (input validation at CLI boundary): unknown zone fails loud."""
    result = _run_cli(
        content_dir,
        "--genre",
        "tea_and_murder",
        "--zone",
        "atlantis",
        "--season",
        "winter",
        "--seed",
        "1",
    )
    assert result.returncode != 0, f"CLI silently accepted unknown zone (stdout: {result.stdout!r})"
    assert "atlantis" in (result.stderr + result.stdout), (
        "CLI must name the offending zone in its error output"
    )


def test_cli_unknown_genre_exits_nonzero(content_dir: Path) -> None:
    """Lang-rule #11: unknown genre pack must fail loud, never fall back.

    Uses a guaranteed-nonexistent slug rather than a workshopping pack — that
    way the test stays valid even if every workshopping pack is later promoted.
    """
    result = _run_cli(
        content_dir,
        "--genre",
        "__definitely_nonexistent_pack__",
        "--zone",
        "anywhere",
        "--season",
        "winter",
        "--seed",
        "1",
    )
    assert result.returncode != 0, (
        f"CLI silently accepted pack without weather.yaml (stdout: {result.stdout!r})"
    )


# ---------------------------------------------------------------------------
# Wiring discipline — non-test consumer exists
# ---------------------------------------------------------------------------


def test_weathergen_cli_module_is_callable_and_validates_args() -> None:
    """The CLI's ``main`` is callable in-process and rejects missing required args.

    Strengthens the bare ``hasattr`` wiring check: confirms ``main`` is not just
    present but actually executes argparse and exits non-zero on bad input,
    which means the module is fully wired (parser registered, exit semantics
    intact). Combined with ``test_cli_wiring_real_pack_produces_valid_weather_state``
    (subprocess against real content), this covers both the import and the
    behavioral wiring CLAUDE.md requires.
    """
    from sidequest.cli.weathergen.weathergen import main

    # Missing required --zone / --season / --seed must exit non-zero via
    # argparse's SystemExit. The exit code argparse uses for usage errors is 2.
    with pytest.raises(SystemExit) as exc_info:
        main(["--genre-packs-path", "/tmp/dummy", "--genre", "tea_and_murder"])
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Additional invariants (review-phase hardening)
# ---------------------------------------------------------------------------


def test_climate_rules_rejects_all_zero_weights() -> None:
    """A palette whose weights sum to zero cannot be sampled — reject at validation."""
    raw = {
        "climate_zones": {
            "z": {
                "seasons": {
                    "winter": {
                        "temp_range": [-5, 5],
                        "conditions": ["snow", "clear_cold"],
                        "weights": [0, 0],
                    }
                }
            }
        }
    }
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate(raw)


def test_climate_rules_rejects_negative_weight_element() -> None:
    """Per-element ``Field(ge=0)`` rejects a negative weight at parse time."""
    raw = {
        "climate_zones": {
            "z": {
                "seasons": {
                    "winter": {
                        "temp_range": [-5, 5],
                        "conditions": ["snow", "clear_cold"],
                        "weights": [3, -1],
                    }
                }
            }
        }
    }
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate(raw)


def test_climate_rules_rejects_inverted_temp_range() -> None:
    """``temp_range[0] > temp_range[1]`` is a content authoring error — reject loudly."""
    raw = {
        "climate_zones": {
            "z": {
                "seasons": {
                    "winter": {
                        "temp_range": [5, -5],
                        "conditions": ["snow"],
                        "weights": [1],
                    }
                }
            }
        }
    }
    with pytest.raises(ValidationError):
        ClimateRulesFile.model_validate(raw)


def test_weather_state_allows_event_with_empty_effects() -> None:
    """A fired event with no effects is valid — vibe-only events are permitted
    by the authored YAML schema, and authors can express purely narrative
    beats with ``effects: []``."""
    state = WeatherState(
        zone="z",
        season="s",
        condition="c",
        temperature_c=0.0,
        precipitation=False,
        special_event="haar",
        effects=[],
        seed=1,
    )
    assert state.special_event == "haar"
    assert state.effects == []


def test_weather_state_rejects_effects_without_event() -> None:
    """Effects with no special_event is incoherent — the effects came from
    nowhere. Reject at the model layer so deserialized inputs can't smuggle
    orphan effects past the generator."""
    with pytest.raises(ValidationError, match="effects without a special_event"):
        WeatherState(
            zone="z",
            season="s",
            condition="c",
            temperature_c=0.0,
            precipitation=False,
            special_event=None,
            effects=["travel_blocked"],
            seed=1,
        )


def test_generator_yaml_top_level_list_is_rejected(tmp_path: Path) -> None:
    """A YAML whose root is a list (not a mapping) raises a ValueError naming the type."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text("- zone1\n- zone2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="list"):
        WeatherGenerator(yaml_path)


def test_generator_precipitation_true_can_fire_with_positive_chance(
    minimal_weather_yaml: Path,
) -> None:
    """With ``precipitation_chance=0.5``, both True and False must appear across seeds."""
    gen = WeatherGenerator(minimal_weather_yaml)
    seen = {gen.generate(zone="testzone", season="winter", seed=s).precipitation for s in range(50)}
    assert seen == {True, False}, (
        f"expected both precipitation outcomes across 50 seeds, got {seen}"
    )


def test_generator_precipitation_defaults_false_when_chance_omitted(tmp_path: Path) -> None:
    """When ``precipitation_chance`` is omitted, generator never rolls precipitation=True."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  testzone:
    seasons:
      winter:
        temp_range: [-5, 5]
        conditions: [snow, clear_cold]
        weights: [1, 1]
""",
        encoding="utf-8",
    )
    gen = WeatherGenerator(yaml_path)
    for seed in range(30):
        assert gen.generate(zone="testzone", season="winter", seed=seed).precipitation is False


def test_generator_season_agnostic_event_fires_in_any_season(tmp_path: Path) -> None:
    """A ``SpecialEvent`` without a ``season`` key fires in every season at its chance."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  testzone:
    seasons:
      spring:
        temp_range: [10, 20]
        conditions: [clear]
        weights: [1]
      winter:
        temp_range: [-5, 5]
        conditions: [snow]
        weights: [1]
    special_events:
      - name: omen
        chance: 1.0
        effects: [unease]
""",
        encoding="utf-8",
    )
    gen = WeatherGenerator(yaml_path)
    spring = gen.generate(zone="testzone", season="spring", seed=1)
    winter = gen.generate(zone="testzone", season="winter", seed=1)
    assert spring.special_event == "omen"
    assert winter.special_event == "omen"
    assert spring.effects == ["unease"]
    assert winter.effects == ["unease"]


def test_generator_cross_season_seed_stability_with_mixed_event_pools(tmp_path: Path) -> None:
    """RNG-state stability invariant: condition draw is at the same RNG position
    across seasons regardless of which special events are season-eligible.

    Verifies the documented invariant (weather.py special-event roll loop):
    every event consumes one rng.random() call regardless of season filter, so
    the condition draw position is identical across seasons for a given seed.
    """
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  testzone:
    seasons:
      spring:
        temp_range: [10, 20]
        conditions: [clear, overcast]
        weights: [1, 1]
      winter:
        temp_range: [-5, 5]
        conditions: [clear, overcast]
        weights: [1, 1]
    special_events:
      - name: spring_only
        season: spring
        chance: 0.0
      - name: winter_only
        season: winter
        chance: 0.0
      - name: anywhen
        chance: 0.0
""",
        encoding="utf-8",
    )
    gen = WeatherGenerator(yaml_path)
    # All special-event chances are 0.0 so none fire; the condition draw should
    # produce the same index across seasons for a given seed. Both seasons have
    # identical palettes (same conditions, same weights) — the RNG state when
    # rng.choices() runs must be identical.
    for seed in range(20):
        spring_condition = gen.generate(zone="testzone", season="spring", seed=seed).condition
        winter_condition = gen.generate(zone="testzone", season="winter", seed=seed).condition
        assert spring_condition == winter_condition, (
            f"seed={seed}: cross-season RNG drift — spring={spring_condition!r} "
            f"winter={winter_condition!r}; special-event roll should consume RNG "
            f"identically regardless of season"
        )


def test_generator_event_with_empty_effects_yields_vibe_only_state(tmp_path: Path) -> None:
    """A YAML event that omits ``effects`` produces a valid WeatherState with
    ``special_event`` named and ``effects=[]`` — vibe-only events are valid
    per the authored schema."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  testzone:
    seasons:
      winter:
        temp_range: [-5, 5]
        conditions: [clear]
        weights: [1]
    special_events:
      - name: pure_mood_event
        season: winter
        chance: 1.0
""",
        encoding="utf-8",
    )
    gen = WeatherGenerator(yaml_path)
    state = gen.generate(zone="testzone", season="winter", seed=1)
    assert state.special_event == "pure_mood_event"
    assert state.effects == []


def test_cli_empty_content_path_env_is_rejected() -> None:
    """An empty ``SIDEQUEST_CONTENT_PATH`` would resolve to ``Path('.')`` and
    silently look for ``weather.yaml`` under CWD. The CLI must refuse."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sidequest.cli.weathergen",
            "--genre",
            "tea_and_murder",
            "--zone",
            "glen_floor",
            "--season",
            "winter",
            "--seed",
            "1",
        ],
        env={**os.environ, "SIDEQUEST_CONTENT_PATH": ""},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0, (
        f"CLI accepted empty SIDEQUEST_CONTENT_PATH (stdout: {result.stdout!r})"
    )
    assert "genre-packs-path" in result.stderr.lower() or "empty" in result.stderr.lower()
