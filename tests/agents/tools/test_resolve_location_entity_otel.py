"""Resolver tool emits dedicated ``location.*`` OTEL spans (Story 54-8).

Sibling to ``test_resolve_location_entity.py`` (Story 54-6), which covers
the ``ctx.otel_span`` side-channel attribute-setting. 54-8 wraps the
call in a dedicated ``location.entity.resolve`` span and emits
``location.entity.minted`` / ``location.entity.promoted`` on those
``mode_outcome``s. The dedicated spans are what the GM panel reads;
the side-channel attributes stay for tool-dispatch introspection.

Wiring test (CLAUDE.md "Every test suite needs a wiring test"): each
test below calls the real ``resolve_location_entity`` function through
its production entry point, not a unit harness — proves the dedicated
spans actually fire from the tool execution path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import ToolContext
from sidequest.agents.tools.resolve_location_entity import (
    ResolveLocationEntityArgs,
    resolve_location_entity,
)
from sidequest.protocol.models import LocationEntity, LocationEntityBinding


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


def _authored() -> list[LocationEntity]:
    return [
        LocationEntity(
            id="bar",
            label="the bar",
            tier="real_object",
            binding=LocationEntityBinding(kind="location_feature", ref="glenross_arms_bar"),
        ),
        LocationEntity(id="cobwebs", label="cobwebs", tier="flavor_only"),
    ]


def _make_mock_repository() -> MagicMock:
    """Mock SaveRepository with PG location-promotion interface."""
    _rows: list[Any] = []
    repo = MagicMock()

    def _list(*, region_id: str) -> list[Any]:
        return [r for r in _rows if r.region_id == region_id]

    def _upsert(row: Any) -> None:
        for i, existing in enumerate(_rows):
            if existing.region_id == row.region_id and existing.entity_id == row.entity_id:
                _rows[i] = row
                return
        _rows.append(row)

    repo.list_location_promotions.side_effect = _list
    repo.upsert_location_promotion.side_effect = _upsert
    return repo


def _build_ctx(
    tmp_path: Path,
    *,
    region_id: str = "the_glenross_arms",
    entities: list[LocationEntity] | None = None,
    world_id: str = "glenross",
    turn_number: int = 3,
) -> ToolContext:
    """Mirror the fixture in ``test_resolve_location_entity.py`` — mock
    SaveRepository (PG interface) + a stubbed GenrePack carrying the entity list."""
    region = MagicMock()
    region.entities = entities if entities is not None else _authored()
    cartography = MagicMock()
    cartography.regions = {region_id: region}
    world = MagicMock()
    world.cartography = cartography
    genre_pack = MagicMock()
    genre_pack.worlds = {world_id: world}

    return ToolContext(
        world_id=world_id,
        session_id="test-session",
        perspective_pc=None,
        turn_number=turn_number,
        repository=_make_mock_repository(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
    )


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _only(exporter: InMemorySpanExporter, name: str):
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, (
        f"expected exactly one {name!r} span, got {len(matches)}: "
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# narrator_proactive — match path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_match_emits_resolve_span(
    tmp_path: Path, exporter: InMemorySpanExporter
) -> None:
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="the bar",
        region_id="the_glenross_arms",
        mode="narrator_proactive",
        engagement_kind="mention",
    )

    await resolve_location_entity(args, ctx)

    span = _only(exporter, "location.entity.resolve")
    attrs = span.attributes or {}
    assert attrs["resolved"] is True
    assert attrs["mode"] == "narrator_proactive"
    assert attrs["mode_outcome"] == "matched"
    assert attrs["entity_id"] == "bar"
    # Match path: no mint, no promotion.
    assert "location.entity.minted" not in _names(exporter)
    assert "location.entity.promoted" not in _names(exporter)


# ---------------------------------------------------------------------------
# narrator_proactive — miss path (lie-detector)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_miss_emits_resolve_span_marking_lie_detector(
    tmp_path: Path, exporter: InMemorySpanExporter
) -> None:
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="the dragon",
        region_id="the_glenross_arms",
        mode="narrator_proactive",
        engagement_kind="mechanical",
    )

    await resolve_location_entity(args, ctx)

    span = _only(exporter, "location.entity.resolve")
    attrs = span.attributes or {}
    assert attrs["resolved"] is False
    assert attrs["mode"] == "narrator_proactive"
    assert attrs["mode_outcome"] == "no_match"
    # Proactive miss: no mint, no promotion.
    assert "location.entity.minted" not in _names(exporter)
    assert "location.entity.promoted" not in _names(exporter)


# ---------------------------------------------------------------------------
# player_initiated — miss path (mint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_initiated_miss_emits_resolve_and_minted(
    tmp_path: Path, exporter: InMemorySpanExporter
) -> None:
    ctx = _build_ctx(tmp_path, turn_number=3)
    args = ResolveLocationEntityArgs(
        label="the antique sextant",
        region_id="the_glenross_arms",
        mode="player_initiated",
        engagement_kind="mention",
    )

    await resolve_location_entity(args, ctx)

    names = _names(exporter)
    assert "location.entity.resolve" in names
    assert "location.entity.minted" in names
    assert "location.entity.promoted" not in names

    minted = _only(exporter, "location.entity.minted")
    attrs = minted.attributes or {}
    assert attrs["region_id"] == "the_glenross_arms"
    assert attrs["label"] == "the antique sextant"
    assert attrs["turn"] == 3
    # entity_id is derived deterministically — non-empty.
    assert attrs["entity_id"], "minted span must carry the new entity id"


# ---------------------------------------------------------------------------
# flavor_only mechanical — promote path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flavor_only_mechanical_emits_resolve_and_promoted(
    tmp_path: Path, exporter: InMemorySpanExporter
) -> None:
    ctx = _build_ctx(tmp_path, turn_number=11)
    args = ResolveLocationEntityArgs(
        label="cobwebs",
        region_id="the_glenross_arms",
        mode="narrator_proactive",
        engagement_kind="mechanical",
    )

    await resolve_location_entity(args, ctx)

    names = _names(exporter)
    assert "location.entity.resolve" in names
    assert "location.entity.promoted" in names
    assert "location.entity.minted" not in names

    promoted = _only(exporter, "location.entity.promoted")
    attrs = promoted.attributes or {}
    assert attrs["from_tier"] == "flavor_only"
    assert attrs["to_tier"] == "yes_and"
    assert attrs["entity_id"] == "cobwebs"
    assert attrs["turn"] == 11


# ---------------------------------------------------------------------------
# matched path — no side-effect spans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matched_path_does_not_emit_minted_or_promoted(
    tmp_path: Path, exporter: InMemorySpanExporter
) -> None:
    """A normal match (real_object, ``engagement_kind="mention"``) emits the
    resolve span only — never a mint, never a promotion. Guards against the
    common bug where the dev wires the side-effect spans into the matched
    branch by accident."""
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="the bar",
        region_id="the_glenross_arms",
        mode="player_initiated",
        engagement_kind="mechanical",
    )

    await resolve_location_entity(args, ctx)

    names = _names(exporter)
    assert "location.entity.resolve" in names
    assert "location.entity.minted" not in names
    assert "location.entity.promoted" not in names
