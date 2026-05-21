"""RED — Story 24-10 AC2/AC3/AC4: bootstrap loads grounding + threads it to TurnContext.

Three hops of the wiring chain are exercised here, all through real
production functions (no source-text greps, per CLAUDE.md "No Source-Text
Wiring Tests"):

* **AC2/AC3 — bootstrap.** ``load_world_grounding`` is the connect-time
  orchestration that mirrors ``attach_dungeon_to_session`` (connect.py:763):
  it invokes the three loaders against the pack/world dirs, and — when a
  pack-level weather.yaml is present — constructs a single ``WeatherGenerator``
  and calls ``generate()`` once for the playtest's (zone, season, seed). That
  ``generate()`` call fires the ``world_grounding.weather_proposed`` span via
  the 24-7 hook already inside ``WeatherGenerator.generate``. We assert the
  span fired (the GM-panel lie-detector signal that bootstrap actually ran the
  generator, not just held the YAML).

* **AC4 — carrier fields exist.** Reflection tripwire (the allowed exception
  to the no-source-text rule — interrogates runtime dataclass types, not
  source strings): both ``_SessionData`` and ``TurnContext`` must gain
  ``weather_state`` / ``world_demographics`` / ``world_calendar``.

* **AC4 — threading.** ``_build_turn_context`` (session_handler.py) must copy
  the grounding off the session data onto the per-turn ``TurnContext``,
  exactly as it already threads ``lore_store`` / ``monster_manual``.

Fixtures only — synthetic pack/world dirs in ``tmp_path`` and a cloned
``test_genre`` fixture pack. No live ``genre_packs/*`` reads, no content
property assertions.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.weather import WeatherState

# Module under test — does not exist yet (RED).
from sidequest.game.world_grounding_loader import load_world_grounding
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData

_GROUNDING_FIELDS = {"weather_state", "world_demographics", "world_calendar"}

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
    "world": "synth_world",
    "parish": {"name": "Testburgh", "total_population": 412},
    "recurring_cast": [{"id": "minister", "name": "Rev. Test"}],
}
_VALID_CALENDAR: dict = {
    "version": "0.1.0",
    "world": "synth_world",
    "current": {"month": "October", "day": 14, "year": 1908},
}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _grounded_pack(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic pack dir with weather.yaml and a world dir with
    demographics.yaml + calendar.yaml. Returns (pack_dir, world_dir)."""
    pack_dir = tmp_path / "synth_pack"
    world_dir = pack_dir / "worlds" / "synth_world"
    _write(pack_dir / "weather.yaml", _VALID_WEATHER)
    _write(world_dir / "demographics.yaml", _VALID_DEMOGRAPHICS)
    _write(world_dir / "calendar.yaml", _VALID_CALENDAR)
    return pack_dir, world_dir


# ---------------------------------------------------------------------------
# AC2 / AC3 — bootstrap orchestration + weather_proposed span
# ---------------------------------------------------------------------------


def test_load_world_grounding_populates_all_three_sections(tmp_path: Path) -> None:
    """A fully-grounded synthetic pack → bootstrap returns a WeatherState plus
    demographics + calendar dicts (AC2: all three loaders invoked; AC3:
    generator instantiated and sampled at bootstrap)."""
    pack_dir, world_dir = _grounded_pack(tmp_path)

    grounding = load_world_grounding(
        pack_dir=pack_dir,
        world_dir=world_dir,
        zone="glen_floor",
        season="autumn",
        seed=42,
    )

    assert isinstance(grounding.weather_state, WeatherState)
    assert grounding.weather_state.zone == "glen_floor"
    assert grounding.weather_state.season == "autumn"
    assert grounding.weather_state.seed == 42
    assert isinstance(grounding.world_demographics, dict)
    assert grounding.world_demographics["parish"]["total_population"] == 412
    assert isinstance(grounding.world_calendar, dict)
    assert grounding.world_calendar["current"]["year"] == 1908


def test_load_world_grounding_fires_weather_proposed_span(
    tmp_path: Path, otel_capture: InMemorySpanExporter
) -> None:
    """AC3: instantiating the generator and calling generate() at bootstrap
    must fire the 24-7 ``world_grounding.weather_proposed`` span — the GM-panel
    proof that bootstrap actually sampled weather (vs. holding inert YAML).
    The proposed span pairs with weather_used (fired later in the tool) on the
    shared seed; if it never fires, the dashboard's lie-detector is blind to
    bootstrap."""
    pack_dir, world_dir = _grounded_pack(tmp_path)

    load_world_grounding(
        pack_dir=pack_dir,
        world_dir=world_dir,
        zone="glen_floor",
        season="autumn",
        seed=42,
    )

    proposed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.weather_proposed"
    ]
    assert len(proposed) == 1, (
        "bootstrap must fire exactly one weather_proposed span "
        f"(got {len(proposed)}; all spans: {[s.name for s in otel_capture.get_finished_spans()]})"
    )
    attrs = proposed[0].attributes or {}
    assert attrs["zone"] == "glen_floor"
    assert attrs["season"] == "autumn"
    assert attrs["seed"] == 42


