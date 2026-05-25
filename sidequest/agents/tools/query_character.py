"""Tool: query_character — fetch a character sheet, perception-coarsened by view.

Phase C Task 6 — first per-tool perception rule
-----------------------------------------------
Replaces ad-hoc PC-sheet blocks in the narrator prompt with an on-demand
tool call. The handler returns the FULL sheet (filtered by ``include``);
the perception layer (``NarratorPerceptionFilter._rule_query_character``,
registered in ``sidequest/agents/narrator_perception_filter.py``)
coarsens for non-self views before the SDK ever sees the bytes.

Plan deviations
~~~~~~~~~~~~~~~
1. **``"resources"`` dropped from include.** The plan listed
   ``Literal["stats","inventory","status","backstory","resources"]``.
   Resources are session-scoped pools (ADR-033), not per-character —
   ``update_resource_pool`` (Task 5) owns that shape. Forwarding
   ``"resources"`` here would propagate scope confusion to the narrator.
   Final literal: ``["stats","inventory","status","backstory"]``.
2. **OTEL coarsened-flag is set by the handler, not the rule.** The
   rule runs inside the dispatch span (see ``Registry.dispatch``), but
   the handler knows ``ctx.perspective_pc`` directly and writes the
   ``tool.character.perception_coarsened`` attribute before returning,
   so the GM panel sees the value regardless of how the rule chain
   evolves.

Party model (v1)
~~~~~~~~~~~~~~~~
"Party" = ``snapshot.characters``. The session-local PC list IS the
party for our purposes. ADR-037 multiplayer per-player state will be
revisited in a later phase; v1 doesn't need an ``is_in_party`` flag.
NPCs are out of scope here — they belong to ``query_npc`` (Task 7).

Perception coarsening (rule, NOT this handler)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* ``perspective_pc is None`` or ``perspective_pc == character_id``:
  exact sheet, ``include``-filtered.
* ``perspective_pc != character_id`` (other party member): identity
  fields kept; ``stats``/``inventory``/``backstory`` dropped; ``status``
  kept (visibly applied effects are public-knowledge); ``edge_*`` keys
  dropped in favour of ``edge_band`` (unwounded / wounded / bloodied /
  staggering / down). See ADR-078 for the edge-band severity model.

Legacy
~~~~~~
Replaces the per-turn PC blocks in narrator prompt assembly and the
PerceptionRewriter post-hoc string scrub (ADR-028). The narrator now
asks for exactly what it needs and the perception layer is enforced at
the tool boundary, not via regex on rendered text.
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
from sidequest.game.character import Character

_IncludeSection = Literal["stats", "inventory", "status", "backstory"]


class QueryCharacterArgs(BaseModel):
    character_id: str = Field(
        ...,
        min_length=1,
        description="Character name (matched against Character.core.name).",
    )
    include: list[_IncludeSection] = Field(
        default_factory=lambda: ["stats", "status"],
        description=(
            "Sections to return. Request only what the current turn needs to "
            "keep the response slim. Identity fields (name, race, class, "
            "pronouns, is_friendly) are always present."
        ),
    )


def _serialize_status_list(character: Character) -> list[dict[str, Any]]:
    """Status rows shipped to the narrator — text + severity tier only.

    Internal absorption budget / created_turn metadata is omitted; the
    narrator can't act on it and it bloats the payload.
    """
    return [{"text": s.text, "severity": s.severity.value} for s in character.core.statuses]


def _build_full_payload(character: Character) -> dict[str, Any]:
    """Identity fields + edge numbers. Section filtering happens after."""
    return {
        "character_id": character.core.name,
        "name": character.core.name,
        "race": character.race,
        "char_class": character.char_class,
        "pronouns": character.pronouns,
        "is_friendly": character.is_friendly,
        "hp_current": character.core.hp.current,
        "hp_max": character.core.hp.max,
        "hp_fraction": character.hp_fraction(),
    }


@tool(
    name="query_character",
    description=(
        "Fetch a character sheet by id. `include` selects sections to "
        "return — request only what the current turn needs to keep the "
        "response slim. Available sections: stats, inventory, status, "
        "backstory. Identity fields (name, race, class) are always "
        "returned. Non-self views are coarsened by the perception layer: "
        "stats/inventory/backstory are dropped and HP is reduced to a "
        "severity band."
    ),
    category=ToolCategory.READ,
)
async def query_character(args: QueryCharacterArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    character: Character | None = next(
        (c for c in snapshot.characters if c.core.name == args.character_id),
        None,
    )
    if character is None:
        return ToolResult.not_found(f"unknown character: {args.character_id!r}")

    payload = _build_full_payload(character)

    # Section filtering — handler-side. ``include`` is the narrator's request;
    # the perception rule may further drop sections for non-self views.
    if "stats" in args.include:
        payload["stats"] = dict(character.stats)
    if "inventory" in args.include:
        payload["inventory"] = {
            "items": list(character.core.inventory.items),
            "gold": character.core.inventory.gold,
        }
    if "status" in args.include:
        payload["status"] = _serialize_status_list(character)
    if "backstory" in args.include:
        payload["backstory"] = character.backstory

    # OTEL — handler-side so the GM panel sees these even if the rule
    # chain changes shape later. ``perception_coarsened`` is predicted
    # from ctx.perspective_pc (the rule will coarsen when these match
    # the criteria the rule itself checks).
    will_coarsen = ctx.perspective_pc is not None and ctx.perspective_pc != args.character_id
    ctx.otel_span.set_attribute("tool.character.id", args.character_id)
    ctx.otel_span.set_attribute("tool.character.include", list(args.include))
    ctx.otel_span.set_attribute("tool.character.perception_coarsened", will_coarsen)

    return ToolResult.ok(payload)
