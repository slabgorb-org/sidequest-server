"""Fate narrator tool contract (ADR-144 F2b, Story 116-2).

ONE tool: ``propose_fate_compel``. The narrator offers the player a compel — a
complication rooted in one of their aspects, in exchange for a fate point. The tool is a
THIN wrapper (the ``wn_tools`` pattern): resolve the bound Fate module behind the ADR-117
seam and call its EXISTING ``offer_compel``, which fires the EXISTING ``fate.compel.offered``
span the GM panel reads. No economy change happens here — a compel is *proposed*; acceptance
(which earns the fate point) is the player's choice and lands with the F3 UI accept/refuse
round-trip.

Ruleset-gated to Fate packs via the ``@tool(ruleset="fate")`` declaration, so the
advertisement filter (``Registry.tool_definitions(ruleset=...)``) hides it from every
WN/native narrator. The per-tool fail-loud self-guard remains as a backstop (No Silent
Fallbacks per CLAUDE.md): no active session → ERROR_FATAL; non-Fate pack → loud ``ValueError``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateRulesetModule


class ProposeFateCompelArgs(BaseModel):
    actor: str = Field(..., description="The PC the compel targets.")
    aspect_text: str = Field(..., description="The aspect being compelled (verbatim).")
    compel_reason: str = Field(
        ..., description="The complication the compel introduces into the scene."
    )


@tool(
    name="propose_fate_compel",
    description=(
        "Propose a compel: offer the player a complication rooted in one of their aspects "
        "in exchange for a fate point. PROPOSE ONLY — the player chooses to accept or "
        "refuse; do not assume acceptance or spend their fate point yourself."
    ),
    category=ToolCategory.WRITE,
    ruleset="fate",
)
async def propose_fate_compel(args: ProposeFateCompelArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if not session:
        return ToolResult.error("no active session", recoverable=False)

    rules = getattr(ctx.genre_pack, "rules", None)
    declared = getattr(rules, "ruleset", None)
    if declared != "fate":
        raise ValueError(
            f"propose_fate_compel requires a Fate-bound pack; loaded pack has ruleset={declared!r}"
        )

    module = get_ruleset_module("fate")
    if not isinstance(module, FateRulesetModule):  # registry contract backstop (fail loud)
        raise ValueError(f"ruleset 'fate' resolved a non-Fate module: {type(module).__name__}")
    # offer_compel fires fate.compel.offered (no economy change). The GM panel sees the
    # offer even when the player declines.
    module.offer_compel(aspect_text=args.aspect_text, actor=args.actor)
    return ToolResult.ok(
        {"offered": args.aspect_text, "actor": args.actor, "reason": args.compel_reason}
    )
