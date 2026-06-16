"""Tool adapter — resolve_location_entity (Story 54-6 / ADR-109).

Verifies the agent-facing surface of the resolver:

* The ``@tool`` adapter is registered under the name
  ``resolve_location_entity`` in ``default_registry`` (AC-8 wiring).
* ``narrator_proactive`` miss yields ``ToolResultStatus.NOT_FOUND`` so the
  narrator's pending mechanical action does not commit (AC-2 surface).
* ``player_initiated`` miss yields ``OK`` with a minted entity payload
  (AC-3 surface).
* ``flavor_only`` mechanical engagement promotes (AC-4 surface).
* OTEL attributes are set on ``ctx.otel_span`` (AC-9 — the lie-detector
  seam that 54-8 promotes to dedicated spans).
* Unknown region surfaces as ``NOT_FOUND`` rather than silently treating
  it as an empty manifest (no-silent-fallback rule from CLAUDE.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import resolve_location_entity as _module  # noqa: F401
from sidequest.agents.tools.resolve_location_entity import (
    ResolveLocationEntityArgs,
    resolve_location_entity,
)
from sidequest.game.persistence import SqliteStore
from sidequest.protocol.models import LocationEntity, LocationEntityBinding

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _build_ctx(
    tmp_path: Path,
    *,
    region_id: str = "the_glenross_arms",
    entities: list[LocationEntity] | None = None,
    world_id: str = "glenross",
) -> ToolContext:
    """Build a real ToolContext with a real SqliteStore and a stubbed
    GenrePack whose ``worlds[world_id].cartography.regions[region_id]`` has
    the supplied entities."""
    store = SqliteStore(tmp_path / "save.db")

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
        turn_number=3,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
    )


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None, f"expected OK payload, got status={r.status}"
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration / wiring (AC-8)
# ---------------------------------------------------------------------------


def test_resolve_location_entity_is_registered() -> None:
    """The @tool decorator registers the handler in default_registry under
    the name 'resolve_location_entity' as soon as the module is imported.
    This is the wiring proof — if the barrel in ``agents/tools/__init__.py``
    drops the import, this fails."""
    assert "resolve_location_entity" in default_registry.list_names()


def test_resolve_location_entity_registered_via_tools_barrel() -> None:
    """A consumer that imports the barrel package (not the adapter module
    directly) must still see the tool — confirms the barrel re-export."""
    # Force-reimport via the barrel to ensure registration is not contingent
    # on the test-file's explicit `from sidequest.agents.tools.resolve_...`.
    from sidequest.agents import tools as tools_pkg  # noqa: F401

    assert "resolve_location_entity" in default_registry.list_names()


def test_args_model_rejects_empty_label() -> None:
    """ResolveLocationEntityArgs.label must be min_length=1 — empty strings
    should not be accepted by the Pydantic args model."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ResolveLocationEntityArgs(
            label="",
            region_id="r",
            mode="narrator_proactive",
        )


def test_args_model_rejects_empty_region_id() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ResolveLocationEntityArgs(
            label="x",
            region_id="",
            mode="narrator_proactive",
        )


def test_args_model_rejects_invalid_mode() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ResolveLocationEntityArgs(
            label="x",
            region_id="r",
            mode="invented_mode",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# narrator_proactive (AC-2 surface)
# ---------------------------------------------------------------------------


async def test_proactive_match_returns_ok_with_resolution(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="the bar",
        region_id="the_glenross_arms",
        mode="narrator_proactive",
        engagement_kind="mention",
    )
    result = await resolve_location_entity(args, ctx)
    assert result.status is ToolResultStatus.OK
    payload = _payload(result)
    assert payload["resolved"] is True
    assert payload["entity"]["id"] == "bar"
    assert payload["mode_outcome"] == "matched"
    assert payload["region_id"] == "the_glenross_arms"


async def test_proactive_miss_returns_not_found(tmp_path: Path) -> None:
    """Lie-detector path. Narrator referenced something not in the manifest.
    Tool returns NOT_FOUND so the narrator's pending action does not commit."""
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="the dragon",
        region_id="the_glenross_arms",
        mode="narrator_proactive",
        engagement_kind="mechanical",
    )
    result = await resolve_location_entity(args, ctx)
    assert result.status is ToolResultStatus.NOT_FOUND
    assert result.message is not None
    assert "the dragon" in result.message
    # And no row was minted.
    assert (
        ctx.store.list_location_promotions(save_id="default", region_id="the_glenross_arms") == []
    )


# ---------------------------------------------------------------------------
# player_initiated (AC-3 surface)
# ---------------------------------------------------------------------------


async def test_player_initiated_miss_mints(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="the antique sextant",
        region_id="the_glenross_arms",
        mode="player_initiated",
        engagement_kind="mention",
    )
    result = await resolve_location_entity(args, ctx)
    assert result.status is ToolResultStatus.OK
    payload = _payload(result)
    assert payload["resolved"] is True
    assert payload["mode_outcome"] == "minted"
    assert payload["entity"]["tier"] == "yes_and"
    assert payload["entity"]["provenance"] == "yes_and_minted"
    # And the row hit the store.
    rows = ctx.store.list_location_promotions(save_id="default", region_id="the_glenross_arms")
    assert len(rows) == 1
    assert rows[0].label == "the antique sextant"


