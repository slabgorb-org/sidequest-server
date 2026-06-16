"""Tool: veterans_luck — narrator-driven WWN Warrior Veteran's Luck activation.

This is the PRODUCTION CALLER that makes Veteran's Luck reachable in a real
game. It is a THIN wrapper — all rules (once-per-scene guard, status marking,
span emission) live in WwnRulesetModule.veterans_luck.

    narrator: veterans_luck(actor=Ruk, mode=force_hit)
                    |
                    v
    engine:   WwnRulesetModule.veterans_luck(core=ruk.core, mode="force_hit")
                    |
                    v
    status:   Scratch status appended (cleared at scene end)

Guards (fail loud — no silent fallbacks per CLAUDE.md):
- ``ctx.genre_pack.rules.ruleset != "wwn"`` → ValueError (tool is WWN-only)
- actor not found in snapshot → NOT_FOUND
- no active session → ERROR_FATAL

The OTEL span is emitted via wwn.veterans_luck (module-level); the handler
enriches it with per-tool ``tool.veterans_luck.*`` attributes.
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


class VeteransLuckArgs(BaseModel):
    actor: str = Field(
        ...,
        min_length=1,
        description="Name of the Warrior PC invoking Veteran's Luck.",
    )
    mode: Literal["force_hit", "force_miss"] = Field(
        ...,
        description=(
            "'force_hit': declare the Warrior's own attack a hit regardless of the roll. "
            "'force_miss': declare an incoming attack a miss regardless of the roll. "
            "Once per scene — a second call this scene returns applied=False."
        ),
    )


@tool(
    name="veterans_luck",
    description=(
        "Invoke WWN Warrior Veteran's Luck (once per scene Instant action). "
        "WWN-only tool — raises if the loaded pack is not ruleset 'wwn'. "
        "mode: 'force_hit' (declare your attack a hit) or 'force_miss' "
        "(declare an incoming attack a miss). "
        "Second call this scene returns applied=False so the narrator can "
        "describe the ability being spent."
    ),
    category=ToolCategory.WRITE,
    ruleset="wwn",
)
async def veterans_luck(args: VeteransLuckArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    pack = ctx.genre_pack
    if pack is None or pack.rules is None or pack.rules.ruleset != "wwn":
        ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
        raise ValueError(f"veterans_luck is wwn-only; loaded pack has ruleset={ruleset!r}")

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.actor)
    if core is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")

    module = get_ruleset_module(pack.rules.ruleset)
    assert isinstance(module, WwnRulesetModule), (
        f"expected WwnRulesetModule for slug 'wwn', got {type(module).__name__!r}"
    )

    result = module.veterans_luck(core, mode=args.mode)

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.veterans_luck.actor", args.actor)
    ctx.otel_span.set_attribute("tool.veterans_luck.mode", args.mode)
    ctx.otel_span.set_attribute("tool.veterans_luck.applied", result.applied)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "mode": args.mode,
            "applied": result.applied,
            "reason": result.reason,
        }
    )
