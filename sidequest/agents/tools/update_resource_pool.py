"""Tool: update_resource_pool — adjust a session-scoped resource pool.

Plan deviation (Phase C Task 5)
-------------------------------
The Phase C plan specified an arg called ``target: str`` (an actor name).
Per ADR-033 (Genre Mechanics — Confrontations & Resource Pools), resource
pools are **session-scoped**, not per-actor: they live on
``GameSnapshot.resources: dict[str, ResourcePool]`` and are addressed by
pool name alone. Per-actor edge is its own thing — already covered by
``apply_damage`` (Task 3) via ``CreatureCore.apply_hp_delta``.

Forwarding ``target`` would propagate that misread through the SDK schema
and confuse the narrator about scope. So this adapter drops ``target``
and takes only ``pool`` / ``delta`` / ``source``. The OTEL attribute name
is renamed accordingly:

    plan: tool.resource.target  → dropped (no actor scope on session pools)

A signed delta (positive restores, negative spends) is forwarded to
``GameSnapshot.apply_resource_patch`` with ``ResourcePatchOp.Add`` —
``Add`` is the engine's signed-delta operation; the primitive clamps the
result to ``[min, max]`` and emits any threshold crossings.

Threshold crossings (e.g. sanity breaking down at 0.40, notice rising
through 0.75) are surfaced verbatim in the result payload so the
narrator can react to the ledger-bar transition in the same turn.

The OTEL span is emitted via the Phase B Registry dispatcher
(``tool.write.update_resource_pool``); this handler enriches it with the
per-tool ``tool.resource.*`` attributes the GM panel reads.

Sequential-per-session execution is provided by the Registry's
``_write_locks`` map — WRITE handlers don't need their own locking.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.resource_pool import (
    ResourcePatch,
    ResourcePatchOp,
    UnknownResource,
)


class UpdateResourcePoolArgs(BaseModel):
    pool: str = Field(
        ...,
        min_length=1,
        description=(
            "Session-scoped pool name (e.g. 'mana', 'sanity', 'notice'). "
            "Per-actor edge belongs to apply_damage; do not target it here."
        ),
    )
    delta: int = Field(
        ...,
        description=(
            "Signed delta. Negative spends, positive restores. The engine "
            "clamps the result to [min, max]."
        ),
    )
    source: str = Field(
        default="",
        description="One-line cause description; surfaces in OTEL for GM-panel review.",
    )


@tool(
    name="update_resource_pool",
    description=(
        "Adjust a session-scoped resource pool by a signed delta. "
        "Negative spends, positive restores. Pools are session-global "
        "(mana, sanity, notice, etc.) — not per-actor. Returns the pre/post "
        "values and any thresholds crossed by this change."
    ),
    category=ToolCategory.WRITE,
)
async def update_resource_pool(args: UpdateResourcePoolArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    patch = ResourcePatch(
        resource_name=args.pool,
        # ``Add`` is the engine's signed-delta op — value may be negative.
        operation=ResourcePatchOp.Add,
        value=float(args.delta),
    )
    try:
        result = snapshot.apply_resource_patch(patch)
    except UnknownResource:
        return ToolResult.not_found(f"unknown pool: {args.pool!r}")

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.resource.pool", args.pool)
    ctx.otel_span.set_attribute("tool.resource.delta", args.delta)
    ctx.otel_span.set_attribute("tool.resource.source", args.source)
    ctx.otel_span.set_attribute("tool.resource.value_after", result.new_value)

    return ToolResult.ok(
        {
            "pool": args.pool,
            "delta": args.delta,
            "source": args.source,
            "old_value": result.old_value,
            "new_value": result.new_value,
            "crossed_thresholds": [
                {
                    "at": t.at,
                    "event_id": t.event_id,
                    "direction": t.direction,
                }
                for t in result.crossed_thresholds
            ],
        }
    )
