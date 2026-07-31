"""Tool: use_mutation — the PRODUCTION CALLER for AWN mutation use.

Thin wrapper: all rules (ownership, usage limits, Strain, save-vs) live
in sidequest.mutation.use_ops. Mirrors adjust_system_strain's shape —
capability-gated on the module type, never a slug string."""

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
from sidequest.game.ruleset.awn import AwnRulesetModule
from sidequest.mutation.use_ops import use_mutation as resolve_use_mutation


class UseMutationArgs(BaseModel):
    actor: str = Field(..., min_length=1, description="PC/NPC using the mutation.")
    mutation_id: str = Field(
        ...,
        min_length=1,
        description="Catalog id, e.g. 'structure/crushing_jaws'.",
    )
    target: str = Field(
        default="",
        description="Target name for save-vs mutations; empty for self/passive use.",
    )


@tool(
    name="use_mutation",
    description=(
        "Resolve an AWN mutation use mechanically: checks ownership and "
        "per-scene/per-day limits, pays the System Strain cost (refused if "
        "over max), and resolves the target save where the mutation has one. "
        "Returns applied=False with a reason on any refusal so the narrator "
        "describes the limit instead of improvising past it."
    ),
    category=ToolCategory.WRITE,
    ruleset="awn",
)
async def use_mutation(args: UseMutationArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    pack = ctx.genre_pack
    module = (
        get_ruleset_module(pack.rules.ruleset)
        if pack is not None and pack.rules is not None
        else None
    )
    if not isinstance(module, AwnRulesetModule):
        ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
        raise ValueError(
            f"use_mutation requires an AWN ruleset (awn); loaded pack has ruleset={ruleset!r}"
        )
    # pack cannot be None here: module is AwnRulesetModule only when pack was not
    # None and pack.rules was not None (see the ternary above).
    assert pack is not None
    if pack.mutations is None:
        raise ValueError("use_mutation called but the loaded pack has no mutations.yaml catalog")

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.actor)
    if core is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")
    if snapshot.mutation_state is None:
        return ToolResult.not_found(
            f"no mutation state on this session; was {args.actor!r} seeded at chargen?"
        )

    cfg = pack.rules.ruleset_config()

    def _save_resolver(stat: str, target: str) -> Literal["success", "fail"]:
        # v1: the narrator narrates the target's save from the returned
        # save_stat. Story 158-59 wired a real opposed save on the dice path
        # (narration_apply.py's _resolve_mutation_for_beat) but deliberately
        # NOT here: ``args.target`` is unvalidated free text with no seam
        # that resolves it to a CreatureCore/ability-score block, so there
        # is no defender to compute save_params against. Faking a save off
        # no stats would be the same homebrew-math failure ADR-143 forbids
        # (158-59 Delivery Finding: this tool needs its own
        # defender-resolution seam, tracked separately). Returning "fail"
        # applies the full effect.
        return "fail"

    result = resolve_use_mutation(
        state=snapshot.mutation_state,
        catalog=pack.mutations,
        module=module,
        cfg=cfg,
        core=core,
        actor=args.actor,
        mutation_id=args.mutation_id,
        target_id=args.target,
        save_resolver=_save_resolver,
    )

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.mutation.actor", args.actor)
    ctx.otel_span.set_attribute("tool.mutation.id", args.mutation_id)
    ctx.otel_span.set_attribute("tool.mutation.applied", result.applied)
    ctx.otel_span.set_attribute("tool.mutation.reason", result.reason)

    return ToolResult.ok(result.model_dump())
