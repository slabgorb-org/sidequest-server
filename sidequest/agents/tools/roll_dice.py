"""Tool: roll_dice — narrator-private dice rolls for behind-the-scenes checks.

Distinct from ADR-074's player-facing dice flow (which is browser-physics
rolled and returned as client-reported face values). This tool is for the
narrator's own saves, NPC stat checks, and background rolls that should not
interrupt the player with a 3D dice cup.

Notation parsing lives here on purpose: ``sidequest.game.dice`` is the
ADR-074 pure resolver and adding a notation parser there would muddy its
contract. Keep this adapter self-contained.
"""

from __future__ import annotations

import random
import re
from typing import Final

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)

_NOTATION_RE: Final = re.compile(r"^(?P<count>\d*)d(?P<sides>\d+)(?P<mod>[+-]\d+)?$")


class RollDiceArgs(BaseModel):
    notation: str = Field(..., description="Dice notation, e.g. 'd20', '3d6+2'.")
    seed: int | None = Field(default=None, description="Optional seed for reproducibility.")
    reason: str = Field(default="", description="One-line OTEL label for the roll.")


@tool(
    name="roll_dice",
    description=(
        "Roll dice for a mechanical resolution. Use whenever a check, save, "
        "or damage roll is needed. `notation` accepts standard dice notation; "
        "`reason` is a one-line label for the OTEL span."
    ),
    category=ToolCategory.GENERATE,
    # WN combat resolution belongs to run_wn_round on the player's DICE_THROW
    # (ADR-143/074); withheld from the narrator on a live WN combat so it cannot
    # grind a roll inside its own tool loop. sq-playtest 2026-06-22.
    combat_resolution=True,
)
async def roll_dice(args: RollDiceArgs, ctx: ToolContext) -> ToolResult:
    m = _NOTATION_RE.match(args.notation.strip())
    if m is None:
        return ToolResult.error(
            f"invalid notation: {args.notation!r}; expected e.g. 'd20', '3d6+2'"
        )
    count = int(m.group("count") or 1)
    sides = int(m.group("sides"))
    mod = int(m.group("mod") or 0)
    if count < 1 or count > 100 or sides < 2 or sides > 1000:
        return ToolResult.error(f"out-of-range dice spec: {args.notation!r}")

    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    rolls = [rng.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + mod

    ctx.otel_span.set_attribute("tool.dice.notation", args.notation)
    ctx.otel_span.set_attribute("tool.dice.value", total)
    # Session/world attribution: a private roll must be bindable to its
    # game in Jaeger without a temporal argument (story 50-24 AC-3).
    ctx.otel_span.set_attribute("tool.dice.session_id", ctx.session_id)
    ctx.otel_span.set_attribute("tool.dice.world_id", ctx.world_id)
    if args.seed is not None:
        ctx.otel_span.set_attribute("tool.dice.seed", args.seed)

    return ToolResult.ok(
        {
            "value": total,
            "rolls": rolls,
            "notation": args.notation,
            "seed": args.seed,
        }
    )
