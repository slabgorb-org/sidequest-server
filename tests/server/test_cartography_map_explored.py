"""``_build_cartography_map_message`` populates ``explored`` from the PC's
visited regions (follow-up to ui #330 / ping-pong #329).

The merged client (sidequest-ui ``MapOverlay.tsx``) highlights
visited-but-not-current regions from ``mapData.explored[].id ?? .name``,
but the server never populated ``explored`` for region-mode worlds — so
visited regions stayed dark. These tests cover the gap:

  * valid region slugs in ``discovered_regions`` become ``{id,name}``
    entries in ``payload.explored``;
  * scene-title pollution (ping-pong #329: ``discovered_regions`` carries
    narration titles like ``"A Field of Blue Flowers, Munchkin Country"``
    that are NOT region slugs) is filtered to valid regions only — a
    legitimate cleanup, not a silent fallback;
  * the default (no ``discovered_regions`` arg) yields an empty
    ``explored`` — back-compat for the callers/tests that don't pass it;
  * the ``cartography.map_emitted`` OTEL span fires with the
    visited / discovered / dropped census so the GM panel can verify the
    overlay is engine-backed.

Fixtures, not live packs (project memory: no content-coupled unit tests).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server.session_helpers import _build_cartography_map_message


def _region(name: str, adjacent: list[str]) -> Region:
    return Region(name=name, summary=name, description=name, adjacent=adjacent)


def _oz_pack() -> SimpleNamespace:
    """A synthetic region-mode pack with three connected Oz regions."""
    regions = {
        "munchkin_country": _region("Munchkin Country", ["yellow_brick_road"]),
        "yellow_brick_road": _region("The Yellow Brick Road", ["munchkin_country", "emerald_city"]),
        "emerald_city": _region("The Emerald City", ["yellow_brick_road"]),
    }
    cart = CartographyConfig(navigation_mode=NavigationMode.region, regions=regions)
    world = SimpleNamespace(cartography=cart)
    return SimpleNamespace(worlds={"oz": world})


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


def _explored_of(msg) -> list[dict]:
    assert msg is not None
    return list(msg.payload.explored)


def test_default_no_discovered_regions_yields_empty_explored() -> None:
    """Back-compat: callers/tests that don't pass discovered_regions get []."""
    msg = _build_cartography_map_message(
        _oz_pack(),
        "oz",
        "yellow_brick_road",
    )
    assert _explored_of(msg) == []


def test_valid_regions_populate_explored_as_id_name() -> None:
    msg = _build_cartography_map_message(
        _oz_pack(),
        "oz",
        "yellow_brick_road",
        discovered_regions=["munchkin_country", "emerald_city"],
    )
    explored = _explored_of(msg)
    assert explored == [
        {
            "id": "munchkin_country",
            "name": "Munchkin Country",
            "connections": ["yellow_brick_road"],
        },
        {
            "id": "emerald_city",
            "name": "The Emerald City",
            "connections": ["yellow_brick_road"],
        },
    ]


def test_explored_carries_connections_from_adjacent() -> None:
    """server #632: each explored entry carries ``connections`` sourced from
    the region's ``adjacent`` list, so MapOverlay can draw node-graph edges.
    Before this, region-mode explored entries had no ``connections`` field —
    isolated nodes at best, and an unguarded ``for…of`` crashed the whole
    GameBoard (ui #330)."""
    msg = _build_cartography_map_message(
        _oz_pack(),
        "oz",
        "munchkin_country",
        discovered_regions=["yellow_brick_road"],
    )
    explored = _explored_of(msg)
    assert len(explored) == 1
    # yellow_brick_road borders both munchkin_country and emerald_city.
    assert explored[0]["connections"] == ["munchkin_country", "emerald_city"]


