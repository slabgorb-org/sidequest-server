"""Story 71-25 — perseus_cloud location grounding (AC2).

Sibling to ``tests/genre/test_perseus_cloud_poi_grounding.py`` (AC1, the
content declaration + loader-path proof). This file proves the *resolve*
half of the grounding contract against the **real** loaded pack:

* A ``narrator_proactive`` resolve of "New Kowloon" against yula's real
  manifest is an authored HIT — ``resolved=True``,
  ``mode_outcome="matched"``, ``provenance="authored"`` — NOT a
  ``NOT_FOUND`` and NOT a ``yes_and_minted`` entity.
* The same resolve driven through the **real** ``resolve_location_entity``
  tool emits a ``location.entity.resolve`` span marking the manifest hit
  and fires **no** ``location.entity.minted`` span. OTEL is the
  lie-detector (CLAUDE.md): the span — not the YAML text — proves the
  entity is grounded end-to-end and survives refactor.
* Edge: a ``player_initiated`` resolve of a POI that is *not* declared in
  yula still mints a ``yes_and`` entity — grounding New Kowloon must not
  regress the Yes-And mint path for genuinely-new player-named places.

These tests intentionally bind to live ``space_opera`` / ``perseus_cloud``
content: the story is a content-grounding verification.
"""

from __future__ import annotations

from pathlib import Path
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
from sidequest.game.location_resolver import _normalize, resolve
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.models import LocationEntity

from .conftest import make_mock_repository

_WORLD = "perseus_cloud"
_REGION = "yula"
_NEW_KOWLOON_LABEL = "New Kowloon"
# A POI deliberately absent from yula's manifest — used to prove the
# Yes-And mint path is unbroken after New Kowloon is grounded.
_UNDECLARED_LABEL = "the foldspace transit kiosk"


@pytest.fixture(scope="module")
def perseus_pack(content_dir: Path):
    return load_genre_pack(content_dir / "genre_packs" / "space_opera")


@pytest.fixture
def yula_entities(perseus_pack) -> list[LocationEntity]:
    return list(perseus_pack.worlds[_WORLD].cartography.regions[_REGION].entities)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


def _build_ctx(perseus_pack, *, turn_number: int = 3) -> ToolContext:
    """A ToolContext carrying the real loaded pack and a mock PG-interface
    repository (location-promotion list/upsert backed in memory)."""
    return ToolContext(
        world_id=_WORLD,
        session_id="test-71-25",
        perspective_pc=None,
        turn_number=turn_number,
        repository=make_mock_repository(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=perseus_pack,
    )


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _only(exporter: InMemorySpanExporter, name: str):
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, (
        f"expected exactly one {name!r} span, got {len(matches)}: {_names(exporter)}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# AC2 — pure resolver: New Kowloon is an authored HIT, not a miss/mint
# ---------------------------------------------------------------------------


def test_new_kowloon_proactive_resolves_as_authored_hit(
    yula_entities: list[LocationEntity],
) -> None:
    """The core AC2 assertion. A narrator_proactive resolve of "New
    Kowloon" against yula's real authored manifest must be an authored
    hit. RED today: ``yula_entities == []`` → no match → ``resolved=False``."""
    store = make_mock_repository()
    resolution = resolve(
        store=store,
        region_id=_REGION,
        authored_entities=yula_entities,
        label=_NEW_KOWLOON_LABEL,
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert resolution.resolved is True
    assert resolution.mode_outcome == "matched"
    assert resolution.entity is not None
    assert _normalize(resolution.entity.label) == _normalize(_NEW_KOWLOON_LABEL)
    assert resolution.entity.provenance == "authored"
    # An authored hit must NOT have minted a promotion row.
    assert store.upsert_location_promotion.call_count == 0


# ---------------------------------------------------------------------------
# AC2 — OTEL lie-detector: resolve through the real tool emits a HIT span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_kowloon_resolves_through_tool_emits_resolve_hit_span(
    perseus_pack, exporter: InMemorySpanExporter
) -> None:
    """End-to-end wiring proof through the production tool entry point.
    A ``location.entity.resolve`` span fires marking the manifest hit, and
    **no** ``location.entity.minted`` span is emitted. This — not a YAML
    text grep — is what proves New Kowloon is grounded (survives refactor,
    fails on real wiring breakage). RED today: the resolve span carries
    ``resolved=False`` / ``mode_outcome="no_match"``."""
    ctx = _build_ctx(perseus_pack)
    args = ResolveLocationEntityArgs(
        label=_NEW_KOWLOON_LABEL,
        region_id=_REGION,
        mode="narrator_proactive",
        engagement_kind="mention",
    )

    await resolve_location_entity(args, ctx)

    span = _only(exporter, "location.entity.resolve")
    attrs = span.attributes or {}
    assert attrs["resolved"] is True
    assert attrs["mode"] == "narrator_proactive"
    assert attrs["mode_outcome"] == "matched"
    assert attrs["region_id"] == _REGION
    # The grounding signal: a hit, never an improvised mint.
    assert "location.entity.minted" not in _names(exporter)


@pytest.mark.asyncio
async def test_new_kowloon_tool_returns_ok_not_not_found(perseus_pack) -> None:
    """The tool's ToolResult for a grounded New Kowloon must be OK (the
    pending narrator action commits), not NOT_FOUND (contract violation)."""
    from sidequest.agents.tool_registry import ToolResultStatus

    ctx = _build_ctx(perseus_pack)
    result = await resolve_location_entity(
        ResolveLocationEntityArgs(
            label=_NEW_KOWLOON_LABEL,
            region_id=_REGION,
            mode="narrator_proactive",
            engagement_kind="mention",
        ),
        ctx,
    )
    assert result.status is ToolResultStatus.OK
    assert result.payload is not None
    assert result.payload["resolved"] is True
    assert result.payload["mode_outcome"] == "matched"


# ---------------------------------------------------------------------------
# AC2 edge — Yes-And mint path stays unbroken for undeclared POIs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undeclared_poi_player_initiated_still_mints(
    perseus_pack, exporter: InMemorySpanExporter
) -> None:
    """Regression guard (green now, must stay green): grounding New Kowloon
    must not break the Yes-And path. A player_initiated resolve of a POI
    that is NOT declared in yula still mints a yes_and entity and fires the
    ``location.entity.minted`` span."""
    from sidequest.agents.tool_registry import ToolResultStatus

    ctx = _build_ctx(perseus_pack, turn_number=7)
    result = await resolve_location_entity(
        ResolveLocationEntityArgs(
            label=_UNDECLARED_LABEL,
            region_id=_REGION,
            mode="player_initiated",
            engagement_kind="mention",
        ),
        ctx,
    )
    assert result.status is ToolResultStatus.OK
    assert result.payload is not None
    assert result.payload["mode_outcome"] == "minted"
    assert result.payload["entity"]["tier"] == "yes_and"
    assert result.payload["entity"]["provenance"] == "yes_and_minted"

    names = _names(exporter)
    assert "location.entity.resolve" in names
    assert "location.entity.minted" in names
