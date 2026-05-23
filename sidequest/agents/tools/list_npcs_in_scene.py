"""Tool: list_npcs_in_scene — id-only roster filtered by scene.

Phase C Task 8 — read tool, no perception rule (yet)
----------------------------------------------------
Returns the NPCs present in a scene as ``{npc_id, name}`` pairs only.
Callers fetch details via ``query_npc``. This keeps the per-call payload
small and routes any per-PC coarsening through ``query_npc``'s rule.

v1 simplifications
~~~~~~~~~~~~~~~~~~
* **No formal scene_id.** ``GameSnapshot`` has no ``current_scene``
  field; NPCs carry ``location`` (world-scoped string) and
  ``current_room`` (chassis interior). v1 treats ``scene_id`` as a
  string that matches either of those — the narrator already uses both
  fields fluidly in prose. A future story may introduce a typed Scene
  with id resolution; until then this matcher covers the live cases.
* **scene_id fallback.** When ``scene_id is None``, the handler looks
  up the perspective PC in ``snapshot.characters`` (by
  ``core.name == ctx.perspective_pc``) and uses that PC's
  ``current_room``. If the PC is absent or has no room (or no
  perspective is set at all), the handler returns the FULL roster —
  this is intentional: omniscient/debug callers get everything, and a
  PC who hasn't been placed yet shouldn't see an empty scene.
* **No line-of-sight engine.** The plan's "filter to NPCs the PC can
  perceive" rule is a no-op in v1: scene-id matching is already
  perspective-respecting once the PC's room is known, and no LOS /
  audibility system exists. Rather than register a pass-through rule
  for cosmetic completeness, this tool registers no rule — the
  registry defaults to pass-through for tools without one. When LOS
  lands, add a perception rule here.

OTEL
~~~~
Only ``tool.npcs.count`` is set per the plan spec.
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
from sidequest.game.npc_scene import is_npc_in_scene
from sidequest.game.session import Npc


class ListNpcsInSceneArgs(BaseModel):
    scene_id: str | None = Field(
        default=None,
        description=(
            "Room id (chassis interior) OR location string (world). When "
            "None, derive from the perspective PC's current_room; if the PC "
            "has no room (or no perspective is set), return all NPCs."
        ),
    )


def _resolve_scene_id(
    args: ListNpcsInSceneArgs,
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
    name="list_npcs_in_scene",
    description=(
        "List NPCs present in the current (or specified) scene. Returns ids "
        "+ display names only — call query_npc to fetch details."
    ),
    category=ToolCategory.READ,
)
async def list_npcs_in_scene(args: ListNpcsInSceneArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    eff = _resolve_scene_id(args, ctx, snapshot.characters)

    matched: list[Npc]
    if eff is None:
        # No scene context (no perspective_pc, or PC not in snapshot, or
        # PC has no current_room) — return the full roster. This is the
        # omniscient / debug-caller fallback documented in this module's
        # docstring; it intentionally bypasses ``is_npc_in_scene`` so a
        # PC who has not yet been placed sees everyone rather than an
        # empty scene.
        matched = list(snapshot.npcs)
    else:
        # Story 61-7 unification: delegate per-NPC scene membership to
        # ``sidequest.game.npc_scene.is_npc_in_scene`` so the snapshot
        # projection (``_apply_phase_c_projections``) and this tool reach
        # IDENTICAL verdicts on every NPC. Carries the previous
        # ``current_room or location`` structured-field union, adds the
        # ``last_seen_location`` prose fallback (when both structured
        # fields are unset), and propagates the unresolved-encounter
        # actor-membership branch (previously projection-only) to the
        # tool path.
        encounter = snapshot.encounter
        matched = [
            n
            for n in snapshot.npcs
            if is_npc_in_scene(n, current_room=eff, encounter=encounter)
        ]

    payload: dict[str, Any] = {
        "scene_id": eff,
        "npcs": [{"npc_id": n.core.name, "name": n.core.name} for n in matched],
    }

    ctx.otel_span.set_attribute("tool.npcs.count", len(matched))

    return ToolResult.ok(payload)
