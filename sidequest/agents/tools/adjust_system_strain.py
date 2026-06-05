"""Tool: adjust_system_strain — narrator-driven CWN System Strain changes.

This is the PRODUCTION CALLER that makes the strain engine reachable in a
real game. It is a THIN wrapper — all rules (gating, permanent floor, rest
recovery, first-aid cost) live in CwnRulesetModule.apply_system_strain.

    narrator: adjust_system_strain(actor=Jax, kind=temporary, amount=2, ...)
                    |
                    v
    engine:   CwnRulesetModule.apply_system_strain(core=jax.core, ...)
                    |
                    v
    pool:     SystemStrainPool.current updated in place

Guards (fail loud — no silent fallbacks per CLAUDE.md):
- bound module ``not isinstance(module, CwnRulesetModule)`` → ValueError (the tool
  requires a CWN-family ruleset — ``cwn`` or its ``awn`` subclass — since System
  Strain is a CwnRulesetModule mechanic)
- actor not found in snapshot → NOT_FOUND
- no active session → ERROR_FATAL

The OTEL span is emitted via the Phase B Registry dispatcher
(``tool.write.adjust_system_strain``); the handler enriches it with
per-tool ``tool.strain.*`` attributes the GM panel reads.

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
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.cwn import CwnRulesetModule


class AdjustSystemStrainArgs(BaseModel):
    actor: str = Field(
        ...,
        min_length=1,
        description="Name of the PC or NPC whose System Strain pool to adjust.",
    )
    kind: str = Field(
        ...,
        description=(
            "Type of strain change: 'temporary' (incidental stress), "
            "'permanent' (installed cyberware — positive installs, negative removes), "
            "'rest' (nightly recovery — amount = number of nights), "
            "'first_aid' (medkit application — amount ignored, cost from config)."
        ),
    )
    amount: int = Field(
        ...,
        description=(
            "Strain magnitude. For 'temporary'/'permanent': signed delta. "
            "For 'rest': number of nights (positive). "
            "For 'first_aid': ignored (cost read from pack config)."
        ),
    )
    source: str = Field(
        default="",
        description="One-line cause description; surfaces in OTEL for GM-panel review.",
    )


@tool(
    name="adjust_system_strain",
    description=(
        "Adjust a CWN character's System Strain pool. CWN-only tool — raises "
        "if the loaded pack is not ruleset 'cwn'. "
        "kind: 'temporary' (incidental stress), 'permanent' (cyberware install/remove), "
        "'rest' (nightly recovery), 'first_aid' (medkit cost from pack config). "
        "Over-max adds are refused (applied=False); the refusal reason is returned so "
        "the narrator can describe the limit being hit."
    ),
    category=ToolCategory.WRITE,
)
async def adjust_system_strain(args: AdjustSystemStrainArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    # Capability gate (not a slug string): System Strain is a CwnRulesetModule
    # mechanic, so the tool serves any module that IS a CwnRulesetModule — covers
    # cwn AND awn (AwnRulesetModule subclasses it). Resolve the bound module and
    # check the capability rather than `ruleset != "cwn"`, which silently
    # excluded awn.
    pack = ctx.genre_pack
    module = (
        get_ruleset_module(pack.rules.ruleset)
        if pack is not None and pack.rules is not None
        else None
    )
    if not isinstance(module, CwnRulesetModule):
        ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
        raise ValueError(
            f"adjust_system_strain requires a CWN-family ruleset (cwn/awn); "
            f"loaded pack has ruleset={ruleset!r}"
        )

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.actor)
    if core is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")

    cfg = pack.rules.ruleset_config()

    result = module.apply_system_strain(
        core=core,
        kind=args.kind,
        amount=args.amount,
        source=args.source,
        cfg=cfg,
    )

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.strain.actor", args.actor)
    ctx.otel_span.set_attribute("tool.strain.kind", args.kind)
    ctx.otel_span.set_attribute("tool.strain.amount", args.amount)
    ctx.otel_span.set_attribute("tool.strain.source", args.source)
    ctx.otel_span.set_attribute("tool.strain.applied", result.applied)
    ctx.otel_span.set_attribute("tool.strain.current_after", result.current)
    ctx.otel_span.set_attribute("tool.strain.delta", result.delta)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "kind": args.kind,
            "amount": args.amount,
            "source": args.source,
            "applied": result.applied,
            "current": result.current,
            "max": result.max,
            "permanent": result.permanent,
            "delta": result.delta,
            "reason": result.reason,
        }
    )