def test_load_world_grounding_no_weather_yaml_leaves_weather_none_no_span(
    tmp_path: Path, otel_capture: InMemorySpanExporter
) -> None:
    """AC7 graceful path at the bootstrap level: a pack with NO weather.yaml
    must not construct a generator, must leave ``weather_state`` None, and must
    NOT fire weather_proposed. Demographics/calendar still load if present."""
    pack_dir = tmp_path / "synth_pack"
    world_dir = pack_dir / "worlds" / "synth_world"
    # No weather.yaml at pack root. Demographics + calendar present.
    _write(world_dir / "demographics.yaml", _VALID_DEMOGRAPHICS)
    _write(world_dir / "calendar.yaml", _VALID_CALENDAR)

    grounding = load_world_grounding(
        pack_dir=pack_dir,
        world_dir=world_dir,
        zone="glen_floor",
        season="autumn",
        seed=42,
    )

    assert grounding.weather_state is None
    assert grounding.world_demographics is not None
    assert grounding.world_calendar is not None
    proposed = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "world_grounding.weather_proposed"
    ]
    assert proposed == [], "weather_proposed must not fire when no weather.yaml is authored"


# ---------------------------------------------------------------------------
# AC4 — carrier fields exist (reflection tripwire)
# ---------------------------------------------------------------------------


def test_session_data_has_grounding_fields() -> None:
    """``_SessionData`` is the per-session carrier that the connect handler
    populates at bootstrap (mirroring lore_store / monster_manual). It must
    declare the three grounding fields so connect.py can stamp them."""
    field_names = {f.name for f in dataclasses.fields(_SessionData)}
    missing = _GROUNDING_FIELDS - field_names
    assert not missing, f"_SessionData is missing grounding carrier fields: {missing}"


def test_turn_context_has_grounding_fields() -> None:
    """``TurnContext`` (the per-turn object built by _build_turn_context) must
    declare the three grounding fields so they can propagate to ToolContext at
    orchestrator.py:3259."""
    from sidequest.agents.orchestrator import TurnContext

    field_names = {f.name for f in dataclasses.fields(TurnContext)}
    missing = _GROUNDING_FIELDS - field_names
    assert not missing, f"TurnContext is missing grounding fields: {missing}"


# ---------------------------------------------------------------------------
# AC4 — threading through the real _build_turn_context
# ---------------------------------------------------------------------------


def test_build_turn_context_threads_grounding_from_session_data(
    tmp_path: Path, minimal_pack_factory
) -> None:
    """The real ``_build_turn_context`` must copy weather_state /
    world_demographics / world_calendar off ``_SessionData`` onto the
    ``TurnContext`` — the same pass-through it already does for lore_store and
    monster_manual. Without this hop the bootstrap-loaded grounding never
    reaches the per-turn context and the ToolContext stays None every turn.

    Uses a cloned synthetic fixture pack as an inert structural vehicle for
    GenrePack — no content properties are asserted, only the wiring.
    """
    pack = minimal_pack_factory(tmp_path)
    genre_pack = load_genre_pack(pack.path)

    snap = GameSnapshot(
        genre_slug=pack.path.name,
        world_slug="synth_world",
        location="Main Hall",
        turn_manager=TurnManager(interaction=1),
    )
    sd = _SessionData(
        genre_slug=pack.path.name,
        world_slug="synth_world",
        player_name="TestHero",
        player_id="player:TestHero",
        snapshot=snap,
        store=MagicMock(),
        genre_pack=genre_pack,
        orchestrator=MagicMock(),
    )

    weather = WeatherState(
        zone="glen_floor",
        season="autumn",
        condition="smirr",
        temperature_c=11.5,
        precipitation=True,
        special_event=None,
        effects=[],
        seed=42,
    )
    demographics = {"parish": {"total_population": 412}, "recurring_cast": []}
    calendar = {"current": {"year": 1908}}

    # Stamp grounding onto the session carrier as the connect bootstrap will.
    sd.weather_state = weather
    sd.world_demographics = demographics
    sd.world_calendar = calendar

    ctx = _build_turn_context(sd)

    assert ctx.weather_state is weather
    assert ctx.world_demographics == demographics
    assert ctx.world_calendar == calendar
