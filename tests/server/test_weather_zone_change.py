"""RED (spec §2 A2, plan task 19): per-region-change weather re-generation.

As the party crosses into a region of a different climate zone, the session
re-samples ``sd.weather_state`` (deterministic seed derived from the region, so
the same region always yields the same weather) and emits a
``weather.zone_changed`` watcher event (``component="location"``) — the GM
panel's lie-detector for the weather subsystem (CLAUDE.md OTEL Observability).

Three layers, each independently RED until task 19 lands:
  * reflection tripwire — ``_SessionData`` gains the two cache fields
    (sanctioned inspect-the-runtime-type check, NOT a source grep).
  * pure helper — ``regenerate_weather_for_region`` re-samples state
    deterministically and no-ops without a generator.
  * emit wiring — the extracted ``_maybe_regenerate_weather_on_region_change``
    helper fires ``weather.zone_changed`` on a real zone change, verified by a
    capture test AND a DB-readback that reaches ``turn_telemetry``.

Design note: the plan sketched the emit inline in ``websocket_session_handler``.
This suite instead targets an extracted ``map_emit`` helper, mirroring the
sibling ``_maybe_emit_cartography_map`` / ``test_map_treatment_span.py`` — the
established, refactor-stable, unit-drivable house pattern for this exact seam.
See the TEA design deviation in the session file.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import sidequest.server.websocket_handlers.map_emit as map_emit
from sidequest.game.weather import WeatherGenerator
from sidequest.server.session_state import _SessionData


@pytest.fixture
def two_zone_generator(tmp_path: Path) -> WeatherGenerator:
    """A real generator over two autumn zones — glen_floor and highland_pass."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  glen_floor:
    seasons:
      autumn:
        temp_range: [5, 12]
        conditions: [smirr]
        weights: [1]
  highland_pass:
    seasons:
      autumn:
        temp_range: [-2, 6]
        conditions: [blizzard]
        weights: [1]
