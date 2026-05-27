"""Tool: query_encounter — initiative order + current encounter beat.

Phase C Task 18 — read tool, handler-side perception coarsening
---------------------------------------------------------------
Returns the active :class:`StructuredEncounter` summary so the narrator
can stage a beat without reaching into ``snapshot.encounter`` directly:

* Encounter shell — ``encounter_type``, ``beat``, ``structured_phase``,
  ``outcome``, ``resolved``.
* Both metric dials — ``player_metric`` and ``opponent_metric`` with
  ``name``/``current``/``threshold``.
* Combatant roster — list of actor entries with ``name``, ``role``,
  ``side``, ``withdrawn``, and a perception-coarsened edge surface.

Combatant detail (HP / inventory / stats) is intentionally *not*
returned here — the narrator chases up via :func:`query_character` (PCs)
or :func:`lookup_monster` (canonical monster blocks) so each tool keeps
its perception contract clean.

Perception (handler-side, not a registered rule)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Foe HP is coarsened to an ADR-078 severity band
(``unwounded`` / ``wounded`` / ``bloodied`` / ``staggering`` / ``down``)
via the same boundary table used by :func:`query_character`. This is
done in the handler — *no* :func:`register_rule` call — because:

1. The coarsening uses information beyond the payload itself
   (``actor.side`` plus a cross-snapshot lookup), which the rule
   signature ``(payload, perspective_pc)`` doesn't carry.
2. Foe-HP redaction has no "self / no-perspective ⇒ exact" escape
   hatch the way :func:`query_character` does — even the perspective
   PC's narrator should not see opponent HP as a raw number.

Players and neutrals surface raw edge ``current``/``max`` so the
narrator can pace decisively when an ally is about to drop. This
mirrors Task 6's "self / exact" rule extended to the whole party.

Cross-tool reuse — ``_hp_band``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Imports :func:`sidequest.agents.narrator_perception_filter._hp_band`
directly. The underscore is a Python convention, not an enforcement
boundary; reusing it keeps the severity-band boundaries single-sourced
(Task 6 owns the canonical definition). If a third caller appears, the
function moves to a shared helper module — for now, two callers is
under the rule-of-three.

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.encounter.id`` — ``encounter_type`` (the closest thing
  :class:`StructuredEncounter` has to a stable identifier; the plan
  calls this ``id`` and the GM panel reads ``encounter_type``).
* ``tool.encounter.beat`` — current beat, or ``-1`` when no encounter
  is active. Matches the sentinel convention in
  :mod:`query_scene_state`.
* ``tool.encounter.combatant_count`` — ``len(encounter.actors)``, or
  ``0`` when no encounter.

Sidecar / state
~~~~~~~~~~~~~~~
Read-only — no sidecar row, no patches.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sidequest.agents.narrator_perception_filter import _hp_band
from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)


class QueryEncounterArgs(BaseModel):
    """No arguments — returns the full encounter snapshot in one call.

    A future version may add include flags (e.g. drop ``actors`` to keep
    the payload tiny when only the metric dials matter); v1 returns the
    full shape because every narrator turn that asks needs all of it.
    """

    model_config = {"extra": "forbid"}


@tool(
    name="query_encounter",
    description=(
        "Fetch initiative order and current encounter beat. Returns "
        "combatant ids + names; call `query_character` / "
        "`lookup_monster` for details. Foe HP is coarsened to "
        "unwounded/wounded/bloodied/staggering/down — player and "
        "neutral edge come back raw."
    ),
    category=ToolCategory.READ,
)
async def query_encounter(args: QueryEncounterArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    snapshot = session.snapshot
    encounter = snapshot.encounter

    if encounter is None:
        # Sentinel attributes so the GM panel can read every attribute
        # unconditionally (matches query_scene_state's convention).
        ctx.otel_span.set_attribute("tool.encounter.id", "")
        ctx.otel_span.set_attribute("tool.encounter.beat", -1)
        ctx.otel_span.set_attribute("tool.encounter.combatant_count", 0)
        return ToolResult.ok({"encounter_active": False})

    actors_payload: list[dict[str, Any]] = []
    for actor in encounter.actors:
        # ``side`` is a Literal in the model (resolves to str at runtime);
        # be defensive in case a future migration switches to StrEnum.
        side_value = actor.side.value if hasattr(actor.side, "value") else str(actor.side)
        entry: dict[str, Any] = {
            "name": actor.name,
            "role": actor.role,
            "side": side_value,
            "withdrawn": actor.withdrawn,
        }
        core = snapshot.find_creature_core(actor.name)
        if side_value == "opponent":
            # Foes: severity band only. No raw edge under any
            # perspective — even the perspective PC's narrator shouldn't
            # see opponent HP as a number.
            if core is not None and core.hp.max > 0:
                fraction = core.hp.current / core.hp.max
                entry["hp_band"] = _hp_band(fraction)
            else:
                # Either no matching creature in the snapshot or a
                # degenerate hp.max=0 (would zero-div). "unknown" is
                # the safe sentinel for the narrator.
                entry["hp_band"] = "unknown"
        else:
            # players + neutrals — surface raw current/max so the
            # narrator can pace party-side decisions decisively.
            if core is not None:
                entry["hp_current"] = core.hp.current
                entry["hp_max"] = core.hp.max
        actors_payload.append(entry)

    structured_phase: str | None = (
        encounter.structured_phase.value if encounter.structured_phase is not None else None
    )

    payload: dict[str, Any] = {
        "encounter_active": True,
        "encounter_type": encounter.encounter_type,
        "beat": encounter.beat,
        "structured_phase": structured_phase,
        "outcome": encounter.outcome,
        "resolved": encounter.resolved,
        "player_metric": {
            "name": encounter.player_metric.name,
            "current": encounter.player_metric.current,
            "threshold": encounter.player_metric.threshold,
        },
        "opponent_metric": {
            "name": encounter.opponent_metric.name,
            "current": encounter.opponent_metric.current,
            "threshold": encounter.opponent_metric.threshold,
        },
        "actors": actors_payload,
    }

    ctx.otel_span.set_attribute("tool.encounter.id", encounter.encounter_type)
    ctx.otel_span.set_attribute("tool.encounter.beat", encounter.beat)
    ctx.otel_span.set_attribute("tool.encounter.combatant_count", len(encounter.actors))

    return ToolResult.ok(payload)
