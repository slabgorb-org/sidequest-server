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
        {"id": "munchkin_country", "name": "Munchkin Country"},
        {"id": "emerald_city", "name": "The Emerald City"},
    ]


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
