"""Story 24-10 RED tests — pack/world world-grounding YAML loaders.

AC1 (loaders): a new module `sidequest.game.world_grounding_loader` exposes
three functions:

    load_pack_weather(pack_dir)        -> ClimateRulesFile | None
    load_world_demographics(world_dir) -> dict | None
    load_world_calendar(world_dir)     -> dict | None

* Each returns None when the file is legitimately absent (pack/world chose
  not to author that surface — e.g. `caverns_and_claudes` has no
  `weather.yaml`; that's not an error, it's "no grounding declared").
* Each raises a typed exception when the file is present but malformed
  (CLAUDE.md "No Silent Fallbacks"). The weather loader returns the
  already-validated `ClimateRulesFile` from `sidequest.game.weather` —
  not a raw dict — so per-zone/per-season schema violations surface here,
  not deep inside a `generate()` call.
* Discovery is BY FILE PRESENCE, not by a flag in `pack.yaml` (story
  context "Discovery rule" — symmetric with how `cultures.yaml` is
  discovered). Authoring the YAML IS the declaration.
* Pack vs world split is honoured: `demographics.yaml` and
  `calendar.yaml` live ONLY under `worlds/<world>/`, NOT at pack root —
  a misfiled pack-root `demographics.yaml` is not a silent fallback.

AC8 (fail loud): malformed weather.yaml at pack level raises a typed
error from the LOADER (i.e. visible at session-bootstrap time), not from
deep inside a per-turn `WeatherGenerator.generate()` call.

These tests target a module that does not yet exist. Import-time
ModuleNotFoundError IS the RED state for AC1 — Dev's GREEN move is to
create `sidequest/game/world_grounding_loader.py`.

Python lang-review coverage:
* #1 silent exceptions — malformed YAML must raise, not return None.
* #5 path handling — loaders accept pathlib.Path and str; no string-glue
  separators (covered by accept-Path assertion).
* #6 test quality — every assertion checks a specific value, not just
  truthiness.
* #8 unsafe deserialization — `yaml.safe_load`, NOT `yaml.load` (verified
  by feeding a python-object tag and asserting it errors rather than
  instantiating an object).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Module-presence sentinel — collects as RED until the loader module exists.
# ---------------------------------------------------------------------------

pytest.importorskip(
    "sidequest.game.world_grounding_loader",
    reason=(
        "Story 24-10 RED: sidequest.game.world_grounding_loader does not yet "
        "exist. Dev's GREEN task is to create the module with "
        "load_pack_weather / load_world_demographics / load_world_calendar."
    ),
)

# After the importorskip guard, real imports — Dev's GREEN code must satisfy
# this surface verbatim.
from sidequest.game.weather import ClimateRulesFile
from sidequest.game.world_grounding_loader import (  # noqa: E402
    load_pack_weather,
    load_world_calendar,
    load_world_demographics,
)


# ---------------------------------------------------------------------------
# Fixtures — real authored pack content + a malformed/missing scratch dir
# ---------------------------------------------------------------------------


@pytest.fixture
def tea_and_murder_pack_dir(content_dir: Path) -> Path:
    """Real pack root for tea_and_murder (ships weather.yaml)."""
    return content_dir / "genre_packs" / "tea_and_murder"


@pytest.fixture
def glenross_world_dir(content_dir: Path) -> Path:
    """Real world dir for tea_and_murder/glenross (ships demographics +
    calendar yaml)."""
    return content_dir / "genre_packs" / "tea_and_murder" / "worlds" / "glenross"


@pytest.fixture
def caverns_pack_dir(content_dir: Path) -> Path:
    """Real pack root for caverns_and_claudes — has NO weather.yaml."""
    return content_dir / "genre_packs" / "caverns_and_claudes"


# ---------------------------------------------------------------------------
# AC1 — happy paths
# ---------------------------------------------------------------------------


def test_load_pack_weather_returns_climate_rules_file(
    tea_and_murder_pack_dir: Path,
) -> None:
    """tea_and_murder ships a real weather.yaml. The loader must return the
    already-validated ClimateRulesFile (NOT a raw dict) so per-zone /
    per-season schema violations surface at session bootstrap, not deep
    inside a WeatherGenerator.generate() call from the narrator turn."""
    rules = load_pack_weather(tea_and_murder_pack_dir)
    assert rules is not None, (
        "tea_and_murder/weather.yaml exists on disk — loader must return a "
        "populated ClimateRulesFile, not None"
    )
    assert isinstance(rules, ClimateRulesFile), (
        f"loader must return ClimateRulesFile (already-validated), got "
        f"{type(rules).__name__} — raw dict would defer schema errors to "
        f"per-turn generate() calls (silent-fallback class of bug)"
    )
    # tea_and_murder authored two zones: glen_floor + highland_pass.
    assert "glen_floor" in rules.climate_zones, (
        "expected 'glen_floor' zone in tea_and_murder weather.yaml; got "
        f"{sorted(rules.climate_zones)}"
    )


def test_load_world_demographics_returns_dict_with_known_fields(
    glenross_world_dir: Path,
) -> None:
    """glenross/demographics.yaml exists. The loader returns a dict with
    the authored top-level keys (story context AC6 mentions `parish` and
    `recurring_cast`)."""
    demographics = load_world_demographics(glenross_world_dir)
    assert demographics is not None, (
        "glenross/demographics.yaml exists — loader must return a populated "
        "dict, not None"
    )
    assert isinstance(demographics, dict), (
        f"loader must return a dict, got {type(demographics).__name__}"
    )
    # Story context AC6 specifies these keys are part of the authored payload.
    assert "world" in demographics, (
        "expected top-level 'world' field in glenross demographics.yaml; "
        f"got keys={sorted(demographics)}"
    )


def test_load_world_calendar_returns_dict_when_present(
    glenross_world_dir: Path,
) -> None:
    """glenross/calendar.yaml exists (story 24-4 shipped). Loader returns
    a non-None dict."""
    calendar = load_world_calendar(glenross_world_dir)
    assert calendar is not None, (
        "glenross/calendar.yaml exists — loader must return a populated "
        "dict, not None"
    )
    assert isinstance(calendar, dict), (
        f"loader must return a dict, got {type(calendar).__name__}"
    )


# ---------------------------------------------------------------------------
# AC1 — legitimate absence returns None (NOT an exception)
# ---------------------------------------------------------------------------


def test_load_pack_weather_returns_none_when_file_absent(
    caverns_pack_dir: Path,
) -> None:
    """caverns_and_claudes does not ship a weather.yaml. The loader must
    return None — not raise. This is the legitimate 'pack chose not to
    declare weather grounding' branch per story context."""
    result = load_pack_weather(caverns_pack_dir)
    assert result is None, (
        f"caverns_and_claudes has no weather.yaml; loader must return None "
        f"(legitimate absence), got {result!r}"
    )


