"""Tool: tick_tropes — advance the trope engine one turn.

Phase C Task 20 — WRITE tool
----------------------------
Replaces the sidecar ``trope_tick`` field. The narrator calls this
when its pending narration should be reconciled against the trope
engine (ADR-018 progression, beat fire, activation gating, time-skip
drift).

Mechanic
~~~~~~~~
Delegates entirely to
:func:`sidequest.game.trope_tick.tick_tropes`. That engine mutates
``snapshot.active_tropes`` in place across Passes A→E (passive
progression, optional time-skip drift, staggered beat fire, implicit
resolution, activation gate, aggregate metrics). The handler diffs
``active_tropes`` IDs before and after to surface which tropes
*newly* engaged (transitioned dormant → progressing) on this tick —
that diff is what the GM panel surfaces as "engaged this turn."

Deviations from plan
~~~~~~~~~~~~~~~~~~~~
1. ``narration_text`` is accepted but **not used** by the v1 trope
   engine. The trope engine v1 advances by passive progression and
   pre-loaded escalation thresholds, not by text matching. The field
   is kept on the args model for forward-compat (ADR-018 v2 may key
   activation on narration keywords) and is recorded in OTEL so the
   GM panel can audit what text the tick was associated with.
2. ``ctx.genre_pack`` is **Phase B amendment #3** — the trope engine
   needs ``pack.tropes`` to look up ``TropeDefinition`` rows; the
   pack is not on ``GameSnapshot`` (it lives on
   ``SessionHandler.genre_pack``). When ``ctx.genre_pack is None``
   (Phase C: no production wire yet), this tool records an OTEL
   marker (``genre_pack_wired=False``) and no-ops with
   ``engaged_count=0``. Phase E wires the production call site.
3. ``active_tropes`` is ``list[TropeState]``; entries have ``id`` but
   no ``name`` field. The diff therefore exposes ``engaged_ids`` and
   uses the same list as ``engaged_names`` (the planning sketch had
   a separate name field, but the model doesn't carry one).

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.tropes.engaged_count`` — newly engaged trope count.
* ``tool.tropes.engaged_names`` — newly engaged trope IDs (same as
  ``engaged_ids`` — :class:`TropeState` has no name field).
* ``tool.tropes.genre_pack_wired`` — ``True`` iff a genre pack was
  available; ``False`` when Phase C runs with the slot empty.
* ``tool.tropes.days_advanced`` — value passed to the engine for the
  Pass A2 time-skip drift.
* ``tool.tropes.narration_text_len`` — length of the (unused) text
  field so the GM panel can correlate ticks with narration size.

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
from sidequest.game.trope_tick import tick_tropes as _tick_tropes_engine


class TickTropesArgs(BaseModel):
    model_config = {"extra": "forbid"}

    narration_text: str = Field(
        default="",
        description=(
            "Pending narration text. The v1 trope engine does not text-match; "
            "the field is accepted forward-compat (ADR-018 v2 will key on "
            "text) and recorded in OTEL for GM-panel audit."
        ),
    )
    days_advanced: int = Field(
        default=0,
        ge=0,
        description=(
            "Days advanced by this narration tick — drives the time-skip "
            "beat drift in trope_tick PASS_A2. Zero (default) skips that "
            "pass entirely."
        ),
    )


@tool(
    name="tick_tropes",
    description=(
        "Advance the trope engine one turn. Runs ADR-018 progression, "
        "staggered beat fire, implicit resolution, activation gating, and "
        "(when `days_advanced > 0`) time-skip drift. Returns the count and "
        "IDs of tropes that newly engaged (dormant → progressing) on this "
        "tick. `narration_text` is accepted forward-compat but not used by "
        "the v1 engine. No-ops with `engaged_count=0` when the genre pack "
        "is not wired (Phase C state)."
    ),
    category=ToolCategory.WRITE,
)
async def tick_tropes(args: TickTropesArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)
    snapshot = session.snapshot

    if ctx.genre_pack is None:
        # Phase C: no production wire for the genre pack yet. Record the
        # marker so the GM panel can see the tool fired but the engine was
        # silenced, and return a zero-engagement payload.
        ctx.otel_span.set_attribute("tool.tropes.engaged_count", 0)
        ctx.otel_span.set_attribute("tool.tropes.engaged_names", [])
        ctx.otel_span.set_attribute("tool.tropes.genre_pack_wired", False)
        ctx.otel_span.set_attribute("tool.tropes.days_advanced", args.days_advanced)
        ctx.otel_span.set_attribute("tool.tropes.narration_text_len", len(args.narration_text))
        return ToolResult.ok(
            {
                "engaged_count": 0,
                "engaged_names": [],
                "engaged_ids": [],
                "active_total": len(snapshot.active_tropes),
                "genre_pack_wired": False,
            }
        )

    # TropeState carries only ``id`` (no name) — the diff is by id, and the
    # engaged_names surface in the payload reuses those ids.
    active_before = {t.id for t in snapshot.active_tropes if t.status == "progressing"}

    _tick_tropes_engine(
        snapshot,
        ctx.genre_pack,
        now_turn=ctx.turn_number,
        days_advanced=args.days_advanced,
    )

    active_after = {t.id for t in snapshot.active_tropes if t.status == "progressing"}
    newly_engaged_ids = sorted(active_after - active_before)
    # TropeState has no separate display name; reuse ids for "names".
    engaged_names = newly_engaged_ids

    ctx.store.save(snapshot)

    ctx.otel_span.set_attribute("tool.tropes.engaged_count", len(newly_engaged_ids))
    ctx.otel_span.set_attribute("tool.tropes.engaged_names", engaged_names)
    ctx.otel_span.set_attribute("tool.tropes.genre_pack_wired", True)
    ctx.otel_span.set_attribute("tool.tropes.days_advanced", args.days_advanced)
    ctx.otel_span.set_attribute("tool.tropes.narration_text_len", len(args.narration_text))

    return ToolResult.ok(
        {
            "engaged_count": len(newly_engaged_ids),
            "engaged_names": engaged_names,
            "engaged_ids": newly_engaged_ids,
            "active_total": len(snapshot.active_tropes),
            "genre_pack_wired": True,
        }
    )
