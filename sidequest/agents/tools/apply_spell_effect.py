"""Tool: apply_spell_effect — narrator-declared spell effect log + mana decrement.

Phase C Task 23 — WRITE tool.

v1 scope (deliberately narrow)
------------------------------
This adapter does **not** invoke the full magic resolver
(:func:`sidequest.magic.learned_ops.cast`). The resolver requires a
fully-shaped :class:`sidequest.magic.models.MagicWorking` and runs
preparation, slot-level, and consent validation — coupling that the
narrator tool surface is not ready to take in Phase C.

What this tool does instead:

1. Appends a :class:`sidequest.magic.state.WorkingRecord` to
   ``MagicState.working_log`` with ``plugin='narrator_declared'`` and
   ``mechanism='apply_spell_effect_tool'``. The record carries the
   narrator's declared ``spell_id``, ``caster`` (as ``actor``), and a
   JSON-encoded ``flavor`` blob containing ``targets`` + ``overrides``.
2. If ``cost > 0`` and the caster has a character-scope ``mana`` ledger
   bar, decrement that bar by ``cost`` (clamped at 0). If no ``mana``
   bar exists, the log entry still lands and ``mana_decremented=False``
   is reported.

What this tool does NOT do (deferred to Phase D/E):

- Validate spell preparation (``MagicState.prepared_spells``).
- Burn a spell slot (``slots_l1`` / ``slots_l2`` ledger bars).
- Apply hard limits, threshold crossings, or consent checks.
- Promote statuses from mandatory outputs.
- Route non-``mana`` cost types via :meth:`MagicState._route_cost`.

The Phase D/E replacement will route through ``narration_apply`` and
``learned_ops.cast`` once the SDK is the production narrator path.
Until then this tool serves as a narrator-declared effect log — the
GM panel can see *what the narrator said* even when the mechanical
resolver isn't engaged.

Why ``magic_state=None`` is fatal
---------------------------------
Unlike :mod:`query_magic_state` (which gracefully reports
``magic_state_present=false`` for non-magical worlds), this tool is a
WRITE. If the narrator is calling ``apply_spell_effect`` on a world
with no magic config, that's a routing error, not a content gap —
fail loudly so Sebastien's lie-detector flags it.

Cost-recording vs decrement
---------------------------
The ``WorkingRecord.costs`` dict is populated with ``{"mana": cost}``
whenever ``cost > 0`` — even when no mana bar exists on the caster.
This keeps the narrator's declared cost visible in the GM panel even
when the engine couldn't actually deduct it; the gap between
"declared" and "deducted" is itself a signal.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.magic.state import (
    BarKey,
    WorkingRecord,
    _serialize_bar_key,
)


class ApplySpellEffectArgs(BaseModel):
    spell_id: str = Field(..., min_length=1, description="Spell identifier (e.g. 'fireball').")
    caster: str = Field(
        ...,
        min_length=1,
        description="Name of casting character or NPC.",
    )
    targets: list[str] = Field(
        default_factory=list,
        description="Names of affected characters/NPCs.",
    )
    cost: int = Field(
        default=0,
        ge=0,
        description=(
            "Cost paid in the caster's 'mana' bar (if present). 0 = no cost "
            "mutation. v1 supports the 'mana' bar only; other cost types are "
            "deferred to the Phase D/E resolver hookup."
        ),
    )
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form narrator-supplied modifiers (range, duration, save_dc, "
            "etc.). v1 records to working_log.flavor as JSON; the resolver "
            "ignores them. Phase D/E will route these into the MagicWorking."
        ),
    )


@tool(
    name="apply_spell_effect",
    description="Apply a spell's effect to its targets via the magic resolver.",
    category=ToolCategory.WRITE,
)
async def apply_spell_effect(args: ApplySpellEffectArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    ms = snapshot.magic_state
    if ms is None:
        # WRITE on a non-magical world is a routing error, not a content
        # gap — fail loudly. See module docstring.
        return ToolResult.error(
            "no magic_state — world has no magic config",
            recoverable=False,
        )

    # Encode targets + overrides as a JSON blob into the record's
    # ``flavor`` field. WorkingRecord has no structured ``targets`` /
    # ``overrides`` columns in v1 (see query_magic_state module docstring
    # — same shape limitation as the working-record actor filter), so
    # JSON-in-flavor is the v1 transport. Phase D/E will widen
    # WorkingRecord with a structured targets column.
    flavor_blob = json.dumps(
        {"targets": list(args.targets), "overrides": dict(args.overrides)},
        default=str,
    )

    record = WorkingRecord(
        plugin="narrator_declared",
        mechanism="apply_spell_effect_tool",
        actor=args.caster,
        # Record the declared cost regardless of whether the engine has
        # a matching bar to deduct — the GM panel can compare declared
        # vs deducted via the OTEL span.
        costs={"mana": float(args.cost)} if args.cost > 0 else {},
        domain="generic",
        narrator_basis=f"narrator-declared spell effect: {args.spell_id}",
        flavor=flavor_blob,
        spell_id=args.spell_id,
        slot_level=None,
    )
    ms.working_log.append(record)

    # Decrement mana-like bar if present. v1 only handles the literal
    # 'mana' bar; other cost types ride the Phase D/E resolver.
    mana_decremented = False
    mana_remaining_after: float | None = None
    if args.cost > 0:
        key = _serialize_bar_key(BarKey(scope="character", owner_id=args.caster, bar_id="mana"))
        bar = ms.ledger.get(key)
        if bar is not None:
            bar.value = max(0.0, bar.value - float(args.cost))
            mana_decremented = True
            mana_remaining_after = bar.value

    ctx.store.save(snapshot)

    ctx.otel_span.set_attribute("tool.spell.id", args.spell_id)
    ctx.otel_span.set_attribute("tool.spell.caster", args.caster)
    ctx.otel_span.set_attribute("tool.spell.target_count", len(args.targets))
    ctx.otel_span.set_attribute("tool.spell.cost", args.cost)
    ctx.otel_span.set_attribute("tool.spell.mana_decremented", mana_decremented)
    if mana_remaining_after is not None:
        ctx.otel_span.set_attribute("tool.spell.mana_remaining_after", mana_remaining_after)

    return ToolResult.ok(
        {
            "spell_id": args.spell_id,
            "caster": args.caster,
            "targets": list(args.targets),
            "cost": args.cost,
            "mana_decremented": mana_decremented,
            "mana_remaining_after": mana_remaining_after,
            "working_log_size": len(ms.working_log),
        }
    )
