"""Tool: update_npc_disposition — adjust an NPC's disposition score.

Phase C Task 9 — WRITE tool
---------------------------
``Npc.disposition`` is a single global ``Disposition`` wrapper in v1
(int in ``[-100, 100]``, clamped by the ctor; ADR-020 three-tier
attitude band derived via ``Disposition.attitude()``). There is no
multi-axis disposition surface (trust / fear / respect, etc.) and no
per-PC observed-view storage today.

Forward-compat args, v1 collapse
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Phase C plan calls for ``axis: str`` and ``perspective_pc: str |
None`` so the SDK schema already matches the eventual ADR-020 shape.
v1 **accepts these args, records them in OTEL, and ignores them
mechanically**: every call mutates the single global integer regardless
of ``axis`` label or ``perspective_pc`` selector. This lets the
narrator hold the future contract today; when ADR-020 grows real
multi-axis or per-PC tracks, the surface stays stable and only the
handler body changes.

``delta`` is declared as ``float`` to match the plan's signature, but
``Disposition`` is integer-valued — the handler casts to int via
``int(delta)`` before applying. The ``Disposition`` ctor clamps the
result to ``[-100, 100]``.

OTEL
~~~~
The Phase B Registry seeds the dispatch span
(``tool.write.update_npc_disposition``). This handler enriches it with
``tool.disposition.npc_id`` / ``axis`` / ``delta`` / ``perspective_pc``
so the GM panel can review the call. OTEL attribute values cannot be
``None``; ``perspective_pc=None`` is written as the empty string. The
attitude band transitions (e.g. NEUTRAL → FRIENDLY) are surfaced in the
result payload alongside the raw values for the narrator's reaction.

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
from sidequest.game.disposition import Disposition
from sidequest.game.session import Npc


class UpdateNpcDispositionArgs(BaseModel):
    npc_id: str = Field(
        ...,
        min_length=1,
        description="NPC display name (matched against Npc.core.name).",
    )
    delta: float = Field(
        ...,
        description=(
            "Signed delta in disposition units. Engine clamps to [-100, 100]. "
            "Coerced to int in v1 (Disposition is integer-valued)."
        ),
    )
    axis: str = Field(
        default="general",
        description=(
            "Multi-axis disposition is forward-looking (trust / fear / "
            "respect, ADR-020). v1 records the axis name in OTEL but "
            "mutates the single global disposition value regardless."
        ),
    )
    perspective_pc: str | None = Field(
        default=None,
        description=(
            "Per-PC disposition view is forward-looking. v1 always adjusts "
            "the global view; this arg is recorded in OTEL but otherwise "
            "ignored."
        ),
    )
    reason: str = Field(
        default="",
        description="One-line cause description; surfaces in the result payload for the narrator.",
    )


@tool(
    name="update_npc_disposition",
    description=(
        "Adjust an NPC's disposition along a named axis. Per-PC "
        "dispositions adjust the PC-specific view; omitting "
        "perspective_pc adjusts the global view."
    ),
    category=ToolCategory.WRITE,
)
async def update_npc_disposition(args: UpdateNpcDispositionArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    npc: Npc | None = next(
        (n for n in snapshot.npcs if n.core.name == args.npc_id),
        None,
    )
    if npc is None:
        return ToolResult.not_found(f"unknown npc: {args.npc_id!r}")

    before_value = npc.disposition.value
    before_attitude = npc.disposition.attitude().value
    # v1: Disposition is integer-valued — cast delta to int. Ctor clamps to [-100, 100].
    new_value = before_value + int(args.delta)
    npc.disposition = Disposition(new_value)
    after_value = npc.disposition.value
    after_attitude = npc.disposition.attitude().value

    ctx.store.save(snapshot)

    ctx.otel_span.set_attribute("tool.disposition.npc_id", args.npc_id)
    ctx.otel_span.set_attribute("tool.disposition.axis", args.axis)
    ctx.otel_span.set_attribute("tool.disposition.delta", float(args.delta))
    # OTEL attribute values can't be None — empty string when unset.
    ctx.otel_span.set_attribute("tool.disposition.perspective_pc", args.perspective_pc or "")

    return ToolResult.ok(
        {
            "npc_id": args.npc_id,
            "axis": args.axis,
            "delta": args.delta,
            "perspective_pc": args.perspective_pc,
            "reason": args.reason,
            "value_before": before_value,
            "value_after": after_value,
            "attitude_before": before_attitude,
            "attitude_after": after_attitude,
        }
    )