""",
        encoding="utf-8",
    )
    return WeatherGenerator(yaml_path)


# ---------------------------------------------------------------------------
# Reflection tripwire: _SessionData caches the generator + season at bootstrap
# ---------------------------------------------------------------------------


def test_session_data_has_weather_cache_fields() -> None:
    """Re-gen on the hot turn path must not re-resolve the world dir, so the
    generator + season are cached on the session. Interrogates the dataclass
    fields (runtime type), not source text — the sanctioned tripwire pattern."""
    fields = set(_SessionData.__dataclass_fields__)
    assert "weather_generator" in fields
    assert "weather_season" in fields


# ---------------------------------------------------------------------------
# Pure helper: regenerate_weather_for_region
# ---------------------------------------------------------------------------


def _regen_sd(gen: WeatherGenerator, *, game_slug: str = "glenross_test") -> SimpleNamespace:
    return SimpleNamespace(
        game_slug=game_slug,
        weather_generator=gen,
        weather_season="autumn",
        weather_state=gen.generate("glen_floor", "autumn", seed=1),
    )


def test_regenerate_weather_for_region_flips_zone(two_zone_generator: WeatherGenerator) -> None:
    """Re-sampling for a new region updates ``sd.weather_state`` to that zone."""
    from sidequest.game.world_grounding_bootstrap import regenerate_weather_for_region

    sd = _regen_sd(two_zone_generator)
    assert sd.weather_state.zone == "glen_floor"

    regenerate_weather_for_region(sd, "castle_ross", "highland_pass")
    assert sd.weather_state.zone == "highland_pass"


def test_regenerate_weather_is_deterministic_by_region(
    two_zone_generator: WeatherGenerator,
) -> None:
    """Same session + same region → identical weather every time.

    The seed is derived from the region (not a fresh RNG), so a party that
    leaves and returns to a region sees the SAME weather — reproducible across
    process restarts, auditable by the GM panel."""
    from sidequest.game.world_grounding_bootstrap import regenerate_weather_for_region

    sd_a = _regen_sd(two_zone_generator)
    sd_b = _regen_sd(two_zone_generator)
    regenerate_weather_for_region(sd_a, "castle_ross", "highland_pass")
    regenerate_weather_for_region(sd_b, "castle_ross", "highland_pass")
    assert sd_a.weather_state.model_dump() == sd_b.weather_state.model_dump()


def test_regenerate_weather_noops_without_generator(
    two_zone_generator: WeatherGenerator,
) -> None:
    """A world with no ``weather.yaml`` (generator None) must not crash and must
    leave the state untouched — no silent substitution of default weather."""
    from sidequest.game.world_grounding_bootstrap import regenerate_weather_for_region

    sd = _regen_sd(two_zone_generator)
    sd.weather_generator = None
    before = sd.weather_state
    regenerate_weather_for_region(sd, "castle_ross", "highland_pass")
    assert sd.weather_state is before


# ---------------------------------------------------------------------------
# Emit wiring: _maybe_regenerate_weather_on_region_change fires the span
# ---------------------------------------------------------------------------


def _emit_sd_snapshot(
    gen: WeatherGenerator, *, region_zone: str | None, current_zone: str
) -> tuple[SimpleNamespace, SimpleNamespace]:
    region = SimpleNamespace(weather_zone=region_zone)
    cart = SimpleNamespace(regions={"castle_ross": region})
    world = SimpleNamespace(cartography=cart)
    pack = SimpleNamespace(worlds={"glenross": world})
    sd = SimpleNamespace(
        genre_pack=pack,
        world_slug="glenross",
        genre_slug="tea_and_murder",
        player_id="",
        game_slug="glenross_test",
        weather_generator=gen,
        weather_season="autumn",
        weather_state=gen.generate(current_zone, "autumn", seed=1),
    )
    snapshot = SimpleNamespace(current_region="castle_ross")
    return sd, snapshot


def test_weather_zone_change_helper_is_importable() -> None:
    """The extracted emit helper must exist on the production module."""
    assert callable(map_emit._maybe_regenerate_weather_on_region_change)


def test_zone_change_emits_weather_zone_changed(
    two_zone_generator: WeatherGenerator, monkeypatch
) -> None:
    """Entering a region of a different zone re-samples state AND publishes one
    ``weather.zone_changed`` event with the from/to zones and location component."""
    captured: list[dict] = []
    monkeypatch.setattr(
        map_emit,
        "_watcher_publish",
        lambda et, fields, **k: captured.append({"event_type": et, "fields": fields, **k}),
    )
    sd, snapshot = _emit_sd_snapshot(
        two_zone_generator, region_zone="highland_pass", current_zone="glen_floor"
    )

    map_emit._maybe_regenerate_weather_on_region_change(object(), sd=sd, snapshot=snapshot)

    hits = [e for e in captured if e["event_type"] == "weather.zone_changed"]
    assert len(hits) == 1, (
        f"expected 1 weather.zone_changed, got {[e['event_type'] for e in captured]}"
    )
    assert hits[0]["fields"]["to_zone"] == "highland_pass"
    assert hits[0]["fields"]["from_zone"] == "glen_floor"
    assert hits[0]["fields"]["region"] == "castle_ross"
    assert hits[0]["fields"]["world"] == "glenross"
    assert hits[0]["component"] == "location"
    # And the state actually re-sampled to the new zone.
    assert sd.weather_state.zone == "highland_pass"


def test_no_emit_when_zone_unchanged(two_zone_generator: WeatherGenerator, monkeypatch) -> None:
    """Crossing into a region of the SAME zone is not a change — no event."""
    captured: list[dict] = []
    monkeypatch.setattr(
        map_emit,
        "_watcher_publish",
        lambda et, fields, **k: captured.append({"event_type": et}),
    )
    sd, snapshot = _emit_sd_snapshot(
        two_zone_generator, region_zone="glen_floor", current_zone="glen_floor"
    )

    map_emit._maybe_regenerate_weather_on_region_change(object(), sd=sd, snapshot=snapshot)
    assert not [e for e in captured if e["event_type"] == "weather.zone_changed"]


def test_no_emit_when_region_has_no_weather_zone(
    two_zone_generator: WeatherGenerator, monkeypatch
) -> None:
    """A region with no declared ``weather_zone`` leaves weather alone."""
    captured: list[dict] = []
    monkeypatch.setattr(
        map_emit,
        "_watcher_publish",
        lambda et, fields, **k: captured.append({"event_type": et}),
    )
    sd, snapshot = _emit_sd_snapshot(
        two_zone_generator, region_zone=None, current_zone="glen_floor"
    )

    map_emit._maybe_regenerate_weather_on_region_change(object(), sd=sd, snapshot=snapshot)
    assert not [e for e in captured if e["event_type"] == "weather.zone_changed"]


@pytest.fixture
def mismatched_season_generator(tmp_path: Path) -> WeatherGenerator:
    """glen_floor defines autumn; tundra defines ONLY winter — so the session's
    bootstrap season (autumn) is absent from tundra."""
    yaml_path = tmp_path / "weather.yaml"
    yaml_path.write_text(
        """
