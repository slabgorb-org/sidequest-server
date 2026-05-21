"""Tool: get_world_grounding — weather + demographics + calendar grounding.

Story 24-6 — read tool, no perception rule
-----------------------------------------
Narrator-callable grounding fetcher per **ADR-102** (native tool-use) and
**ADR-103** (OTEL via tool registry). Replaces the original 24-6 design
(always-on VALLEY-zone prompt injection) — the model now requests
grounding *on demand*, which saves prompt tokens and makes every grounded
narration beat observable as a span on the GM dashboard.

Three sections surface, selectable via ``include`` (default: all three):

* ``weather``       — current :class:`~sidequest.game.weather.WeatherState`
                      (story 24-5 output).
* ``demographics``  — per-pack/world demographics dict (story 24-3 YAML).
* ``calendar``      — per-pack/world calendar dict (story 24-4 YAML).

Data flow
~~~~~~~~~
The session handler loads each section at session bootstrap and stamps it
on every :class:`~sidequest.agents.tool_registry.ToolContext` it builds
(see the three ``ToolContext`` fields: ``weather_state``,
``world_demographics``, ``world_calendar``). This handler is a pure
reader — it never loads YAML or runs the generator. Story 24-7 added
two ``world_grounding.*`` state_transition spans here
(``weather_used`` and ``demographics_injected``) and a paired
``weather_proposed`` span at the generator seam in
:mod:`sidequest.game.weather` — together they feed the GM dashboard's
proposed-vs-used lie-detector view.

Missing-data behavior
~~~~~~~~~~~~~~~~~~~~~
Per **CLAUDE.md "No Silent Fallbacks"**: an absent section is **not**
papered over. When ``ctx.<field>`` is ``None`` (e.g. calendar.yaml has
not been authored yet because story 24-4 is backlog), the payload value
is explicitly ``None`` and the dispatch span carries
``tool.grounding.<section>_present = False``. The GM panel's
lie-detector view distinguishes "narrator skipped this section" from
"session didn't have the data plumbed in" — both are interesting
failure modes, neither is hidden.

Perception rule
~~~~~~~~~~~~~~~
None registered. Weather, demographics, and calendar are public-knowledge
genre-truth surface — no fog-of-war applies (the same call from any PC
returns the same data). Matches the ``query_scene_state`` precedent.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)

_GroundingSection = Literal["weather", "demographics", "calendar"]


class GetWorldGroundingArgs(BaseModel):
    include: list[_GroundingSection] = Field(
        default_factory=lambda: ["weather", "demographics", "calendar"],
        description=(
            "Sections to surface. 'weather' = current scene weather (zone, "
            "season, condition, temperature, precipitation, special_event). "
            "'demographics' = settlement profile + recurring cast (parish "
            "population, named NPCs, services). 'calendar' = current date "
            "in the world calendar (month, day, year, festivals). Default "
            "is all three. An absent section in the response (value is "
            "null) means the session has no data wired for that section — "
            "not a silent failure."
        ),
    )


@tool(
    name="get_world_grounding",
    description=(
        "Fetch current weather, settlement demographics, and calendar date "
        "for the active scene's world. Use when establishing scene location "
        "or time, naming a background NPC, or grounding sensory description "
        "(weather, season, hour). The data is genre-truth public knowledge — "
        "all PCs perceive it the same way. Call once when you need grounding; "
        "the data is stable across a scene."
    ),
    category=ToolCategory.READ,
)
async def get_world_grounding(
    args: GetWorldGroundingArgs,
    ctx: ToolContext,
) -> ToolResult:
    payload: dict[str, Any] = {"include": list(args.include)}

    weather_present = ctx.weather_state is not None
    demographics_present = ctx.world_demographics is not None
    calendar_present = ctx.world_calendar is not None

    if "weather" in args.include:
        payload["weather"] = (
            ctx.weather_state.model_dump() if ctx.weather_state is not None else None
        )

    if "demographics" in args.include:
        payload["demographics"] = ctx.world_demographics

    if "calendar" in args.include:
        payload["calendar"] = ctx.world_calendar

    # OTEL — section-presence booleans land on the dispatch span. The GM
    # panel uses these to distinguish "narrator skipped this section" from
    # "session didn't have the data plumbed in". Stamped unconditionally
    # (regardless of `include`) so the dashboard view is consistent across
    # calls — a section the narrator didn't ask for is still "present" in
    # the session sense.
    ctx.otel_span.set_attribute("tool.grounding.weather_present", weather_present)
    ctx.otel_span.set_attribute(
        "tool.grounding.demographics_present", demographics_present
    )
    ctx.otel_span.set_attribute("tool.grounding.calendar_present", calendar_present)

    # Story 24-7: world_grounding.* state_transition spans fire only when
    # data is actually returned to the narrator (requested AND wired).
    # These pair with world_grounding.weather_proposed (emitted from the
    # generator) and feed the GM panel's proposed-vs-used lie detector.
    if "weather" in args.include and ctx.weather_state is not None:
        from sidequest.telemetry.spans import emit_weather_used_span

        emit_weather_used_span(
            zone=ctx.weather_state.zone,
            season=ctx.weather_state.season,
            condition=ctx.weather_state.condition,
            seed=ctx.weather_state.seed,
            world_id=ctx.world_id,
            perspective_pc=ctx.perspective_pc,
        )

    if "demographics" in args.include and ctx.world_demographics is not None:
        from sidequest.telemetry.spans import emit_demographics_injected_span

        emit_demographics_injected_span(
            world_id=ctx.world_id,
            demographics=ctx.world_demographics,
            perspective_pc=ctx.perspective_pc,
        )

    return ToolResult.ok(payload)
