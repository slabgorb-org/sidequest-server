"""Tool: set_stakes — set or append the session's current stakes.

Story 77-2 (ADR-137 Option B) — WRITE tool
------------------------------------------
The narrator-facing affordance to set/append ``GameSnapshot.active_stakes`` in
ordinary play, replacing the two off-path writers (the deprecated
``apply_world_patch`` escape hatch and the trope-resolution handshake that
never fires in a prose-only pack). Fires ``stakes.set`` so the GM panel can
verify the stakes substrate is engaged (the oz turn-13 failure was
``active_stakes: ""`` for the entire session).

Guardrail: reuses the existing ``_ACTIVE_STAKES_GUARDRAIL = 1024`` from
``narration_apply`` — NOT a new constant. The per-call input is bounded to the
guardrail at the args schema, and an append that pushes the field past the cap
is trimmed to the last 1024 chars so the freshly-written tail (the load-bearing
content for the next narrator turn) always survives.

``is_fresh`` is True when this call takes ``active_stakes`` from empty to
populated (establishment), False when it evolves already-present stakes.

Modelled on ``update_npc_disposition`` (load -> mutate -> save -> OTEL); the
Registry's per-session WRITE lock serialises concurrent calls.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.session import _ACTIVE_STAKES_GUARDRAIL
from sidequest.telemetry.spans import stakes_set_span


class SetStakesArgs(BaseModel):
    stakes: str = Field(
        ...,
        min_length=1,
        max_length=_ACTIVE_STAKES_GUARDRAIL,
        description=(
            "The current stakes — what is at risk right now. Replaces the "
            "existing stakes unless append=true."
        ),
    )
    append: bool = Field(
        default=False,
        description=(
            "When true, append to the existing stakes (newline-separated) "
            "instead of replacing them. Trimmed to the 1024-char guardrail."
        ),
    )


@tool(
    name="set_stakes",
    description=(
        "Set the session's current stakes (what is at risk now), or append to "
        "them with append=true. The stakes drive pacing and escalation — set "
        "them when the situation's tension meaningfully changes."
    ),
    category=ToolCategory.WRITE,
)
async def set_stakes(args: SetStakesArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    prior = snapshot.active_stakes
    is_fresh = not prior.strip()

    new_stakes = f"{prior}\n{args.stakes}" if args.append and prior else args.stakes

    # Reuse the existing guardrail; keep the freshly-written tail (the
    # load-bearing field for the next narrator), dropping oldest content.
    if len(new_stakes) > _ACTIVE_STAKES_GUARDRAIL:
        new_stakes = new_stakes[-_ACTIVE_STAKES_GUARDRAIL:]

    snapshot.active_stakes = new_stakes
    ctx.repository.save(snapshot)

    stakes_set_span(length=len(new_stakes), source="narrator", is_fresh=is_fresh)
    ctx.otel_span.set_attribute("tool.stakes.length", len(new_stakes))
    ctx.otel_span.set_attribute("tool.stakes.is_fresh", is_fresh)
    ctx.otel_span.set_attribute("tool.stakes.append", args.append)

    return ToolResult.ok(
        {
            "active_stakes": new_stakes,
            "length": len(new_stakes),
            "is_fresh": is_fresh,
            "appended": bool(args.append and prior),
        }
    )