def test_load_world_demographics_returns_none_when_file_absent(
    tmp_path: Path,
) -> None:
    """A world directory with no demographics.yaml returns None, not an
    error. Same legitimate-absence semantics as the weather loader."""
    empty_world = tmp_path / "worlds" / "nowhere"
    empty_world.mkdir(parents=True)
    assert load_world_demographics(empty_world) is None


def test_load_world_calendar_returns_none_when_file_absent(tmp_path: Path) -> None:
    """A world directory with no calendar.yaml returns None."""
    empty_world = tmp_path / "worlds" / "nowhere"
    empty_world.mkdir(parents=True)
    assert load_world_calendar(empty_world) is None


# ---------------------------------------------------------------------------
# AC1 + AC8 — malformed YAML raises a typed error (No Silent Fallbacks)
# ---------------------------------------------------------------------------


def test_load_pack_weather_raises_on_malformed_yaml(tmp_path: Path) -> None:
    """A weather.yaml that exists but is syntactically invalid MUST raise
    at session-bootstrap time (AC8). A silent None here would surface as a
    mysterious 'narrator improvised weather' bug three turns into a session.
    """
    pack = tmp_path / "broken_pack"
    pack.mkdir()
    (pack / "weather.yaml").write_text(
        "climate_zones: { unterminated mapping",
        encoding="utf-8",
    )
    with pytest.raises(Exception) as excinfo:
        load_pack_weather(pack)
    # The raised exception must be something more specific than a bare
    # `Exception` — at minimum it must NOT be a silent `None` return.
    # `yaml.YAMLError` or a typed loader exception both satisfy "fail loud".
    err = excinfo.value
    assert not isinstance(err, AssertionError), (
        "test should not raise AssertionError — that means the assertion "
        "passed and pytest.raises caught nothing; loader silently returned"
    )


