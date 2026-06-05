"""Tool: commit_effort — narrator-driven WWN Effort commitment.

This is the PRODUCTION CALLER that makes the Effort engine reachable in a
real game. It is a THIN wrapper — all rules (pool lookup, over-commit refusal,
commitment recording, span emission) live in WwnRulesetModule.commit_effort.

    narrator: commit_effort(actor=Kael, source=channeler, points=2, ...)
                    |
                    v
    engine:   WwnRulesetModule.commit_effort(core=kael.core, ...)
                    |
                    v
    pool:     EffortPool.commitments updated in place

Guards (fail loud — no silent fallbacks per CLAUDE.md):
- ``ctx.genre_pack.rules.ruleset != "wwn"`` → ValueError (tool is WWN-only)
- actor not found in snapshot → NOT_FOUND
- no active session → ERROR_FATAL
- unknown source pool → ValueError raised by the module (propagates)

The OTEL span is emitted via the Phase B Registry dispatcher
(``tool.write.commit_effort``); the handler enriches it with
per-tool ``tool.effort.*`` attributes the GM panel reads.
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
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.wwn import WwnRulesetModule


class CommitEffortArgs(BaseModel):
    actor: str = Field(
        ...,
        min_length=1,
        description="Name of the PC whose Effort pool to commit from.",
    )
    source: str = Field(
        ...,
        description=(
            "Class-source pool key (e.g. 'channeler', 'vowed', 'high_mage'). "
            "Each class uses its own pool — points from one source cannot fuel "
            "another. A missing pool raises immediately (fail loud)."
        ),
    )
    points: int = Field(
        default=1,
        ge=1,
        description="Number of Effort points to commit (default 1).",
    )
    duration: Literal["scene", "day", "maintained"] = Field(
        default="scene",
        description=(
            "Commitment duration: 'scene' (released at scene end automatically), "
            "'day' (released only on a long rest with comfortable=True), "
            "'maintained' (released by an explicit Instant action)."
        ),
    )
    label: str = Field(
        default="",
        description="The Art or power being fueled — for the GM panel and OTEL.",
    )


@tool(
    name="commit_effort",
    description=(
        "Commit WWN Effort from a class-source pool. WWN-only tool — raises "
        "if the loaded pack is not ruleset 'wwn'. "
        "source: the class pool key (e.g. 'channeler', 'vowed'). "
        "duration: 'scene' (auto-cleared at scene end), 'day' (released on long rest), "
        "'maintained' (released by Instant action). "
        "Over-commit is refused (applied=False); the refusal reason is returned so "
        "the narrator can describe the Effort limit being hit."
    ),
    category=ToolCategory.WRITE,
    ruleset="wwn",
)
async def commit_effort(args: CommitEffortArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    pack = ctx.genre_pack
    if pack is None or pack.rules is None or pack.rules.ruleset != "wwn":
        ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
        raise ValueError(f"commit_effort is wwn-only; loaded pack has ruleset={ruleset!r}")

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.actor)
    if core is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")

    module = get_ruleset_module(pack.rules.ruleset)
    assert isinstance(module, WwnRulesetModule), (
        f"expected WwnRulesetModule for slug 'wwn', got {type(module).__name__!r}"
    )

    # commit_effort raises ValueError for an unknown source pool — let it propagate.
    result = module.commit_effort(
        core=core,
        source=args.source,
        points=args.points,
        duration=args.duration,
        label=args.label,
    )

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.effort.actor", args.actor)
    ctx.otel_span.set_attribute("tool.effort.source", args.source)
    ctx.otel_span.set_attribute("tool.effort.points", args.points)
    ctx.otel_span.set_attribute("tool.effort.duration", args.duration)
    ctx.otel_span.set_attribute("tool.effort.label", args.label)
    ctx.otel_span.set_attribute("tool.effort.applied", result.applied)
    ctx.otel_span.set_attribute("tool.effort.available_after", result.available)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "source": args.source,
            "points": args.points,
            "duration": args.duration,
            "label": args.label,
            "applied": result.applied,
            "available": result.available,
            "max": result.max,
            "reason": result.reason,
        }
    )
