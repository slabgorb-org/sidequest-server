"""Tool: query_scene_state — current room / beat / tension / scenario.

Phase C Task 15 — read tool, no perception rule
-----------------------------------------------
Grounds narration in the current setting by surfacing four scene anchors:

* **room** — the perspective PC's :attr:`Character.current_room`, with a
  fallback chain (see "v1 room resolution" below).
* **beat** — :attr:`StructuredEncounter.beat` (0 when no encounter is
  active is unambiguous because ``encounter_active`` accompanies it).
* **tension** — :attr:`ScenarioState.tension` (0.0–1.0).
* **scenario** *(plan deviation, see below)* — minimal flags from
  :class:`ScenarioState` (``resolved``, ``discovered_clue_count``,
  ``guilty_npc``) that don't belong on the flat tension scalar.

Plan deviations
~~~~~~~~~~~~~~~
The plan listed three sections (room/beat/tension). This implementation
adds a fourth ``"scenario"`` section because :class:`ScenarioState` carries
useful narrator-facing surface beyond ``tension`` (the resolved flag and
the discovered-clue count) that doesn't fit on a single scalar. The three
plan sections remain the default ``include`` so the tool's "small,
predictable response" property is preserved.

v1 room resolution
~~~~~~~~~~~~~~~~~~
``GameSnapshot`` has no formal ``scene_id`` field. Rooms are per-actor on
:attr:`Character.current_room` and :attr:`Npc.current_room`. v1 derives a
single scene anchor from the PC roster with this fallback chain:

1. ``ctx.perspective_pc``'s :class:`Character` (matched by
   ``character.core.name``) → ``current_room`` if set.
2. Otherwise, scan ``snapshot.characters`` for the first one whose
   ``current_room`` is not ``None``.
3. Otherwise, ``None``.

This may evolve when room-graph navigation (ADR-055) provides a
session-level "active room" pointer; v1 sticks to the per-actor field
because that is where production state lives today.

Perception rule
~~~~~~~~~~~~~~~
None. v1 hides nothing — scene anchors are public-knowledge facts about
the setting and are returned raw. (Future: a per-PC fog-of-war system
could mask remote-scene tension; not v1.)

OTEL sentinel convention
~~~~~~~~~~~~~~~~~~~~~~~~
OTEL attribute values cannot be ``None``. When a field is missing from
the snapshot, the dispatch-span attribute carries a sentinel instead of
being omitted (omitting would force the GM panel to distinguish "field
absent" from "tool didn't run"). Sentinels:

* ``tool.scene.room_id`` — empty string (``""``) when no room resolves.
* ``tool.scene.beat`` — ``-1`` when no :class:`StructuredEncounter`.
* ``tool.scene.tension`` — ``-1.0`` when no :class:`ScenarioState`.

The JSON payload returned to the narrator still uses ``None`` for the
same cases; only the OTEL surface uses sentinels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot


_SceneSection = Literal["room", "beat", "tension", "scenario"]

# Sentinel values used for OTEL attribute writes when the underlying
# snapshot field is None. OTEL attribute values cannot themselves be
# None; see module docstring for the convention.
_ROOM_SENTINEL = ""
_BEAT_SENTINEL = -1
_TENSION_SENTINEL = -1.0


class QuerySceneStateArgs(BaseModel):
    include: list[_SceneSection] = Field(
        default_factory=lambda: ["room", "beat", "tension"],
        description=(
            "Sections to surface. 'room' = perspective-PC current_room "
            "(with fallback to the first PC that has a current_room). "
            "'beat' = StructuredEncounter.beat plus an encounter_active "
            "flag. 'tension' = ScenarioState.tension (0.0–1.0). "
            "'scenario' = minimal scenario flags (resolved, "
            "discovered_clue_count, guilty_npc). The default omits "
            "'scenario' to keep responses small."
        ),
    )


def _resolve_room(
    snapshot: GameSnapshot,
    perspective_pc: str | None,
) -> str | None:
    """v1 room resolution — see module docstring for the rule."""
    if perspective_pc is not None:
        pc = next(
            (c for c in snapshot.characters if c.core.name == perspective_pc),
            None,
        )
        if pc is not None and pc.current_room is not None:
            return pc.current_room
    for c in snapshot.characters:
        if c.current_room is not None:
            return c.current_room
    return None


@tool(
    name="query_scene_state",
    description=(
        "Fetch the current scene's room id, active beat (encounter or "
        "social), and tension level. Use to ground narration in the "
        "current setting. Optional 'scenario' section adds resolved/"
        "discovered_clue_count/guilty_npc when narrating mystery beats."
    ),
    category=ToolCategory.READ,
)
async def query_scene_state(
    args: QuerySceneStateArgs,
    ctx: ToolContext,
) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    snapshot = session.snapshot

    payload: dict[str, Any] = {}

    if "room" in args.include:
        payload["room_id"] = _resolve_room(snapshot, ctx.perspective_pc)

    if "beat" in args.include:
        encounter = snapshot.encounter
        payload["beat"] = encounter.beat if encounter is not None else None
        payload["encounter_active"] = encounter is not None

    if "tension" in args.include:
        state = snapshot.scenario_state
        payload["tension"] = state.tension if state is not None else None

    if "scenario" in args.include:
        state = snapshot.scenario_state
        if state is not None:
            # guilty_npc="" is the pre-pick sentinel in ScenarioState;
            # treat it as None at the surface so the narrator doesn't see
            # an empty-string id.
            payload["scenario"] = {
                "resolved": state.resolved,
                "discovered_clue_count": len(state.discovered_clues),
                "guilty_npc": state.guilty_npc or None,
            }
        else:
            payload["scenario"] = None

    payload["include"] = list(args.include)

    # OTEL — sentinel-encode None values so the GM panel can read every
    # attribute unconditionally. The JSON payload returned to the
    # narrator still uses None.
    if "room" in args.include:
        ctx.otel_span.set_attribute(
            "tool.scene.room_id",
            payload["room_id"] if payload["room_id"] is not None else _ROOM_SENTINEL,
        )
    if "beat" in args.include:
        ctx.otel_span.set_attribute(
            "tool.scene.beat",
            payload["beat"] if payload["beat"] is not None else _BEAT_SENTINEL,
        )
    if "tension" in args.include:
        ctx.otel_span.set_attribute(
            "tool.scene.tension",
            payload["tension"] if payload["tension"] is not None else _TENSION_SENTINEL,
        )

    return ToolResult.ok(payload)
