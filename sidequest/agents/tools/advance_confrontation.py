"""Tool: advance_confrontation — advance the player or opponent dial.

Phase C Task 21 — WRITE tool
----------------------------
Replaces the sidecar ``confrontation_advances`` field. The narrator
calls this during a structured confrontation (combat, chase, trial,
poker, debate) to advance one of the two side-routed dials.

ADR-033 status
~~~~~~~~~~~~~~
ADR-033 (Genre Mechanics Engine — Confrontations & Resource Pools) is
*partial* in the live codebase. No formal ``Confrontation`` class with
named axes exists yet. The closest live system is
:class:`~sidequest.game.encounter.StructuredEncounter`, which carries
two :class:`~sidequest.game.encounter.EncounterMetric` ascending dials:
``player_metric`` and ``opponent_metric``. Each has a ``current`` value
that advances toward a ``threshold``; the side that reaches threshold
first triggers resolution.

v1 mapping
~~~~~~~~~~
* ``axis: Literal["player", "opponent"]`` — which metric dial to
  advance. Other named axes (e.g. ``"stakes"``, ``"tension"``,
  ``"composure"``) are an ADR-033 forward-looking concept and not
  implemented here; passing anything else is rejected at the args
  model.
* ``delta: int`` — signed delta added to ``metric.current``. The engine
  does *not* clamp; values can grow past ``threshold`` (or below zero
  for negative deltas — useful for "regroup" beats). The narrator is
  responsible for sensible deltas; ``crossed_threshold`` in the result
  signals that a resolution beat is now due.
* ``confrontation_id: str`` — accepted forward-compat for the eventual
  multi-confrontation registry (ADR-033's ``ConfrontationDefinition``
  graph). v1 always targets ``snapshot.encounter``; the id is recorded
  in OTEL so the GM panel can show what the narrator *intended* to
  select even before the registry exists.
* ``reason: str`` — free-form one-line audit note for OTEL.

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.confrontation.id`` — forward-compat id; empty string by default.
* ``tool.confrontation.axis`` — ``"player"`` or ``"opponent"``.
* ``tool.confrontation.delta`` — signed delta the narrator passed in.
* ``tool.confrontation.value_after`` — ``metric.current`` after the
  mutation; lets the GM panel chart the dial in real time.
* ``tool.confrontation.reason`` — narrator's audit note (empty string
  when omitted).
* ``tool.confrontation.crossed_threshold`` — ``True`` iff this call
  pushed ``current`` from below ``threshold`` to at-or-above. A metric
  that was *already* past threshold and advances further is *not* a
  fresh crossing (the resolution beat already fired).

Concurrency
~~~~~~~~~~~
Sequential-per-session execution is provided by the Registry's
``_write_locks`` map — WRITE handlers don't need their own locking.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)


class AdvanceConfrontationArgs(BaseModel):
    model_config = {"extra": "forbid"}

    confrontation_id: str = Field(
        default="",
        description=(
            "Reserved for ADR-033 multi-confrontation support. v1 always "
            "advances the current snapshot.encounter; the arg is recorded "
            "in OTEL so the GM panel can audit the narrator's intent."
        ),
    )
    axis: Literal["player", "opponent"] = Field(
        ...,
        description=(
            "Which metric dial to advance: 'player' targets "
            "encounter.player_metric, 'opponent' targets opponent_metric."
        ),
    )
    delta: int = Field(
        ...,
        description=(
            "Signed delta to add to metric.current. The engine does not "
            "clamp; values can exceed threshold or go negative. Use "
            "crossed_threshold in the result to detect a resolution event."
        ),
    )
    reason: str = Field(
        default="",
        description="One-line narrator note for OTEL / GM-panel audit.",
    )


@tool(
    name="advance_confrontation",
    description=(
        "Advance a Confrontation Def axis by a delta. Use during "
        "structured confrontations (combat, chase, trial, poker, debate). "
        "v1 binds to the active StructuredEncounter's player_metric or "
        "opponent_metric dial; pass axis='player' or 'opponent'. Returns "
        "value_before/value_after and a crossed_threshold flag; fails "
        "fatally if no encounter is active."
    ),
    category=ToolCategory.WRITE,
)
async def advance_confrontation(args: AdvanceConfrontationArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    encounter = snapshot.encounter
    if encounter is None:
        return ToolResult.error(
            "no active encounter — cannot advance confrontation",
            recoverable=False,
        )

    metric = encounter.player_metric if args.axis == "player" else encounter.opponent_metric
    value_before = metric.current
    metric.current = value_before + args.delta
    value_after = metric.current

    ctx.store.save(snapshot)

    crossed_threshold = (value_before < metric.threshold) and (value_after >= metric.threshold)

    ctx.otel_span.set_attribute("tool.confrontation.id", args.confrontation_id)
    ctx.otel_span.set_attribute("tool.confrontation.axis", args.axis)
    ctx.otel_span.set_attribute("tool.confrontation.delta", args.delta)
    ctx.otel_span.set_attribute("tool.confrontation.value_after", value_after)
    ctx.otel_span.set_attribute("tool.confrontation.reason", args.reason)
    ctx.otel_span.set_attribute("tool.confrontation.crossed_threshold", crossed_threshold)

    return ToolResult.ok(
        {
            "confrontation_id": args.confrontation_id,
            "axis": args.axis,
            "delta": args.delta,
            "value_before": value_before,
            "value_after": value_after,
            "threshold": metric.threshold,
            "crossed_threshold": crossed_threshold,
            "metric_name": metric.name,
        }
    )
