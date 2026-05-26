"""Tool adapter: resolve_location_entity. ADR-109 §5.3 (Story 54-6).

Translates the narrator's tool call into the pure-Python resolver in
``sidequest.game.location_resolver``. The OTEL span attributes are the
lie-detector seam — Story 54-8 wires the full ``location.*`` span
definitions plus GM-panel routing; this story sets the attributes on
whatever span the dispatcher provides via ``ctx.otel_span``.

Two modes:

* ``narrator_proactive``: the narrator is the source of the entity name.
  Manifest miss returns ``NOT_FOUND`` so the narrator's pending
  mechanical action does not commit.
* ``player_initiated``: the player is the source. Manifest miss mints a
  new ``yes_and`` entity in the ``location_promotions`` table and
  returns ``OK``.

``flavor_only`` entities engaged with ``engagement_kind="mechanical"``
auto-promote to ``yes_and`` regardless of mode (Diamonds-and-Coal).

Unknown region / missing genre_pack: NOT_FOUND. Silent-fallback would
let player_initiated mode mint entities into a region that doesn't
exist (CLAUDE.md "No Silent Fallbacks").
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.location_resolver import resolve
from sidequest.protocol.models import LocationEntity


class ResolveLocationEntityArgs(BaseModel):
    label: str = Field(
        ...,
        min_length=1,
        description=(
            "The label as the narrator's prose or the player's input names "
            "it (e.g. 'the bar', 'the cracked telescope'). Case and leading "
            "articles ('the' / 'a' / 'an') are normalised internally."
        ),
    )
    region_id: str = Field(
        ...,
        min_length=1,
        description=(
            "The region or room id whose manifest to consult. Must match "
            "an authored region (cartography.yaml) or a materialised room."
        ),
    )
    mode: Literal["narrator_proactive", "player_initiated"] = Field(
        ...,
        description=(
            "narrator_proactive: prose claim. Manifest miss = no-commit "
            "(NOT_FOUND). player_initiated: player input. Manifest miss = "
            "mint a yes_and entity."
        ),
    )
    engagement_kind: Literal["mention", "mechanical"] = Field(
        default="mention",
        description=(
            "mention: descriptive only, no mutation. mechanical: about to "
            "damage / move / take / modify — flavor_only entities promote "
            "to yes_and on mechanical engagement."
        ),
    )


def _authored_entities_for(ctx: ToolContext, region_id: str) -> list[LocationEntity] | None:
    """Resolve the authored entity list for ``region_id`` from the
    ``GenrePack`` carried on the tool context.

    Returns ``None`` when any link in the lookup chain is missing — the
    adapter surfaces this as ``NOT_FOUND`` rather than treating it as an
    empty manifest (no silent fallback).
    """
    pack = ctx.genre_pack
    if pack is None:
        return None
    worlds = getattr(pack, "worlds", None)
    if worlds is None:
        return None
    world = worlds.get(ctx.world_id) if hasattr(worlds, "get") else None
    if world is None:
        return None
    cartography = getattr(world, "cartography", None)
    if cartography is None:
        return None
    regions = getattr(cartography, "regions", None)
    if regions is None:
        return None
    region = regions.get(region_id) if hasattr(regions, "get") else None
    if region is None:
        return None
    entities = getattr(region, "entities", None)
    if entities is None:
        return None
    return list(entities)


@tool(
    name="resolve_location_entity",
    description=(
        "Resolve a named entity against the region's location manifest. "
        "Call this BEFORE any mechanical claim against a described entity "
        "(damage, move, take, search) and on every player input that "
        "names something in the location. narrator_proactive miss is a "
        "contract violation — the pending mechanical action does not "
        "commit. player_initiated miss canonises the new entity (Yes-And). "
        "flavor_only entities promote to yes_and on mechanical engagement "
        "(Diamonds-and-Coal)."
    ),
    category=ToolCategory.WRITE,
)
async def resolve_location_entity(args: ResolveLocationEntityArgs, ctx: ToolContext) -> ToolResult:
    # Imported inside the function: keeps the resolver tool's import-time
    # surface minimal (the spans module pulls in opentelemetry) and matches
    # the pattern already used by _maybe_emit_location_overlay_changed in
    # the session handler.
    from sidequest.telemetry.spans import (
        location_entity_minted_span,
        location_entity_promoted_span,
        location_entity_resolve_span,
    )

    authored = _authored_entities_for(ctx, args.region_id)
    if authored is None:
        return ToolResult.not_found(
            f"region {args.region_id!r} not found in world {ctx.world_id!r} cartography"
        )

    resolution = resolve(
        store=ctx.repository,
        region_id=args.region_id,
        authored_entities=authored,
        label=args.label,
        mode=args.mode,
        engagement_kind=args.engagement_kind,
        turn_number=ctx.turn_number,
    )

    # 54-6 side-channel: keep the legacy ctx.otel_span attributes for
    # tool-dispatch introspection. Other write tools rely on the same
    # pattern; the dedicated spans (below) coexist for GM-panel routing.
    span = ctx.otel_span
    span.set_attribute("location.region_id", args.region_id)
    span.set_attribute("location.label", args.label)
    span.set_attribute("location.mode", args.mode)
    span.set_attribute("location.engagement_kind", args.engagement_kind)
    span.set_attribute("location.resolved", resolution.resolved)
    span.set_attribute("location.mode_outcome", resolution.mode_outcome)
    span.set_attribute("location.from_promotion", resolution.from_promotion)
    if resolution.entity is not None:
        span.set_attribute("location.entity_id", resolution.entity.id)
        span.set_attribute("location.entity_tier", resolution.entity.tier)
        if resolution.entity.binding is not None:
            span.set_attribute("location.binding_kind", resolution.entity.binding.kind)

    # 54-8: dedicated GM-panel span on every call. The route extractor
    # in telemetry/spans/location.py reads these attributes and sets the
    # explicit is_lie_detector boolean the GM panel keys on.
    entity_id = resolution.entity.id if resolution.entity is not None else None
    tier = resolution.entity.tier if resolution.entity is not None else None
    binding_kind = (
        resolution.entity.binding.kind
        if resolution.entity is not None and resolution.entity.binding is not None
        else None
    )
    with location_entity_resolve_span(
        region_id=args.region_id,
        label=args.label,
        mode=args.mode,
        engagement_kind=args.engagement_kind,
        resolved=resolution.resolved,
        mode_outcome=resolution.mode_outcome,
        from_promotion=resolution.from_promotion,
        entity_id=entity_id,
        tier=tier,
        binding_kind=binding_kind,
    ):
        pass

    # Mint / promotion side-effects each get their own dedicated span so
    # the GM panel can render them as positive-canon (blue) rows
    # independent of the resolve span's lie-detector signal.
    if resolution.mode_outcome == "minted" and resolution.entity is not None:
        with location_entity_minted_span(
            region_id=args.region_id,
            entity_id=resolution.entity.id,
            label=resolution.entity.label,
            canon=resolution.entity.promoted_canon or resolution.entity.label,
            turn=ctx.turn_number,
        ):
            pass
    elif resolution.mode_outcome == "promoted" and resolution.entity is not None:
        with location_entity_promoted_span(
            region_id=args.region_id,
            entity_id=resolution.entity.id,
            from_tier="flavor_only",
            to_tier=resolution.entity.tier,
            canon=resolution.entity.promoted_canon or resolution.entity.label,
            turn=ctx.turn_number,
        ):
            pass

    if not resolution.resolved:
        return ToolResult.not_found(
            f"no entity matching {args.label!r} in region {args.region_id!r} "
            "(narrator_proactive contract violation)"
        )

    return ToolResult.ok(resolution.model_dump(mode="json"))
