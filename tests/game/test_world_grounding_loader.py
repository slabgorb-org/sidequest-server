"""RED — Story 24-10 AC1 + AC8: world-grounding YAML loaders.

The three loaders bridge authored pack/world YAML to the runtime grounding
fields. Per Story 24-10 AC1 each loader:

* returns a typed object when the file is present and valid,
* returns ``None`` when the file is *absent* (a legitimate authoring choice —
  not every pack grounds weather/demographics/calendar),
* raises loudly when the file is *present but malformed* (AC8 / CLAUDE.md
  "No Silent Fallbacks" — a misfiled or broken file must surface, never
  degrade to a silent ``None`` and a downstream "grounding mysteriously
  absent" symptom).

Fixtures only. Per the project rule "no content-coupled tests", these build
synthetic pack/world directories in ``tmp_path`` and never read live
``genre_packs/*``. The loaders take a directory ``Path``, so the tests point
them at fixtures they author inline — whether ``tea_and_murder/glenross``
exists, on which branch, with what weather, is irrelevant here. Content
correctness is a separate validator's job, not this suite's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sidequest.game.weather import ClimateRulesFile

# The module under test does not exist yet — this import fails in RED, which
# is correct: every test below is pending Dev's GREEN implementation.
from sidequest.game.world_grounding_loader import (
    load_pack_weather,
    load_world_calendar,
    load_world_demographics,
)

# ---------------------------------------------------------------------------
# Synthetic YAML payloads (valid shapes)
# ---------------------------------------------------------------------------

_VALID_WEATHER: dict = {
    "climate_zones": {
        "glen_floor": {
            "seasons": {
                "autumn": {
                    "temp_range": [5.0, 14.0],
                    "conditions": ["smirr", "haar", "clear"],
                    "weights": [3, 2, 1],
                    "precipitation_chance": 0.4,
                }
            }
        }
    }
}

_VALID_DEMOGRAPHICS: dict = {
    "version": "0.1.0",
    "world": "testworld",
    "parish": {"name": "Testburgh", "total_population": 412},
    "recurring_cast": [
        {"id": "minister", "name": "Rev. Test"},
        {"id": "postmistress", "name": "Margaret Test"},
    ],
}

_VALID_CALENDAR: dict = {
    "version": "0.1.0",
    "world": "testworld",
    "current": {"month": "October", "day": 14, "year": 1908},
}

_MALFORMED_YAML = "climate_zones: [unterminated\n  : : :\n"


def _write(path: Path, data: dict | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_pack_weather — pack-level weather.yaml
# ---------------------------------------------------------------------------


def test_load_pack_weather_returns_climate_rules_file_when_present(tmp_path: Path) -> None:
    """A valid pack-level weather.yaml loads to the already-validated
    ``ClimateRulesFile`` model (AC1: weather loader returns the typed model,
    not a raw dict — the generator consumes the model directly)."""
    pack_dir = tmp_path / "synth_pack"
    _write(pack_dir / "weather.yaml", _VALID_WEATHER)

    result = load_pack_weather(pack_dir)

    assert isinstance(result, ClimateRulesFile)
    # Round-trips the authored zone — proves the loader parsed and validated
    # rather than returning an empty shell.
    assert "glen_floor" in result.climate_zones
    assert "autumn" in result.climate_zones["glen_floor"].seasons


def test_load_pack_weather_returns_none_when_absent(tmp_path: Path) -> None:
    """No weather.yaml in the pack dir → ``None``. A pack that never authored
    weather is legitimate; it must not raise (AC1 / AC7 graceful path)."""
    pack_dir = tmp_path / "synth_pack"
    pack_dir.mkdir()

    assert load_pack_weather(pack_dir) is None


def test_load_pack_weather_raises_on_malformed_yaml(tmp_path: Path) -> None:
    """A present-but-syntactically-broken weather.yaml raises (AC8). The
    file existing IS the pack's declaration of weather grounding; a broken
    declaration must fail loud, never silently become ``None``."""
    pack_dir = tmp_path / "synth_pack"
    _write(pack_dir / "weather.yaml", _MALFORMED_YAML)

    with pytest.raises(Exception):  # noqa: B017 — Dev picks the typed class; RED only pins "loud"
        load_pack_weather(pack_dir)


def test_load_pack_weather_raises_on_schema_invalid_yaml(tmp_path: Path) -> None:
    """A present weather.yaml that parses but violates the ClimateRulesFile
    schema (weights length != conditions length) must raise, not return a
    half-valid model. No silent fallback to a default palette."""
    bad = {
        "climate_zones": {
            "glen_floor": {
                "seasons": {
                    "autumn": {
                        "temp_range": [5.0, 14.0],
                        "conditions": ["smirr", "haar", "clear"],
                        "weights": [3, 2],  # mismatch — schema validator rejects
                        "precipitation_chance": 0.4,
                    }
                }
            }
        }
    }
    pack_dir = tmp_path / "synth_pack"
    _write(pack_dir / "weather.yaml", bad)

    with pytest.raises(Exception):  # noqa: B017
        load_pack_weather(pack_dir)


# ---------------------------------------------------------------------------
# load_world_demographics — world-level demographics.yaml
# ---------------------------------------------------------------------------


def test_load_world_demographics_returns_dict_when_present(tmp_path: Path) -> None:
    """A valid world-level demographics.yaml loads to a dict carrying the
    authored parish/recurring_cast (AC1)."""
    world_dir = tmp_path / "synth_pack" / "worlds" / "synth_world"
    _write(world_dir / "demographics.yaml", _VALID_DEMOGRAPHICS)

    result = load_world_demographics(world_dir)

    assert isinstance(result, dict)
    assert result["parish"]["total_population"] == 412
    assert len(result["recurring_cast"]) == 2


def test_load_world_demographics_returns_none_when_absent(tmp_path: Path) -> None:
    """No demographics.yaml → ``None`` (legitimate; AC7 graceful path)."""
    world_dir = tmp_path / "synth_pack" / "worlds" / "synth_world"
    world_dir.mkdir(parents=True)

    assert load_world_demographics(world_dir) is None


def test_load_world_demographics_raises_on_malformed_yaml(tmp_path: Path) -> None:
    """A present-but-broken demographics.yaml raises (AC8)."""
    world_dir = tmp_path / "synth_pack" / "worlds" / "synth_world"
    _write(world_dir / "demographics.yaml", "parish: [unterminated\n : : :\n")

    with pytest.raises(Exception):  # noqa: B017
        load_world_demographics(world_dir)


# ---------------------------------------------------------------------------
# load_world_calendar — world-level calendar.yaml
# ---------------------------------------------------------------------------


def test_load_world_calendar_returns_dict_when_present(tmp_path: Path) -> None:
    """A valid world-level calendar.yaml loads to a dict carrying the
    authored current-date block (AC1)."""
    world_dir = tmp_path / "synth_pack" / "worlds" / "synth_world"
    _write(world_dir / "calendar.yaml", _VALID_CALENDAR)

    result = load_world_calendar(world_dir)

    assert isinstance(result, dict)
    assert result["current"]["year"] == 1908


def test_load_world_calendar_returns_none_when_absent(tmp_path: Path) -> None:
    """No calendar.yaml → ``None`` (legitimate; AC7 graceful path)."""
    world_dir = tmp_path / "synth_pack" / "worlds" / "synth_world"
    world_dir.mkdir(parents=True)

    assert load_world_calendar(world_dir) is None


def test_load_world_calendar_raises_on_malformed_yaml(tmp_path: Path) -> None:
    """A present-but-broken calendar.yaml raises (AC8)."""
    world_dir = tmp_path / "synth_pack" / "worlds" / "synth_world"
    _write(world_dir / "calendar.yaml", "current: {month: October\n : : :\n")

    with pytest.raises(Exception):  # noqa: B017
        load_world_calendar(world_dir)


# ---------------------------------------------------------------------------
# Pack/world split (AC1 technical guardrail) — no cross-level fallback
# ---------------------------------------------------------------------------


def test_demographics_is_world_level_not_pack_level(tmp_path: Path) -> None:
    """demographics.yaml is a WORLD-level file. A demographics.yaml misfiled
    at the PACK root must NOT be picked up by the world loader pointed at the
    world dir — the loader honours the pack-vs-world split (AC1 guardrail).
    Reading the misfiled file would be a silent wrong-path fallback."""
    pack_dir = tmp_path / "synth_pack"
    world_dir = pack_dir / "worlds" / "synth_world"
    world_dir.mkdir(parents=True)
    # Misfiled at pack root, NOT in the world dir.
    _write(pack_dir / "demographics.yaml", _VALID_DEMOGRAPHICS)

    # World loader pointed at the (empty) world dir must not reach up to the
    # pack-root file.
    assert load_world_demographics(world_dir) is None