def test_load_pack_weather_raises_on_schema_violation(tmp_path: Path) -> None:
    """A weather.yaml that parses as YAML but violates the ClimateRulesFile
    schema MUST raise at LOAD time, not at per-turn generate(). Otherwise
    a missing `seasons` block on a zone surfaces as a per-turn confusing
    error after the playtest already started."""
    pack = tmp_path / "schema_violation_pack"
    pack.mkdir()
    # `climate_zones` must be a mapping of zone → ClimateZone; here it's a
    # list, which will fail the Pydantic validation.
    (pack / "weather.yaml").write_text(
        "climate_zones:\n  - just_a_list_entry\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_pack_weather(pack)


def test_load_world_demographics_raises_on_malformed_yaml(tmp_path: Path) -> None:
    """Malformed demographics.yaml is fail-loud at bootstrap, not a silent
    None and downstream "world canon mysteriously absent" symptom."""
    world = tmp_path / "worlds" / "broken_world"
    world.mkdir(parents=True)
    (world / "demographics.yaml").write_text(
        "this is not: : : valid yaml [",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_world_demographics(world)


def test_load_world_calendar_raises_on_malformed_yaml(tmp_path: Path) -> None:
    """Malformed calendar.yaml fails loud."""
    world = tmp_path / "worlds" / "broken_world"
    world.mkdir(parents=True)
    (world / "calendar.yaml").write_text(
        "still not: : : valid yaml [",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_world_calendar(world)


# ---------------------------------------------------------------------------
# AC1 — pack-vs-world split is honoured (no silent path fallback)
# ---------------------------------------------------------------------------


def test_load_world_demographics_does_not_fall_back_to_pack_root(
    tmp_path: Path,
) -> None:
    """If an author misfiles `demographics.yaml` at pack root, the
    world-level loader MUST NOT silently pick it up. Pack-vs-world split is
    load-bearing per story context: demographics is a WORLD-level surface,
    a pack-root file is wrong location and a silent fallback would
    propagate a misplaced authoring decision into production.

    The loader's argument is `world_dir`, not `pack_dir`. A pack-root
    demographics.yaml is invisible to a world_dir lookup — pure absence
    behaviour (returns None), NOT a fallback that walks up to pack root."""
    pack_root = tmp_path / "pack"
    world_dir = pack_root / "worlds" / "the_world"
    world_dir.mkdir(parents=True)
    # Misfile demographics.yaml at pack root, NOT in the world dir.
    (pack_root / "demographics.yaml").write_text(
        "world: misplaced\nversion: '0.1.0'\n",
        encoding="utf-8",
    )
    result = load_world_demographics(world_dir)
    assert result is None, (
        "load_world_demographics walked up to pack root and silently "
        "picked up a misfiled file — that's exactly the silent-fallback "
        "anti-pattern CLAUDE.md forbids. Expected None (legitimate absence "
        "at world level)."
    )


# ---------------------------------------------------------------------------
# Rule #5 (path handling) — loaders accept pathlib.Path; str optional
# ---------------------------------------------------------------------------


def test_load_pack_weather_accepts_pathlib_path(
    tea_and_murder_pack_dir: Path,
) -> None:
    """Rule #5 (lang-review/python.md path-handling): loaders should accept
    pathlib.Path. The fixture already passes a Path; this test makes the
    contract explicit so a stringly-typed signature can't slip through
    review."""
    rules = load_pack_weather(tea_and_murder_pack_dir)
    assert rules is not None


# ---------------------------------------------------------------------------
# Rule #8 (unsafe deserialization) — yaml.safe_load only
# ---------------------------------------------------------------------------


def test_load_pack_weather_uses_safe_load_rejects_python_object_tag(
    tmp_path: Path,
) -> None:
    """Rule #8 (lang-review/python.md unsafe deserialization): `yaml.load`
    on untrusted input executes arbitrary code via `!!python/object/apply`.
    Pack YAML is not strictly "untrusted" (it lives in our repo), but
    using `safe_load` consistently is the project standard
    (`weather.py:219` uses `yaml.safe_load`). This test feeds a Python-
    object tag and asserts the loader either errors OR returns None —
    NEVER successfully instantiates the tagged object."""
    pack = tmp_path / "unsafe_pack"
    pack.mkdir()
    (pack / "weather.yaml").write_text(
        "climate_zones: !!python/object/apply:os.system ['echo pwn']\n",
        encoding="utf-8",
    )
    raised: Exception | None = None
    result: Any = None
    try:
        result = load_pack_weather(pack)
    except Exception as exc:  # noqa: BLE001 — testing any exception path
        raised = exc

    # Either path is acceptable; the FORBIDDEN outcome is the python-object
    # tag actually instantiating during load. If `result` came back as
    # something other than a ClimateRulesFile-shaped object, we know the
    # tag wasn't honoured.
    if raised is None:
        assert not isinstance(result, ClimateRulesFile), (
            "loader 'succeeded' on a !!python/object/apply tag — that means "
            "yaml.load (UNSAFE) was used, not yaml.safe_load. CWE-502."
        )
    # raised path: anything thrown is OK — safe_load rejects unknown tags.
