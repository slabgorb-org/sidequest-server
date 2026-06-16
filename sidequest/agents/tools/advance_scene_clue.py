"""Tool: advance_scene_clue — mark a clue as discovered in the scenario graph.

Phase C Task 17 — ADR-053 (Scenario System)
-------------------------------------------
Wraps :meth:`ScenarioState.discover_clue` with the SDK tool contract.
Replaces the sidecar ``scenario_advances[]`` field on
``WorldStatePatch``: the narrator now declares clue advancement one
call at a time, with the DAG prerequisite check evaluated server-side
so prerequisite violations land as a recoverable tool error (narrator
can correct by advancing the required clues first).

Behaviour
~~~~~~~~~
1. Looks up ``clue_id`` in :attr:`ScenarioState.clue_graph` and runs
   :meth:`ScenarioState.discover_clue` — that helper raises
   :class:`PrerequisiteNotSatisfiedError` if any ``requires`` are not
   yet in :attr:`ScenarioState.discovered_clues`.
2. Distinguishes ``"discovered"`` vs ``"duplicate"`` based on whether
   the id was already in :attr:`ScenarioState.discovered_clues` *before*
   the call (the engine's own OTEL span carries ``duplicate=True`` but
   does not surface a "transition" enum — we compute it here for the
   GM-panel attr).
3. Persists the snapshot via ``ctx.store.save``.

evidence_text
~~~~~~~~~~~~~
The plan's args carry an ``evidence_text`` field — a free-form
narrator note describing how the clue was advanced. :class:`ScenarioState`
has no slot for it; v1 records it as an OTEL attribute only
(``tool.clue.evidence_text``). When per-clue evidence becomes a
persisted concept, the field will migrate to the engine.

Deviation from plan
~~~~~~~~~~~~~~~~~~~
The plan's example return payload omitted a ``discovered_count``. We
include it (mirrors :func:`commit_known_fact`'s ``fact_id`` /
:func:`query_scenario_clues`'s ``discovered_count``) so the narrator
can decide pacing without a follow-up read.

OTEL
~~~~
Per the plan:
    * ``tool.clue.id`` — the advanced clue id
    * ``tool.clue.transition`` — ``"discovered"`` | ``"duplicate"`` |
      ``"blocked_by_prerequisite"``
    * ``tool.clue.perspective_pc`` — perspective PC or empty string
    * ``tool.clue.evidence_text`` — v1 deviation; not persisted
      anywhere else (server-only attr on the dispatch span).

On the blocked path, ``tool.clue.missing_prerequisites`` is also set
(the ordered list :class:`PrerequisiteNotSatisfiedError` carries).
The engine's own span (``scenario.clue_prerequisite_violation``)
fires in parallel from :meth:`ScenarioState.discover_clue` for the
trace consumer.

Concurrency
~~~~~~~~~~~
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
from sidequest.game.scenario_state import PrerequisiteNotSatisfiedError


class AdvanceSceneClueArgs(BaseModel):
    clue_id: str = Field(
        ...,
        min_length=1,
        description="Clue id from ScenarioPack.clue_graph.nodes[*].id.",
    )
    evidence_text: str = Field(
        default="",
        description=(
            "Free-form narrator note describing how this clue was advanced. "
            "v1 records to OTEL only; not stored in scenario_state."
        ),
    )


@tool(
    name="advance_scene_clue",
    description=(
        "Mark a clue as advanced (discovered/connected) by the perspective "
        "PC. Records evidence text into the clue graph. Server enforces DAG "
        "prerequisites — a clue with unsatisfied 'requires' returns a "
        "recoverable error so the narrator can advance prerequisites first. "
        "Use after the PC physically uncovers, deduces, or has revealed to "
        "them a piece of the mystery."
    ),
    category=ToolCategory.WRITE,
)
async def advance_scene_clue(args: AdvanceSceneClueArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    ss = snapshot.scenario_state
    if ss is None:
        return ToolResult.error(
            "no active scenario_state — cannot advance clue",
            recoverable=False,
        )

    was_already_discovered = args.clue_id in ss.discovered_clues

    try:
        ss.discover_clue(args.clue_id)
    except PrerequisiteNotSatisfiedError as exc:
        ctx.otel_span.set_attribute("tool.clue.id", args.clue_id)
        ctx.otel_span.set_attribute("tool.clue.transition", "blocked_by_prerequisite")
        ctx.otel_span.set_attribute(
            "tool.clue.missing_prerequisites",
            list(exc.missing_prerequisites),
        )
        ctx.otel_span.set_attribute("tool.clue.perspective_pc", ctx.perspective_pc or "")
        ctx.otel_span.set_attribute("tool.clue.evidence_text", args.evidence_text)
        return ToolResult.error(
            f"clue {args.clue_id!r} requires {exc.missing_prerequisites!r} first",
            recoverable=True,
        )

    ctx.store.save(snapshot)

    transition = "duplicate" if was_already_discovered else "discovered"
    ctx.otel_span.set_attribute("tool.clue.id", args.clue_id)
    ctx.otel_span.set_attribute("tool.clue.transition", transition)
    ctx.otel_span.set_attribute("tool.clue.perspective_pc", ctx.perspective_pc or "")
    ctx.otel_span.set_attribute("tool.clue.evidence_text", args.evidence_text)

    return ToolResult.ok(
        {
            "clue_id": args.clue_id,
            "transition": transition,
            "discovered_count": len(ss.discovered_clues),
            "perspective_pc": ctx.perspective_pc,
        }
    )
