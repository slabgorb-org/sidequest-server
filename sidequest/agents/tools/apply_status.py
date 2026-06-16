"""Tool: apply_status — narrator-driven status condition → ADR-078 severity tier.

Plan deviation (Phase C Task 4)
-------------------------------
The Phase C plan specified an arg called ``duration_rounds: int | None``.
The engine does **not** track status duration in rounds. The real model
(``sidequest/game/status.py``, ADR-078) clears statuses by a severity
*tier*:

    Scratch  — clears at scene end (graze, lost composure, shake).
    Wound    — clears at session end or rest (real injury, notable shake).
    Scar     — persists until milestone or healing event (permanent mark).
    Boon     — temporary BENEFICIAL effect; clears at scene end with Scratch.

Forwarding the plan's ``duration_rounds`` would propagate a fiction
through the SDK schema and onto every future status-touching tool. So
this adapter takes ``severity`` instead, and the OTEL attribute names
are renamed to match the real model:

    plan: tool.status.name             → real: tool.status.text
    plan: tool.status.duration_rounds  → real: tool.status.severity

The narrator can ask for any free-text status; the recovery cadence
comes from the severity tier the narrator picks (or the ``Scratch``
default).

The OTEL span is emitted via the Phase B Registry dispatcher
(``tool.write.apply_status``); this handler enriches it with the
per-tool ``tool.status.*`` attributes the GM panel reads.

Sequential-per-session execution is provided by the Registry's
``_write_locks`` map — WRITE handlers don't need their own locking.
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
from sidequest.game.status import Status, StatusSeverity


class ApplyStatusArgs(BaseModel):
    target: str = Field(..., description="Name of character or NPC to affect.")
    text: str = Field(
        ...,
        min_length=1,
        description="The status text (e.g. 'prone', 'charmed', 'inspired').",
    )
    severity: Literal["Scratch", "Wound", "Scar", "Boon"] = Field(
        default="Scratch",
        description=(
            "Recovery cadence per ADR-078: Scratch clears at scene end; "
            "Wound clears at session end or rest; Scar persists until "
            "milestone; Boon is a temporary buff that clears at scene end."
        ),
    )
    source: str = Field(
        default="",
        description="One-line cause description.",
    )


@tool(
    name="apply_status",
    description=(
        "Apply a status condition (prone, dazed, charmed, inspired, etc.) "
        "to a character or NPC. `severity` selects the recovery cadence: "
        "Scratch (clears at scene end), Wound (clears at session end/rest), "
        "Scar (permanent until milestone), Boon (temporary buff, clears at "
        "scene end). Per ADR-078, duration is severity-tiered, not "
        "round-counted."
    ),
    category=ToolCategory.WRITE,
)
async def apply_status(args: ApplyStatusArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.target)
    if core is None:
        return ToolResult.not_found(f"unknown target: {args.target!r}")

    # The Literal value is the StatusSeverity enum value verbatim;
    # constructing the enum here keeps the args model decoupled from the
    # engine enum (Pydantic schema stays a flat string enum the SDK can
    # present cleanly).
    severity = StatusSeverity(args.severity)
    status = Status(
        text=args.text,
        severity=severity,
        absorbed_shifts=0,
        created_turn=ctx.turn_number,
        # The narrator tool doesn't know the encounter id; the encounter
        # engine sets it when statuses arise from that path.
        created_in_encounter=None,
    )
    core.statuses.append(status)
    ctx.store.save(snapshot)

    ctx.otel_span.set_attribute("tool.status.target", args.target)
    ctx.otel_span.set_attribute("tool.status.text", args.text)
    ctx.otel_span.set_attribute("tool.status.severity", args.severity)
    ctx.otel_span.set_attribute("tool.status.source", args.source)

    return ToolResult.ok(
        {
            "target": args.target,
            "text": args.text,
            "severity": args.severity,
            "source": args.source,
            "active_statuses": [s.text for s in core.statuses],
        }
    )
