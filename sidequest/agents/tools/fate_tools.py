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
    actor: str = Field(..., min_length=1, max_length=64, description="The PC the compel targets.")
    aspect_text: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="The aspect being compelled (verbatim).",
    )
    compel_reason: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="The complication the compel introduces into the scene.",
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
    # offer even when the player declines — including the proposed complication (reason).
    #
    # ADR-144 F3e: PERSIST the offer onto the CANONICAL in-turn snapshot's active
    # conflict (the one the end-of-turn ``room.save`` writes — the advance_confrontation
    # discipline), NOT a fresh ``repository.load()`` copy that the save would clobber. The
    # persisted PendingCompel rides the next FATE_STATE projection to the player's
    # accept/refuse control. With no active conflict there is nowhere to attach (the F3e
    # surface is conflict-scoped) — offer_compel still fires the span, it just isn't
    # actionable. ``ctx.snapshot`` is the conflict-turn snapshot; on a non-conflict offer
    # it may be absent, which is the not-actionable path (no silent state loss — the offer
    # was never persistable to begin with).
    encounter = ctx.snapshot.encounter if ctx.snapshot is not None else None
    module.offer_compel(
        aspect_text=args.aspect_text,
        actor=args.actor,
        reason=args.compel_reason,
        encounter=encounter,
    )
    return ToolResult.ok(
        {"offered": args.aspect_text, "actor": args.actor, "reason": args.compel_reason}
    )