def test_scene_title_pollution_is_filtered_out() -> None:
    """Ping-pong #329: discovered_regions is polluted with scene titles
    that aren't region slugs. The filter-to-valid-regions keeps only real
    regions (load-bearing cleanup, not a silent fallback)."""
    msg = _build_cartography_map_message(
        _oz_pack(),
        "oz",
        "yellow_brick_road",
        discovered_regions=[
            "munchkin_country",
            "A Field of Blue Flowers, Munchkin Country",  # scene title, not a slug
            "emerald_city",
            "not_a_real_region",  # unknown slug
        ],
    )
    explored = _explored_of(msg)
    ids = [e["id"] for e in explored]
    assert ids == ["munchkin_country", "emerald_city"]


def test_duplicate_visited_regions_deduped_preserving_order() -> None:
    msg = _build_cartography_map_message(
        _oz_pack(),
        "oz",
        "emerald_city",
        discovered_regions=[
            "emerald_city",
            "munchkin_country",
            "emerald_city",  # duplicate
        ],
    )
    ids = [e["id"] for e in _explored_of(msg)]
    assert ids == ["emerald_city", "munchkin_country"]


def test_emit_fires_cartography_map_span_with_census(
    exporter: InMemorySpanExporter,
) -> None:
    _build_cartography_map_message(
        _oz_pack(),
        "oz",
        "yellow_brick_road",
        discovered_regions=[
            "munchkin_country",
            "A Field of Blue Flowers, Munchkin Country",  # dropped
            "emerald_city",
        ],
    )
    spans = [s for s in exporter.get_finished_spans() if s.name == "cartography.map_emitted"]
    assert len(spans) == 1, (
        f"expected exactly one cartography.map_emitted span, got {len(spans)}: "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs["visited_count"] == 2
    assert attrs["discovered_total"] == 3
    assert attrs["dropped_count"] == 1
    assert attrs["world_slug"] == "oz"


def test_no_span_when_no_region_cartography(exporter: InMemorySpanExporter) -> None:
    """Room-graph / non-region worlds return None and emit no map span."""
    cart = CartographyConfig(navigation_mode=NavigationMode.room_graph, regions={})
    pack = SimpleNamespace(worlds={"dungeon": SimpleNamespace(cartography=cart)})
    msg = _build_cartography_map_message(
        pack, "dungeon", "ropefoot", discovered_regions=["ropefoot"]
    )
    assert msg is None
    spans = [s for s in exporter.get_finished_spans() if s.name == "cartography.map_emitted"]
    assert spans == []


# ---------------------------------------------------------------------------
# discovery_mode: fog (sq-playtest 2026-06-07 — perseus_cloud's full
# 35-system catalog, secret systems included, rendered at discovered=1).
# Under fog only discovered regions ship full lore; their undiscovered
# neighbors ship name-only (the frontier); everything else stays off the
# wire. ``public`` (default) keeps the legacy full-catalog behavior — all
# tests above pin that path.
# ---------------------------------------------------------------------------


def _sector_pack(discovery_mode: str = "fog") -> SimpleNamespace:
    """A five-region star sector: yula — gate — deepwell, plus a secret
    system adjacent only to deepwell and an isolated system adjacent to
    nothing discovered."""
    regions = {
        "yula": _region("Yula", ["gate"]),
        "gate": _region("The Gate", ["yula", "deepwell"]),
        "deepwell": _region("Deepwell", ["gate", "secret_haven"]),
        "secret_haven": _region("Secret Haven", ["deepwell"]),
        "far_reach": _region("Far Reach", []),
    }
    routes = [
        SimpleNamespace(name="Yula Run", description="milk run", from_id="yula", to_id="gate"),
        SimpleNamespace(
            name="Gate Drop", description="deep haul", from_id="gate", to_id="deepwell"
        ),
        SimpleNamespace(
            name="Haven Slip", description="secret lane", from_id="deepwell", to_id="secret_haven"
        ),
    ]
    cart = CartographyConfig(
        navigation_mode=NavigationMode.region,
        regions=regions,
        discovery_mode=discovery_mode,  # type: ignore[arg-type]
    )
    # routes is a typed field; attach untyped namespaces the same way the
    # builder reads them (getattr walk).
    object.__setattr__(cart, "routes", routes)
    world = SimpleNamespace(cartography=cart)
    return SimpleNamespace(worlds={"sector": world})


def test_fog_ships_only_discovered_plus_nameonly_frontier() -> None:
    msg = _build_cartography_map_message(
        _sector_pack(),
        "sector",
        "yula",
        discovered_regions=["yula"],
    )
    assert msg is not None
    shipped = msg.payload.cartography["regions"]
    # Discovered: full entry.
    assert shipped["yula"]["description"] == "Yula"
    assert "undiscovered" not in shipped["yula"]
    # Frontier (adjacent to discovered): name-only, flagged, no onward edges.
    assert shipped["gate"] == {
        "name": "The Gate",
        "description": None,
        "adjacent": [],
        "undiscovered": True,
    }
    # Beyond the frontier: absent — deepwell, the secret system, and the
    # isolated system never reach the wire.
    assert "deepwell" not in shipped
    assert "secret_haven" not in shipped
    assert "far_reach" not in shipped


def test_fog_routes_filtered_to_discovered_endpoints() -> None:
    msg = _build_cartography_map_message(
        _sector_pack(),
        "sector",
        "yula",
        discovered_regions=["yula"],
    )
    assert msg is not None
    routes = msg.payload.cartography["routes"]
    names = [r["name"] for r in routes]
    # yula->gate: one discovered endpoint, other on the frontier → ships.
    assert "Yula Run" in names
    # gate->deepwell: neither endpoint discovered → leaks topology, absent.
    assert "Gate Drop" not in names
    # deepwell->secret_haven: fully beyond the frontier → absent.
    assert "Haven Slip" not in names


def test_fog_current_location_counts_as_discovered_even_if_ledger_lags() -> None:
    """The party is standing in deepwell but the visited ledger only has
    yula — current location must still ship full and open its frontier."""
    msg = _build_cartography_map_message(
        _sector_pack(),
        "sector",
        "deepwell",
        discovered_regions=["yula"],
    )
    assert msg is not None
    shipped = msg.payload.cartography["regions"]
    assert shipped["deepwell"]["description"] == "Deepwell"
    # Its neighbor (the secret system) is now legitimately on the frontier —
    # name-only.
    assert shipped["secret_haven"]["undiscovered"] is True
    assert shipped["secret_haven"]["description"] is None


def test_fog_discovered_adjacency_prunes_hidden_edges() -> None:
    """A discovered region's adjacency list must not name regions that are
    neither discovered nor frontier (no graph leakage through edges)."""
    msg = _build_cartography_map_message(
        _sector_pack(),
        "sector",
        "yula",
        discovered_regions=["yula", "gate"],
    )
    assert msg is not None
    shipped = msg.payload.cartography["regions"]
    # gate is discovered; deepwell is its frontier → edge allowed.
    assert "deepwell" in shipped["gate"]["adjacent"]
    # deepwell (frontier) carries no onward adjacency.
    assert shipped["deepwell"]["adjacent"] == []
    assert "secret_haven" not in shipped


def test_public_mode_ships_full_catalog_unchanged() -> None:
    """Default behavior pinned: public worlds (town maps) keep the whole
    catalog with descriptions."""
    msg = _build_cartography_map_message(
        _sector_pack(discovery_mode="public"),
        "sector",
        "yula",
        discovered_regions=["yula"],
    )
    assert msg is not None
    shipped = msg.payload.cartography["regions"]
    assert set(shipped) == {"yula", "gate", "deepwell", "secret_haven", "far_reach"}
    assert shipped["secret_haven"]["description"] == "Secret Haven"


def test_fog_span_reports_disclosure_census(exporter: InMemorySpanExporter) -> None:
    msg = _build_cartography_map_message(
        _sector_pack(),
        "sector",
        "yula",
        discovered_regions=["yula"],
    )
    assert msg is not None
    spans = [s for s in exporter.get_finished_spans() if s.name == "cartography.map_emitted"]
    assert spans
    attrs = spans[0].attributes or {}
    assert attrs.get("discovery_mode") == "fog"
    assert attrs.get("regions_shipped") == 2  # yula + frontier gate
    assert attrs.get("regions_total") == 5
