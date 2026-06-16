"""Tool: query_npc — fetch an NPC, perception-coarsened by view.

Phase C Task 7 — second per-tool perception rule
------------------------------------------------
The handler returns the full NPC entry (filtered by the ``include_*``
flags); the perception layer
(``NarratorPerceptionFilter._rule_query_npc``, registered in
``sidequest/agents/narrator_perception_filter.py``) coarsens the
disposition before the SDK ever sees the bytes.

v1 semantics (forward-looking compromises)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* **Per-PC disposition is forward-looking.** ``Npc.disposition`` is a
  single global ``Disposition`` wrapper in v1; there is no per-PC
  observed-view storage. The plan's "Disposition is per-perspective"
  framing refers to the eventual shape; today the rule coarsens the
  global view to an ``attitude`` band whenever ``perspective_pc`` is set,
  which is the strictest interpretation of the plan's contract until
  ADR-020 grows real per-PC tracks.
* **"Backstory" aliases ``core.description``.** There is no dedicated
  backstory field on ``Npc``; ``core.description`` is the narrator-facing
  flavor text the narrator already authored. ``include_backstory=True``
  surfaces it under the ``backstory`` key (in addition to the always-
  returned ``description`` key) so the narrator's request shape matches
  the plan's contract. Future stories may add a richer backstory store
  (belief_state / scenario clues); this aliasing is the v1 surface.

Perception coarsening (rule, NOT this handler)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* ``perspective_pc is None``: omniscient (test/debug) — raw payload.
* ``perspective_pc is not None``: ``disposition_value`` (raw int) is
  dropped; the qualitative ``attitude`` band stays visible. Charm /
  deception layering on top of disposition is a future-story concern.

Handler-side OTEL
~~~~~~~~~~~~~~~~~
``tool.npc.perception_coarsened`` reflects whether the rule WILL drop a
field on this call — ``True`` when ``perspective_pc`` is set AND
``include_disposition=True`` (i.e., there's a ``disposition_value`` for
the rule to strip). The flag is intentionally precise: ``False`` for
``include_disposition=False`` calls so the GM panel can tell "rule
inactive because no sensitive field requested" apart from "rule
actively coarsening this view".
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
from sidequest.game.session import Npc


class QueryNpcArgs(BaseModel):
    npc_id: str = Field(
        ...,
        min_length=1,
        description="NPC display name (matched against Npc.core.name).",
    )
    include_disposition: bool = Field(
        default=True,
        description=(
            "When true, return the NPC's disposition. Non-omniscient views "
            "see only the qualitative attitude band (friendly / neutral / "
            "hostile); the raw integer score is dropped by the perception "
            "layer."
        ),
    )
    include_backstory: bool = Field(
        default=False,
        description=(
            "When true, include a ``backstory`` field. v1 aliases this to "
            "the NPC's ``core.description`` — there is no dedicated "
            "backstory store yet."
        ),
    )


def _build_full_payload(npc: Npc, args: QueryNpcArgs) -> dict[str, Any]:
    """Identity + always-present fields. Optional sections appended below."""
    payload: dict[str, Any] = {
        "npc_id": args.npc_id,  # echo so the rule can dispatch on it
        "name": npc.core.name,
        "description": npc.core.description,
        "personality": npc.core.personality,
        "pronouns": npc.pronouns,
        "appearance": npc.appearance,
        "age": npc.age,
        "build": npc.build,
        "height": npc.height,
        "distinguishing_features": list(npc.distinguishing_features),
        "location": npc.location,
        "last_seen_location": npc.last_seen_location,
        "last_seen_turn": npc.last_seen_turn,
        "creature_id": npc.creature_id,
        "threat_level": npc.threat_level,
        "abilities": list(npc.abilities) if npc.abilities else [],
        "morale": npc.morale,
    }
    if args.include_disposition:
        payload["disposition_value"] = npc.disposition.value
        payload["attitude"] = npc.disposition.attitude().value
    if args.include_backstory:
        # v1 alias — see module docstring.
        payload["backstory"] = npc.core.description
    return payload


@tool(
    name="query_npc",
    description=(
        "Fetch an NPC entry by id. Disposition is per-perspective: what "
        "THIS PC has observed of the NPC, not the omniscient view. Set "
        "include_disposition=False to skip the attitude/score section; "
        "include_backstory=True to surface flavor description as backstory."
    ),
    category=ToolCategory.READ,
)
async def query_npc(args: QueryNpcArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    npc: Npc | None = next(
        (n for n in snapshot.npcs if n.core.name == args.npc_id),
        None,
    )
    if npc is None:
        return ToolResult.not_found(f"unknown npc: {args.npc_id!r}")

    payload = _build_full_payload(npc, args)

    # OTEL — handler-side. ``perception_coarsened`` is True iff the rule
    # will actually drop a sensitive field: a perspective is set AND the
    # narrator requested the disposition section (otherwise there is no
    # disposition_value in the payload for the rule to strip).
    will_coarsen = ctx.perspective_pc is not None and args.include_disposition
    ctx.otel_span.set_attribute("tool.npc.id", args.npc_id)
    ctx.otel_span.set_attribute("tool.npc.name", npc.core.name)
    ctx.otel_span.set_attribute("tool.npc.perception_coarsened", will_coarsen)

    return ToolResult.ok(payload)