# ---------------------------------------------------------------------------
# flavor_only promotion (AC-4 surface)
# ---------------------------------------------------------------------------


async def test_flavor_only_mechanical_engagement_promotes(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="cobwebs",
        region_id="the_glenross_arms",
        mode="narrator_proactive",
        engagement_kind="mechanical",
    )
    result = await resolve_location_entity(args, ctx)
    assert result.status is ToolResultStatus.OK
    payload = _payload(result)
    assert payload["mode_outcome"] == "promoted"
    assert payload["entity"]["tier"] == "yes_and"
    assert payload["entity"]["provenance"] == "yes_and_promoted"
    rows = ctx.store.list_location_promotions(save_id="default", region_id="the_glenross_arms")
    assert len(rows) == 1
    assert rows[0].entity_id == "cobwebs"
    assert rows[0].provenance == "yes_and_promoted"


# ---------------------------------------------------------------------------
# Unknown region / world — no silent fallback
# ---------------------------------------------------------------------------


async def test_unknown_region_returns_not_found(tmp_path: Path) -> None:
    """An unknown region MUST surface as NOT_FOUND. Silently treating it as
    an empty manifest would let player_initiated mode mint entities into a
    region that doesn't exist (CLAUDE.md: no silent fallbacks)."""
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="anything",
        region_id="nonexistent_region",
        mode="player_initiated",
        engagement_kind="mention",
    )
    result = await resolve_location_entity(args, ctx)
    assert result.status is ToolResultStatus.NOT_FOUND
    # Critically, no promotions written for the bogus region.
    assert (
        ctx.store.list_location_promotions(save_id="default", region_id="nonexistent_region") == []
    )


async def test_missing_genre_pack_returns_not_found(tmp_path: Path) -> None:
    """If ctx.genre_pack is None, the adapter must return NOT_FOUND rather
    than try to operate on an empty manifest. (Production wires ctx.genre_pack
    at session handler construction — a None here is a wiring bug, not a
    runtime branch.)"""
    store = SqliteStore(tmp_path / "save.db")
    ctx = ToolContext(
        world_id="glenross",
        session_id="s",
        perspective_pc=None,
        turn_number=1,
        store=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=None,
    )
    result = await resolve_location_entity(
        ResolveLocationEntityArgs(
            label="x",
            region_id="r",
            mode="player_initiated",
        ),
        ctx,
    )
    assert result.status is ToolResultStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# OTEL attribute seam (AC-9)
# ---------------------------------------------------------------------------


async def test_otel_attributes_on_resolved_match(tmp_path: Path) -> None:
    """OTEL attribute setting is the lie-detector seam. Story 54-8 wraps
    these into a dedicated location.* span; this story sets the attributes
    on whatever span the dispatcher provides via ctx.otel_span."""
    ctx = _build_ctx(tmp_path)
    args = ResolveLocationEntityArgs(
        label="the bar",
        region_id="the_glenross_arms",
        mode="narrator_proactive",
        engagement_kind="mention",
    )
    await resolve_location_entity(args, ctx)

    attrs = {c.args[0]: c.args[1] for c in cast(Any, ctx.otel_span).set_attribute.call_args_list}
    # AC-9 required attributes
    assert attrs["location.region_id"] == "the_glenross_arms"
    assert attrs["location.label"] == "the bar"
    assert attrs["location.mode"] == "narrator_proactive"
    assert attrs["location.engagement_kind"] == "mention"
    assert attrs["location.resolved"] is True
    assert attrs["location.mode_outcome"] == "matched"
    assert attrs["location.from_promotion"] is False
    # Entity-level attributes — present only when resolved.
    assert attrs["location.entity_id"] == "bar"
    assert attrs["location.entity_tier"] == "real_object"
    assert attrs["location.binding_kind"] == "location_feature"


async def test_otel_attributes_on_miss(tmp_path: Path) -> None:
    """On a narrator_proactive miss, the resolver/adapter must still set
    location.resolved=False so the GM panel can surface the contract
    violation."""
    ctx = _build_ctx(tmp_path)
    await resolve_location_entity(
        ResolveLocationEntityArgs(
            label="the dragon",
            region_id="the_glenross_arms",
            mode="narrator_proactive",
            engagement_kind="mechanical",
        ),
        ctx,
    )
    attrs = {c.args[0]: c.args[1] for c in cast(Any, ctx.otel_span).set_attribute.call_args_list}
    assert attrs["location.resolved"] is False
    assert attrs["location.mode_outcome"] == "no_match"
    # On a miss there is no resolved entity, so entity_* attributes are absent.
    assert "location.entity_id" not in attrs
    assert "location.entity_tier" not in attrs


async def test_otel_attributes_on_mint(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    await resolve_location_entity(
        ResolveLocationEntityArgs(
            label="the antique sextant",
            region_id="the_glenross_arms",
            mode="player_initiated",
            engagement_kind="mention",
        ),
        ctx,
    )
    attrs = {c.args[0]: c.args[1] for c in cast(Any, ctx.otel_span).set_attribute.call_args_list}
    assert attrs["location.resolved"] is True
    assert attrs["location.mode_outcome"] == "minted"
    assert attrs["location.from_promotion"] is True
    assert attrs["location.entity_tier"] == "yes_and"
