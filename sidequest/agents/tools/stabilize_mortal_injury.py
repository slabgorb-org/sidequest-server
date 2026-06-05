"""Tool: stabilize_mortal_injury — narrator-driven CWN Mortal Injury stabilization.

This is the PRODUCTION CALLER that lets the narrator resolve a stabilization
attempt against a CWN Mortal Injury (the Scar Status that CwnRulesetModule.
resolve_downed attaches to a 0-HP character — the character dies at the end of
``mortal_injury_rounds`` unless stabilized).

    narrator: stabilize_mortal_injury(actor=Jax, skill=Heal, attribute=Reflex,
                                      rounds_elapsed=1, roll=18)
                    |
                    v
    Heal check:  roll  vs  difficulty = 8 + rounds_elapsed
                    |
       success ->  remove the Mortal Injury Status, append a "Frail" Wound
       failure ->  leave the Mortal Injury in place (the timer keeps running)

The CWN rule (Cities Without Number, Sine Nomine, CC0): a Dex/Heal or Int/Heal
check vs ``8 + rounds_elapsed`` stabilizes a Mortal Injury. On success the
victim "Recovers at 1 HP + Frail" — the Mortal Injury (a Scar) clears and a
Frail Wound (clears with rest, per StatusSeverity.Wound) takes its place.

Guards (fail loud — no silent fallbacks per CLAUDE.md):
- ``ctx.genre_pack.rules.ruleset != "cwn"`` → ValueError (tool is CWN-only)
- actor not found in snapshot → NOT_FOUND
- no active session → ERROR_FATAL

The OTEL span is emitted via the Phase B Registry dispatcher
(``tool.write.stabilize_mortal_injury``); this handler enriches it with
per-tool ``tool.stabilize.*`` attributes the GM panel reads — mirroring
adjust_system_strain's use of ``ctx.otel_span.set_attribute``.

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
from sidequest.game.status import Status, StatusSeverity

_MORTAL_INJURY_MARKER = "Mortal Injury"
_FRAIL_TEXT = "Frail — recovering at 1 HP"


class StabilizeMortalInjuryArgs(BaseModel):
    actor: str = Field(
        ...,
        min_length=1,
        description="Name of the PC or NPC carrying a Mortal Injury to stabilize.",
    )
    skill: str = Field(
        default="Heal",
        description="Skill used for the stabilization check (CWN: Heal).",
    )
    attribute: str = Field(
        default="Reflex",
        description="Attribute paired with the skill (CWN: Dex/Heal or Int/Heal).",
    )
    rounds_elapsed: int = Field(
        ...,
        ge=0,
        description=(
            "Rounds elapsed since the Mortal Injury was declared. The Heal check "
            "difficulty rises with time: difficulty = 8 + rounds_elapsed."
        ),
    )
    roll: int = Field(
        ...,
        description=(
            "The resolved Heal-check total (e.g. d20 face + modifiers). Success "
            "requires roll >= 8 + rounds_elapsed."
        ),
    )


@tool(
    name="stabilize_mortal_injury",
    description=(
        "Resolve a CWN stabilization attempt against a character's Mortal Injury. "
        "CWN-only tool — raises if the loaded pack is not ruleset 'cwn'. A Heal "
        "check (Dex/Heal or Int/Heal) vs difficulty 8 + rounds_elapsed. On success "
        "the Mortal Injury clears and the character downgrades to a 'Frail' Wound "
        "(recovers at 1 HP); on failure the Mortal Injury stays and the death timer "
        "keeps running."
    ),
    category=ToolCategory.WRITE,
)
async def stabilize_mortal_injury(args: StabilizeMortalInjuryArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    # Capability gate (not a slug string): the Mortal Injury / stabilize-at-0
    # rule is a CwnRulesetModule mechanic, so the tool serves any module that IS
    # a CwnRulesetModule — covers cwn AND awn (AwnRulesetModule subclasses it).
    # Resolve the bound module and check the capability rather than
    # `ruleset != "cwn"`, which silently excluded awn.
    pack = ctx.genre_pack
    module = (
        get_ruleset_module(pack.rules.ruleset)
        if pack is not None and pack.rules is not None
        else None
    )
    if not isinstance(module, CwnRulesetModule):
        ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
        raise ValueError(
            f"stabilize_mortal_injury requires a CWN-family ruleset (cwn/awn); "
            f"loaded pack has ruleset={ruleset!r}"
        )

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.actor)
    if core is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")

    difficulty = 8 + args.rounds_elapsed
    success = args.roll >= difficulty

    if success:
        # Clear the Mortal Injury Scar and downgrade to a Frail Wound.
        core.statuses = [s for s in core.statuses if _MORTAL_INJURY_MARKER not in s.text]
        core.statuses.append(Status(text=_FRAIL_TEXT, severity=StatusSeverity.Wound))

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.stabilize.actor", args.actor)
    ctx.otel_span.set_attribute("tool.stabilize.skill", args.skill)
    ctx.otel_span.set_attribute("tool.stabilize.attribute", args.attribute)
    ctx.otel_span.set_attribute("tool.stabilize.rounds_elapsed", args.rounds_elapsed)
    ctx.otel_span.set_attribute("tool.stabilize.difficulty", difficulty)
    ctx.otel_span.set_attribute("tool.stabilize.roll", args.roll)
    ctx.otel_span.set_attribute("tool.stabilize.success", success)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "skill": args.skill,
            "attribute": args.attribute,
            "rounds_elapsed": args.rounds_elapsed,
            "difficulty": difficulty,
            "roll": args.roll,
            "success": success,
            "outcome": _FRAIL_TEXT if success else "Mortal Injury persists",
        }
    )
