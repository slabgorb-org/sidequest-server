"""Tool: query_magic_state — fetch a character's magic state, self-exact / others-coarsened.

Phase C Task 22 — read tool over ``GameSnapshot.magic_state``.

Surface
~~~~~~~
Per-character slice of :class:`sidequest.magic.state.MagicState`:

* character-scoped ledger bars (value + max from spec.range[1])
* known spells (full list on self, count only otherwise)
* prepared spells, indexed by spell level
* spent spells (cast since last rest)
* control tier (innate-tier counter, ADR per plan §5.4)
* active workings affecting this actor — ``working_log`` entries where
  ``WorkingRecord.actor`` matches ``character_id``

Perception: handler-side, not a registered rule
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unlike :mod:`query_character` (Task 6), this tool enforces perception
coarsening *directly inside the handler*, with no entry in the
``NarratorPerceptionFilter._RULES`` table. Rationale: the v1 surface
(bars + spell lists + control tier + working count) is small enough that
the self / non-self payload shapes diverge dramatically (full bars
versus counts only), and threading a registered rule would have to
re-derive ``character_id`` from the payload. Handler-side keeps the
shape switch local. The handler still writes ``tool.magic.*`` OTEL
attributes so the GM panel sees the perception decision.

* ``perspective_pc is None`` → treated as omniscient / test path → self payload.
* ``perspective_pc == character_id`` → self payload (full).
* otherwise → coarsened payload (counts only, no bar values, no spell ids).

WorkingRecord matching
~~~~~~~~~~~~~~~~~~~~~~
``WorkingRecord`` (see ``sidequest/magic/state.py``) carries an ``actor``
field — the caster — but no ``target`` / ``source`` columns. Working
entries that *target* a non-caster currently fold into the actor
record's flavor / narrator_basis prose, not a structured field. So the
v1 ``active_working_count`` is "workings I cast" rather than "workings
affecting me." A later story can extend ``WorkingRecord`` with a
``targets: list[str]`` column and broaden the filter; the OTEL
attribute keeps the same name.

No-magic-state worlds
~~~~~~~~~~~~~~~~~~~~~
``snapshot.magic_state`` is ``None`` for saves predating the magic
system or for worlds without a magic config (see
``GameSnapshot.magic_state`` comment, ``sidequest/game/session.py``).
The handler returns ``magic_state_present=False`` with an empty payload
and records ``tool.magic.magic_state_present=False`` so the GM panel
can distinguish "no magic in this world" from "character has empty
state". Sebastien's lie-detector wants this separable.
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
from sidequest.magic.state import _deserialize_bar_key


class QueryMagicStateArgs(BaseModel):
    character_id: str = Field(
        ...,
        min_length=1,
        description="Character name to query.",
    )


@tool(
    name="query_magic_state",
    description=(
        "Fetch the character's active spells, prepared spells (if "
        "applicable), and mana pool state. Self queries return the full "
        "ledger (bars, known/prepared/spent spell lists, control tier); "
        "queries from another PC's perspective return counts only and "
        "hide mana bar values. Returns magic_state_present=false on worlds "
        "without a magic config."
    ),
    category=ToolCategory.READ,
)
async def query_magic_state(args: QueryMagicStateArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    ms = snapshot.magic_state

    if ms is None:
        ctx.otel_span.set_attribute("tool.magic.character_id", args.character_id)
        ctx.otel_span.set_attribute("tool.magic.active_spell_count", 0)
        ctx.otel_span.set_attribute("tool.magic.magic_state_present", False)
        ctx.otel_span.set_attribute("tool.magic.mana_remaining", -1.0)
        return ToolResult.ok(
            {
                "character_id": args.character_id,
                "magic_state_present": False,
            }
        )

    is_self = (ctx.perspective_pc is None) or (ctx.perspective_pc == args.character_id)

    # Character-scoped bars for this actor. Keys are serialized
    # ``"<scope>|<owner_id>|<bar_id>"`` strings; deserialize and filter
    # by scope+owner. Skip malformed keys loudly via continue rather
    # than raising — a corrupt save shouldn't take the narrator down.
    character_bars: list[dict[str, Any]] = []
    for key_str, bar in ms.ledger.items():
        try:
            bk = _deserialize_bar_key(key_str)
        except Exception:
            continue
        if bk.scope != "character" or bk.owner_id != args.character_id:
            continue
        # ``LedgerBarSpec.range`` is ``tuple[float, float]`` = (lo, hi).
        # Surface the upper bound as ``max`` for the narrator UI.
        bar_max = bar.spec.range[1] if bar.spec.range else None
        character_bars.append(
            {
                "bar_id": bk.bar_id,
                "value": bar.value,
                "max": bar_max,
            }
        )

    known = ms.known_spells.get(args.character_id, [])
    prepared = ms.prepared_spells.get(args.character_id, {})
    spent = ms.spent_spells.get(args.character_id, {})
    control_tier = ms.control_tier.get(args.character_id, 0)

    # v1: WorkingRecord exposes ``actor`` (caster) only. See module
    # docstring for the broader-target follow-up.
    active_workings = [w for w in ms.working_log if w.actor == args.character_id]

    if is_self:
        payload: dict[str, Any] = {
            "character_id": args.character_id,
            "magic_state_present": True,
            "is_self": True,
            "character_bars": character_bars,
            "known_spells": list(known),
            "prepared_spells": {str(lvl): list(spells) for lvl, spells in prepared.items()},
            "spent_spells": {str(lvl): list(spells) for lvl, spells in spent.items()},
            "control_tier": control_tier,
            "active_working_count": len(active_workings),
        }
    else:
        # Coarsened: counts only. No bar values, no spell ids.
        payload = {
            "character_id": args.character_id,
            "magic_state_present": True,
            "is_self": False,
            "known_spell_count": len(known),
            "prepared_spell_count": sum(len(v) for v in prepared.values()),
            "active_working_count": len(active_workings),
            # bar values intentionally omitted — mana pool hidden for non-self.
        }

    # OTEL — always write the canonical attribute set so the GM panel
    # can correlate self vs coarsened views.
    ctx.otel_span.set_attribute("tool.magic.character_id", args.character_id)
    ctx.otel_span.set_attribute("tool.magic.active_spell_count", len(active_workings))
    ctx.otel_span.set_attribute("tool.magic.magic_state_present", True)
    ctx.otel_span.set_attribute("tool.magic.is_self", is_self)
    # ``mana_remaining`` uses a sentinel of -1.0 when (a) the world has
    # no bar literally named ``mana`` (most worlds — sanity/notice
    # instead) or (b) the perspective is another PC and we are hiding
    # the value. The GM panel treats -1.0 as "n/a" rather than "zero
    # mana."
    if is_self:
        mana_bar = next((b for b in character_bars if b["bar_id"] == "mana"), None)
        ctx.otel_span.set_attribute(
            "tool.magic.mana_remaining",
            float(mana_bar["value"]) if mana_bar is not None else -1.0,
        )
    else:
        ctx.otel_span.set_attribute("tool.magic.mana_remaining", -1.0)

    return ToolResult.ok(payload)
