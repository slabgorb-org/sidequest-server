"""Tool: stabilize_mortal_injury — narrator-driven WN lethality (wwn/cwn/awn) Mortal Injury stabilization.

This is the PRODUCTION CALLER that lets the narrator resolve a stabilization
attempt against a WN-core Mortal Injury (the Scar Status that
WithoutNumberRulesetModule.resolve_downed attaches to a 0-HP character — the
character dies at the end of ``mortal_injury_rounds`` unless stabilized).

    narrator: stabilize_mortal_injury(actor=Jax, skill=Heal, attribute=Reflex,
                                      rounds_elapsed=1, roll=18)
                    |
                    v
    Heal check:  roll  vs  difficulty = 8 + rounds_elapsed
                    |
       success ->  remove the Mortal Injury Status, append a "Frail" Wound
       failure ->  leave the Mortal Injury in place (the timer keeps running)

The WN rule (Without Number family, Sine Nomine, CC0): a Dex/Heal or Int/Heal
check vs ``8 + rounds_elapsed`` stabilizes a Mortal Injury. On success the
victim "Recovers at 1 HP + Frail" — the Mortal Injury (a Scar) clears and a
Frail Wound (clears with rest, per StatusSeverity.Wound) takes its place.

Guards (fail loud — no silent fallbacks per CLAUDE.md):
- bound module ``not isinstance(module, WithoutNumberRulesetModule)`` → ValueError (the tool
  requires a strain-bearing WN ruleset — wwn/cwn/awn — since the Mortal Injury /
  stabilize-at-0 rule is a WN-core lethality mechanic; SWN carries no mortal-injury
  surface)
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
from sidequest.game.ruleset.without_number import (
    WithoutNumberRulesetModule,
    is_dying_window_status,
)
from sidequest.game.status import Status, StatusSeverity
from sidequest.telemetry.spans.wn import (
    dying_window_resolved_span,
    dying_window_tick_span,
)

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
            "Rounds elapsed since the Mortal Injury was declared. The engine "
            "DERIVES this from the window's created_turn provenance and validates "
            "your value against it (a mismatch fails loud — the clock is not "
            "narrator-supplied). Difficulty = 8 + rounds_elapsed."
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
        "Resolve a stabilization attempt against a Without Number character's Mortal Injury. "
        "The strain-bearing WN rulesets (wwn/cwn/awn) — raises if the loaded pack is not one "
        "of them (Mortal Injury is WN-core lethality, ADR-142; SWN carries no mortal-injury "
        "surface). A Heal check (Dex/Heal or Int/Heal) vs difficulty 8 + rounds_elapsed. On "
        "success the Mortal Injury clears and the character downgrades to a 'Frail' Wound "
        "(recovers at 1 HP); on failure the Mortal Injury stays and the death timer "
        "keeps running."
    ),
    category=ToolCategory.WRITE,
    ruleset=("wwn", "cwn", "awn"),
)
async def stabilize_mortal_injury(args: StabilizeMortalInjuryArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    # Capability gate (not a slug string): the Mortal Injury / stabilize-at-0
    # rule is a Without Number lethality mechanic hoisted to the WN core
    # (ADR-142), so the tool serves any module that IS a
    # WithoutNumberRulesetModule — wwn/cwn/awn. Resolve the bound module and
    # check the capability rather than a slug string.
    pack = ctx.genre_pack
    module = (
        get_ruleset_module(pack.rules.ruleset)
        if pack is not None and pack.rules is not None
        else None
    )
    if not isinstance(module, WithoutNumberRulesetModule):
        ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
        raise ValueError(
            f"stabilize_mortal_injury requires a Without Number ruleset (wwn/cwn/awn); "
            f"loaded pack has ruleset={ruleset!r}"
        )

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.actor)
    if core is None:
        return ToolResult.not_found(f"unknown actor: {args.actor!r}")

    # Story 108-6: the clock is engine-owned, not a narrator guess (lie-detector,
    # No Silent Fallbacks). Find the live dying window and derive rounds_elapsed
    # from its created_turn provenance.
    window = next((s for s in core.statuses if is_dying_window_status(s)), None)
    if window is None:
        return ToolResult.error(
            f"{args.actor!r} carries no stabilizable dying window to stabilize",
            recoverable=False,
        )

    derived_rounds = max(0, snapshot.turn_manager.interaction - window.created_turn)
    if args.rounds_elapsed != derived_rounds:
        raise ValueError(
            "stabilize_mortal_injury rounds_elapsed mismatch: narrator supplied "
            f"{args.rounds_elapsed}, engine-derived {derived_rounds} "
            f"(created_turn={window.created_turn}, "
            f"current={snapshot.turn_manager.interaction}). "
            "Refusing to resolve on a fudged clock."
        )
    rounds_elapsed = derived_rounds
    difficulty = 8 + rounds_elapsed
    success = args.roll >= difficulty

    # Honest per-round tick: a stabilize attempt IS a stabilization (the gate
    # cannot know this, so it emits no tick — the tool owns the truthful one),
    # carrying the real roll and outcome for the GM panel.
    dying_window_tick_span(
        ruleset=module.slug,
        actor=args.actor,
        rounds_elapsed=rounds_elapsed,
        difficulty=difficulty,
        action_was_stabilization=True,
        roll=args.roll,
        success=success,
    )

    if success:
        # Clear the dying window and downgrade to a Frail Wound; recover at 1 HP.
        core.statuses = [s for s in core.statuses if not is_dying_window_status(s)]
        core.statuses.append(Status(text=_FRAIL_TEXT, severity=StatusSeverity.Wound))
        core.hp.apply_delta(1 - core.hp.current)
        dying_window_resolved_span(
            ruleset=module.slug,
            actor=args.actor,
            outcome="stabilized",
            final_rounds_elapsed=rounds_elapsed,
            resulting_status="Frail",
        )

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.stabilize.actor", args.actor)
    ctx.otel_span.set_attribute("tool.stabilize.skill", args.skill)
    ctx.otel_span.set_attribute("tool.stabilize.attribute", args.attribute)
    ctx.otel_span.set_attribute("tool.stabilize.rounds_elapsed", rounds_elapsed)
    ctx.otel_span.set_attribute("tool.stabilize.difficulty", difficulty)
    ctx.otel_span.set_attribute("tool.stabilize.roll", args.roll)
    ctx.otel_span.set_attribute("tool.stabilize.success", success)

    return ToolResult.ok(
        {
            "actor": args.actor,
            "skill": args.skill,
            "attribute": args.attribute,
            "rounds_elapsed": rounds_elapsed,
            "difficulty": difficulty,
            "roll": args.roll,
            "success": success,
            "outcome": _FRAIL_TEXT if success else "Mortal Injury persists",
        }
    )
