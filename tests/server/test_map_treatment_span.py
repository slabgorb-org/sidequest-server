"""RED (spec §5, plan task 7): the ``map.treatment_emitted`` OTEL span.

Fires from _maybe_emit_cartography_map ONLY when the MAP_UPDATE payload carries
a treatment block, with component="location" and fields {world, treatment_kind,
region_count, anchor_count, has_image}. The GM panel is the lie-detector: this
span proves the treatment seam engaged rather than being silently skipped.

Two flavors:
  * capture tests — monkeypatch _watcher_publish, assert the span fires / does
    not fire with the right fields.
  * DB-readback WIRING test — bind a real PgTelemetrySink, drive the REAL
    _maybe_emit_cartography_map out-of-frame, and read the row back from
    turn_telemetry. Proves the span reaches the durable sink via publish_event
    (NOT Span.open alone) — the mandatory production-reachability test.
"""

import uuid
from types import SimpleNamespace

import pytest

import sidequest.server.websocket_handlers.map_emit as map_emit
from sidequest.genre.models.world import MapProvenance, MapTreatmentConfig


def _sd_and_snapshot(mt):
    region = SimpleNamespace(name="The Glenross Arms", description="pub", summary="", adjacent=[])
    cart = SimpleNamespace(
        navigation_mode="region",
        starting_region="the_glenross_arms",
        regions={"the_glenross_arms": region},
        routes=[],
        discovery_mode="public",
    )
    world = SimpleNamespace(cartography=cart, is_cluster=False, map_treatment=mt)
    pack = SimpleNamespace(worlds={"glenross": world})
    sd = SimpleNamespace(
        genre_pack=pack, world_slug="glenross", genre_slug="tea_and_murder", player_id=""
    )
    snapshot = SimpleNamespace(
        current_region="the_glenross_arms",
        discovered_regions=["the_glenross_arms"],
        party_location=lambda perspective=None: "the_glenross_arms",
    )
    return sd, snapshot


def _raster_treatment() -> MapTreatmentConfig:
    return MapTreatmentConfig(
        treatment="raster",
        image="sheet.jpg",
        provenance=MapProvenance(source="OS", date="1900", archive="NLS", pd_basis="x"),
        node_anchors={"the_glenross_arms": [1, 2]},
    )


# --- Capture tests -----------------------------------------------------------


def test_treatment_span_fires_when_treatment_present(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(
        map_emit,
        "_watcher_publish",
        lambda et, fields, **k: captured.append({"event_type": et, "fields": fields, **k}),
    )
    # raising=False: resolve_asset_url is imported into session_helpers by task 6;
    # in RED the name is absent, so the no-op patch lets the test fail on the
    # missing span rather than a setup AttributeError.
    monkeypatch.setattr(
        "sidequest.server.session_helpers.resolve_asset_url",
        lambda p, **k: f"https://cdn/{p}",
        raising=False,
    )
    sd, snapshot = _sd_and_snapshot(_raster_treatment())
    map_emit._maybe_emit_cartography_map(
        object(), sd=sd, snapshot=snapshot, emit_fn=lambda msg, t: None
    )
    hits = [e for e in captured if e["event_type"] == "map.treatment_emitted"]
    assert len(hits) == 1
    assert hits[0]["fields"]["treatment_kind"] == "raster"
    assert hits[0]["fields"]["anchor_count"] == 1
    assert hits[0]["component"] == "location"


def test_no_treatment_span_when_absent(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(
        map_emit,
        "_watcher_publish",
        lambda et, fields, **k: captured.append({"event_type": et}),
    )
    sd, snapshot = _sd_and_snapshot(None)
    map_emit._maybe_emit_cartography_map(
        object(), sd=sd, snapshot=snapshot, emit_fn=lambda msg, t: None
    )
    assert not [e for e in captured if e["event_type"] == "map.treatment_emitted"]


# --- DB-readback WIRING test (mandatory production-reachability) -------------


@pytest.fixture
def bound_pg_sink(monkeypatch, migrated_db: str):
    """Bind a real PgTelemetrySink to the watcher hub against a per-worker
    throwaway PG db; yield (pool, session_id). Modeled on
    tests/game/test_mechanical_census_contract.py::repo_and_sink."""
    from sidequest.game import db_pool
    from sidequest.game.pg import sessions
    from sidequest.game.pg.telemetry import PgTelemetrySink
    from sidequest.telemetry.watcher_hub import bind_event_store

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"sq_maptreat_{uuid.uuid4().hex[:8]}"
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


def test_treatment_span_reaches_turn_telemetry(bound_pg_sink, monkeypatch) -> None:
    """Drive the REAL _maybe_emit_cartography_map with a raster treatment and a
    bound PgTelemetrySink; a map.treatment_emitted row must land in
    turn_telemetry. Proves the span reaches the durable sink via publish_event
    (the prod path the GM panel reads), not merely a Span.open decoration."""
    pool, sid = bound_pg_sink
    monkeypatch.setattr(
        "sidequest.server.session_helpers.resolve_asset_url",
        lambda p, **k: f"https://cdn/{p}",
        raising=False,
    )
    sd, snapshot = _sd_and_snapshot(_raster_treatment())
    map_emit._maybe_emit_cartography_map(
        object(), sd=sd, snapshot=snapshot, emit_fn=lambda msg, t: None
    )
    with pool.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry "
            "WHERE session_id = %s AND event_type = 'map.treatment_emitted'",
            (sid,),
        ).fetchone()[0]
        component = conn.execute(
            "SELECT component FROM turn_telemetry "
            "WHERE session_id = %s AND event_type = 'map.treatment_emitted' LIMIT 1",
            (sid,),
        ).fetchone()
    assert count == 1, "map.treatment_emitted did not reach turn_telemetry via publish_event"
    assert component is not None and component[0] == "location"