climate_zones:
  glen_floor:
    seasons:
      autumn:
        temp_range: [5, 12]
        conditions: [smirr]
        weights: [1]
  tundra:
    seasons:
      winter:
        temp_range: [-20, -5]
        conditions: [whiteout]
        weights: [1]
""",
        encoding="utf-8",
    )
    return WeatherGenerator(yaml_path)


def test_zone_change_skips_loud_when_new_zone_lacks_season(
    mismatched_season_generator: WeatherGenerator, monkeypatch
) -> None:
    """Entering a zone that does not define the session's season must NOT crash
    the turn. It skips the re-sample and emits ``weather.zone_change_skipped``
    (loud + observable) — never an uncaught raise, never a silent default.

    A content author can bind a region to a real climate zone whose season set
    doesn't overlap the session season; the pack validator (task 18) only checks
    the zone exists, so this only surfaces at runtime and must be contained."""
    captured: list[dict] = []
    monkeypatch.setattr(
        map_emit,
        "_watcher_publish",
        lambda et, fields, **k: captured.append({"event_type": et, "fields": fields, **k}),
    )
    region = SimpleNamespace(weather_zone="tundra")
    cart = SimpleNamespace(regions={"castle_ross": region})
    pack = SimpleNamespace(worlds={"glenross": SimpleNamespace(cartography=cart)})
    before = mismatched_season_generator.generate("glen_floor", "autumn", seed=1)
    sd = SimpleNamespace(
        genre_pack=pack,
        world_slug="glenross",
        genre_slug="tea_and_murder",
        player_id="",
        game_slug="glenross_test",
        weather_generator=mismatched_season_generator,
        weather_season="autumn",
        weather_state=before,
    )
    snapshot = SimpleNamespace(current_region="castle_ross")

    # Must not raise (the reviewer's reproduced turn-crash).
    map_emit._maybe_regenerate_weather_on_region_change(object(), sd=sd, snapshot=snapshot)

    assert not [e for e in captured if e["event_type"] == "weather.zone_changed"]
    skips = [e for e in captured if e["event_type"] == "weather.zone_change_skipped"]
    assert len(skips) == 1, f"expected 1 skip span, got {[e['event_type'] for e in captured]}"
    assert skips[0]["fields"]["to_zone"] == "tundra"
    assert skips[0]["fields"]["reason"] == "zone_missing_season"
    assert skips[0]["component"] == "location"
    # Weather is left untouched — no silent substitution of a default.
    assert sd.weather_state is before


# ---------------------------------------------------------------------------
# DB-readback WIRING test (mandatory production-reachability, CLAUDE.md)
# ---------------------------------------------------------------------------


@pytest.fixture
def bound_pg_sink(monkeypatch, migrated_db: str):
    """Bind a real PgTelemetrySink to the watcher hub against a per-worker
    throwaway PG db; yield (pool, session_id). Modeled on
    tests/server/test_map_treatment_span.py::bound_pg_sink."""
    from sidequest.game import db_pool
    from sidequest.game.pg import sessions
    from sidequest.game.pg.telemetry import PgTelemetrySink
    from sidequest.telemetry.watcher_hub import bind_event_store

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"sq_weatherzone_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="tea_and_murder", world_slug="glenross"
    )
    sink = PgTelemetrySink(pool, sid)
    bind_event_store(sink)
    try:
        yield pool, sid
    finally:
        bind_event_store(None)
        db_pool.close_pool()


def test_weather_zone_changed_reaches_turn_telemetry(
    bound_pg_sink, two_zone_generator: WeatherGenerator
) -> None:
    """Drive the REAL emit helper with a bound PgTelemetrySink on a genuine zone
    change; a ``weather.zone_changed`` row must land in ``turn_telemetry``.
    Proves the event reaches the durable sink the GM panel reads — not merely a
    monkeypatched capture."""
    pool, sid = bound_pg_sink
    sd, snapshot = _emit_sd_snapshot(
        two_zone_generator, region_zone="highland_pass", current_zone="glen_floor"
    )

    map_emit._maybe_regenerate_weather_on_region_change(object(), sd=sd, snapshot=snapshot)

    with pool.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry "
            "WHERE session_id = %s AND event_type = 'weather.zone_changed'",
            (sid,),
        ).fetchone()[0]
        component = conn.execute(
            "SELECT component FROM turn_telemetry "
            "WHERE session_id = %s AND event_type = 'weather.zone_changed' LIMIT 1",
            (sid,),
        ).fetchone()
    assert count == 1, "weather.zone_changed did not reach turn_telemetry via publish_event"
    assert component is not None and component[0] == "location"
