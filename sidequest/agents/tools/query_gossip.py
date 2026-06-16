"""Tool: query_gossip — enumerate told-by beliefs the PC could plausibly hear.

Phase C Task 12 — read tool, no perception rule (yet)
-----------------------------------------------------
ADR-053 (Scenario System) introduced per-NPC belief bubbles where each
:class:`~sidequest.game.belief_state.Belief` carries a tagged
:class:`~sidequest.game.belief_state.BeliefSource`. Beliefs whose source is
:class:`~sidequest.game.belief_state.BeliefSourceToldBy` are gossip-origin —
the NPC was told this by another named NPC. v1 of this tool enumerates those
beliefs across the NPCs the PC can plausibly hear and returns them in a flat
list.

v1 simplifications
~~~~~~~~~~~~~~~~~~
* **No "PC heard this" channel.** There is no global gossip log; gossip lives
  in the NPCs that heard it. So "what gossip could the PC have heard" reduces
  to "told-by beliefs held by NPCs in the PC's scene".
* **Scene resolution mirrors Task 8 (``list_npcs_in_scene``):**
  - Explicit ``scene_id`` matches ``Npc.location`` OR ``Npc.current_room``.
  - ``None`` + ``perspective_pc`` → derive from the PC's ``current_room``.
  - ``None`` + no PC (or PC absent / unroomed) → scan all NPCs.
* **No audibility / line-of-sight engine.** Scene-id matching is the v1
  audibility approximation. When LOS lands, add a perception rule here.
* **``limit`` caps result size.** Phase C tone — keep token budget tight.

Payload
~~~~~~~
Each item: ``{npc, subject, content, variant, turn_learned, told_by}``. The
``told_by`` value is the original speaker — whoever told the *holding* NPC.

OTEL
~~~~
* ``tool.gossip.item_count`` — number of items returned.
* ``tool.gossip.scene_id`` — only set when an effective scene was resolved.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.belief_state import BeliefSourceToldBy


class QueryGossipArgs(BaseModel):
    scene_id: str | None = Field(
        default=None,
        description=(
            "Match against npc.location OR npc.current_room. None = derive "
            "from perspective_pc.current_room; if absent, scan all NPCs."
        ),
    )
    since_turn: int | None = Field(
        default=None,
        ge=0,
        description=("Lower bound (inclusive) on belief.turn_learned. None = no recency filter."),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Cap result size.",
    )


def _resolve_scene_id(
    args: QueryGossipArgs,
    ctx: ToolContext,
    characters: list[Any],
) -> str | None:
    """Pick the effective scene id from the explicit arg or perspective PC."""
    if args.scene_id is not None:
        return args.scene_id
    if ctx.perspective_pc is None:
        return None
    pc = next(
        (c for c in characters if c.core.name == ctx.perspective_pc),
        None,
    )
    if pc is None:
        return None
    return pc.current_room


@tool(
    name="query_gossip",
    description=(
        "Fetch gossip the perspective PC could plausibly have heard. Filter by scene or recency."
    ),
    category=ToolCategory.READ,
)
async def query_gossip(args: QueryGossipArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    eff_scene = _resolve_scene_id(args, ctx, snapshot.characters)

    items: list[dict[str, Any]] = []
    for npc in snapshot.npcs:
        if eff_scene is not None and npc.location != eff_scene and npc.current_room != eff_scene:
            continue
        for belief in npc.belief_state.beliefs:
            source = belief.source
            if not isinstance(source, BeliefSourceToldBy):
                continue
            if args.since_turn is not None and belief.turn_learned < args.since_turn:
                continue
            items.append(
                {
                    "npc": npc.core.name,
                    "subject": belief.subject,
                    "content": belief.content,
                    "variant": belief.variant,
                    "turn_learned": belief.turn_learned,
                    "told_by": source.by,
                }
            )
            if len(items) >= args.limit:
                break
        if len(items) >= args.limit:
            break

    ctx.otel_span.set_attribute("tool.gossip.item_count", len(items))
    if eff_scene is not None:
        ctx.otel_span.set_attribute("tool.gossip.scene_id", eff_scene)

    return ToolResult.ok(
        {
            "items": items,
            "scene_id": eff_scene,
        }
    )
