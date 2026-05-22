"""Tool: generate_encounter — combatant seed for genre + difficulty.

Phase C Task 26 — GENERATE tool
-------------------------------
``sidequest.cli.encountergen.encountergen`` exists and exposes
``generate_enemy(...)`` plus a ``main(argv)`` CLI entry, but its
internals require RNG seeding, filesystem paths, culture data, and
several other contextual inputs that the tool layer doesn't carry in
v1. Rather than smuggle those dependencies through the tool boundary
half-wired, this v1 follows the same pattern as ``generate_loadout``:

1. Reserves the namespace so the narrator's tool-use spec is stable
   across phases.
2. Lets the narrator's intent reach the GM panel — every encounter
   request is recorded with full OTEL attrs (``genre``, ``difficulty``,
   ``terrain``, ``theme``) and an unwired marker, so Keith can see what
   the narrator wanted even though the subsystem hasn't landed in the
   tool path.

The tool returns a fatal :class:`ToolResult` error (``recoverable=False``)
so the model receives a hard error and cannot confabulate phantom
combatants on an empty seed. OTEL intent attributes are still recorded
so the GM panel can see what the narrator requested. Phase E may wire
the real generator when the narrator path migrates and the RNG/paths
context can be threaded through cleanly.

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.encgen.genre`` — genre slug requested.
* ``tool.encgen.difficulty`` — difficulty tier (1-5).
* ``tool.encgen.terrain`` — terrain hint or empty string when omitted.
* ``tool.encgen.theme`` — theme hint or empty string when omitted.
* ``tool.encgen.combatant_count`` — always ``0`` while the subsystem
  is unwired.
* ``tool.encgen.encountergen_wired`` — bool; ``False`` until the
  ``encountergen`` subsystem is wired through the tool path.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)


class GenerateEncounterArgs(BaseModel):
    model_config = {"extra": "forbid"}

    genre: str = Field(
        ...,
        min_length=1,
        description=(
            "Genre slug (e.g. 'low_fantasy', 'neon_dystopia'). The "
            "encountergen subsystem keys its combatant tables off the "
            "active genre pack; the narrator names it explicitly so the "
            "tool request is self-contained in OTEL."
        ),
    )
    difficulty: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "Encounter difficulty tier (1=trivial, 5=deadly). Bounded "
            "1..5 to match the encountergen tier ladder."
        ),
    )
    terrain: str | None = Field(
        default=None,
        description=(
            "Optional terrain hint (e.g. 'cavern', 'rooftop', 'orbit'). "
            "Used by encountergen to bias combatant selection and "
            "suggested terrain features when wired."
        ),
    )
    theme: str | None = Field(
        default=None,
        description=(
            "Optional thematic hint (e.g. 'ambush', 'ritual', 'patrol'). "
            "Used by encountergen to bias narrative framing when wired."
        ),
    )


@tool(
    name="generate_encounter",
    description=(
        # Story 59-1: the confrontation_trigger guardrail was relocated OFF
        # this description onto begin_confrontation (the live engagement
        # writer). generate_encounter is an always-erroring stub — keying the
        # SDK narrator's start-confrontation criteria to a tool that can never
        # create an encounter mis-routed the call (the engagement-regression
        # root cause). This tool only ever seeds combatants, once wired.
        "Generate an encounter seed (combatant types + difficulty rating "
        "+ suggested terrain features). NOTE: this subsystem is not wired — "
        "it returns a hard error. To START a confrontation, call "
        "begin_confrontation."
    ),
    category=ToolCategory.GENERATE,
)
async def generate_encounter(args: GenerateEncounterArgs, ctx: ToolContext) -> ToolResult:
    # sidequest.cli.encountergen.encountergen exists with generate_enemy(...)
    # but its internals require RNG, paths, and culture data not threaded
    # through the tool boundary in v1. Record the narrator's intent in OTEL,
    # then return a fatal error so the narrator cannot confabulate phantom
    # combatants. Phase E may wire the real encountergen.
    ctx.otel_span.set_attribute("tool.encgen.genre", args.genre)
    ctx.otel_span.set_attribute("tool.encgen.difficulty", args.difficulty)
    ctx.otel_span.set_attribute("tool.encgen.terrain", args.terrain or "")
    ctx.otel_span.set_attribute("tool.encgen.theme", args.theme or "")
    ctx.otel_span.set_attribute("tool.encgen.combatant_count", 0)
    ctx.otel_span.set_attribute("tool.encgen.encountergen_wired", False)
    return ToolResult.error(
        "generate_encounter: this subsystem is NOT wired — encountergen CLI exists "
        "(sidequest/cli/encountergen/encountergen.py) but is not connected to the tool "
        "path in this phase. No encounter was generated; no combatants exist. "
        "Do not narrate combatants or combat as if they were generated by this tool.",
        recoverable=False,
    )
