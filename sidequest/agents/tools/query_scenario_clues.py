"""Tool: query_scenario_clues — discovered clue graph state.

Phase C Task 16 — read tool, no perception rule
-----------------------------------------------
Surfaces the scenario clue graph from the perspective PC's viewpoint:

* ``discovered`` — list of full :class:`ClueNode` projections (id, type,
  description, discovery_method, visibility, locations, implicates,
  requires, red_herring) for clues already in
  :attr:`ScenarioState.discovered_clues`.
* ``discovered_count`` / ``undiscovered_count`` — integer summaries the
  narrator can lean on without leaking undiscovered identifiers.
* ``undiscovered_titles`` — *GM-debug only*. ``None`` by narrator default;
  a list of ids (no description / no links) when
  ``include_undiscovered_titles=True``.
* ``scenario_active`` — flag for the "no scenario bound" case (returns
  empty arrays).
* ``scenario_resolved`` — propagated from :attr:`ScenarioState.resolved`
  for narration cues post-accusation.

ADR-053.

Perception rule
~~~~~~~~~~~~~~~
v1 perception is *handler-side*: undiscovered clues are hidden unless
``include_undiscovered_titles=True`` (and even then only the id surfaces).
No additional rule is registered with the perception filter. This keeps
the perception surface tight to the data: there's no "leak" path that
bypasses the flag, because the handler never emits the full node payload
for an undiscovered clue.

v1 ``discovered_clues`` is session-global (a single
:class:`set[str]` on :class:`ScenarioState`), not per-PC. When per-PC
clue discovery lands, this handler will narrow the discovered set to the
perspective PC's view, and the perception rule may be promoted to a
registered :class:`PerceptionFilter` entry.

OTEL attrs
~~~~~~~~~~
* ``tool.clue_graph.discovered_count`` — len(discovered list)
* ``tool.clue_graph.undiscovered_count`` — len(undiscovered ids)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)


class QueryScenarioCluesArgs(BaseModel):
    include_undiscovered_titles: bool = Field(
        default=False,
        description=(
            "GM-debug flag. When True, undiscovered clue ids are listed "
            "(ids only — no description, no links). Narrator should "
            "default to False; only the GM panel sets it to True."
        ),
    )


@tool(
    name="query_scenario_clues",
    description=(
        "Return the scenario clue graph state from the perspective PC's "
        "view: discovered clues (full payload) plus counts. Optional "
        "include_undiscovered_titles=true (GM debug only) exposes "
        "undiscovered clue ids; the narrator should default to false. "
        "Use to ground mystery narration in what has actually been "
        "uncovered."
    ),
    category=ToolCategory.READ,
)
async def query_scenario_clues(
    args: QueryScenarioCluesArgs,
    ctx: ToolContext,
) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    snapshot = session.snapshot
    ss = snapshot.scenario_state

    if ss is None:
        payload: dict[str, Any] = {
            "scenario_active": False,
            "discovered": [],
            "discovered_count": 0,
            "undiscovered_count": 0,
            # narrator default: titles hidden (None); GM flag: empty list
            "undiscovered_titles": [] if args.include_undiscovered_titles else None,
        }
        ctx.otel_span.set_attribute("tool.clue_graph.discovered_count", 0)
        ctx.otel_span.set_attribute("tool.clue_graph.undiscovered_count", 0)
        return ToolResult.ok(payload)

    discovered_clues: list[dict[str, Any]] = []
    undiscovered_titles: list[str] = []

    for node in ss.clue_graph.nodes:
        if node.id in ss.discovered_clues:
            discovered_clues.append(
                {
                    "id": node.id,
                    "type": node.clue_type,
                    "description": node.description,
                    "discovery_method": node.discovery_method,
                    "visibility": node.visibility,
                    "locations": list(node.locations),
                    "implicates": list(node.implicates),
                    "requires": list(node.requires),
                    "red_herring": node.red_herring,
                }
            )
        else:
            undiscovered_titles.append(node.id)

    payload = {
        "scenario_active": True,
        "scenario_resolved": ss.resolved,
        "discovered": discovered_clues,
        "discovered_count": len(discovered_clues),
        "undiscovered_count": len(undiscovered_titles),
        # narrator default exposes the count but hides the ids.
        "undiscovered_titles": (undiscovered_titles if args.include_undiscovered_titles else None),
    }

    ctx.otel_span.set_attribute("tool.clue_graph.discovered_count", len(discovered_clues))
    ctx.otel_span.set_attribute("tool.clue_graph.undiscovered_count", len(undiscovered_titles))

    return ToolResult.ok(payload)
