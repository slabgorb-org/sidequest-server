"""Tool: advance_encounter_beat — bump or set the encounter beat counter.

Phase C Task 19 — WRITE tool
----------------------------
Replaces the sidecar ``encounter_advances`` field. The narrator calls
this when a structured encounter beat resolves and the staging should
move forward (or when an explicit beat number is required, e.g. a
trope or scenario hook resets the encounter to a specific phase).

Deviation from plan
~~~~~~~~~~~~~~~~~~~
The plan's ``to_beat: str`` is a misread of the engine model.
:attr:`StructuredEncounter.beat` is an :class:`int` counter, not a
named identifier. v1 therefore takes ``to_beat: int | None``:

* ``None`` (default) — auto-advance by +1.
* explicit integer — set the beat directly.

Named beats (string ids tied to template-defined beat definitions)
are a forward-looking design and deferred to a later story. When that
arrives, the args model gains a sibling field; the integer surface
stays for engine-driven advances.

No ``advance_beat`` helper currently exists on
:class:`StructuredEncounter`; the tool mutates ``beat`` directly. If
beat advancement grows side effects (phase transitions, metric
recomputation), the logic moves to a method on the encounter model
and this tool becomes the thin caller — but for v1 the field
assignment is the entire mechanic.

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.encounter.beat_from`` — beat before the call.
* ``tool.encounter.beat_to`` — beat after the call.
* ``tool.encounter.reason`` — free-form narrator note (empty string
  when omitted) so the GM panel can audit why the beat moved.

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


class AdvanceEncounterBeatArgs(BaseModel):
    model_config = {"extra": "forbid"}

    to_beat: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Specific beat number to set; None auto-advances by +1. "
            "Beats are integer counters on StructuredEncounter; named "
            "beats are a forward-looking design."
        ),
    )
    reason: str = Field(
        default="",
        description="One-line narrator note for OTEL / GM-panel audit.",
    )


@tool(
    name="advance_encounter_beat",
    description=(
        "Transition the encounter to its next beat (or a specific beat). "
        "Beats are integer counters on the active StructuredEncounter — "
        "pass `to_beat` to set directly, or omit to auto-advance by +1. "
        "Returns the before/after beat numbers; fails fatally if no "
        "encounter is active."
    ),
    category=ToolCategory.WRITE,
)
async def advance_encounter_beat(args: AdvanceEncounterBeatArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    encounter = snapshot.encounter
    if encounter is None:
        return ToolResult.error(
            "no active encounter — cannot advance beat",
            recoverable=False,
        )

    beat_from = encounter.beat
    if args.to_beat is not None:
        encounter.beat = args.to_beat
    else:
        encounter.beat = beat_from + 1
    beat_to = encounter.beat

    ctx.store.save(snapshot)

    ctx.otel_span.set_attribute("tool.encounter.beat_from", beat_from)
    ctx.otel_span.set_attribute("tool.encounter.beat_to", beat_to)
    ctx.otel_span.set_attribute("tool.encounter.reason", args.reason)

    return ToolResult.ok(
        {
            "beat_from": beat_from,
            "beat_to": beat_to,
            "encounter_type": encounter.encounter_type,
            "reason": args.reason,
        }
    )
