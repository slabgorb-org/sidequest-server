"""Tool: begin_confrontation — START a structured confrontation (Story 59-1).

The SDK engagement SIGNAL the narrator backend was missing
----------------------------------------------------------
Engagement = creating a :class:`~sidequest.game.encounter.StructuredEncounter`
from a Confrontation Def when the prose introduces a stake-binding engagement
(physical, social, or reputational). On the legacy ``claude -p`` backend the
narrator populated the ``confrontation`` sidecar field and the server consumed
it in ``narration_apply`` to instantiate the encounter. The default
``anthropic_sdk`` backend (ADR-101/102) had NO path that could do this:

* ``advance_confrontation`` only ADVANCES an already-active dial and "fails
  fatally if no encounter is active" — it cannot START one.
* ``generate_encounter`` is a hard stub that always returns a fatal error.
* ``apply_world_patch`` carries the sibling game_patch fields but not this one,
  and is a deprecation-targeted escape hatch — wrong home for a load-bearing
  engagement path.

So a tea_and_murder social standoff produced convincing prose with
``confrontation=None`` every turn (2026-05-21 Glenross playtest).

Why this tool does NOT create the encounter itself
--------------------------------------------------
An SDK tool cannot create the encounter on the live snapshot: ``ctx.store.load()``
returns a FRESH deserialized snapshot, and the tool's ``ctx.store.save`` is
clobbered at turn end by ``room.save()``, which persists the room's CANONICAL
in-memory snapshot — the object the tool never touched (verified: persistence.py
``load`` deserializes fresh; session_room.py ``save`` writes the canonical;
nothing reloads it after the dispatch loop). So this tool is the narrator-facing
SIGNAL + validator: it validates the requested type against the genre and the
active-encounter state, emits OTEL, and returns. ``_assemble_turn_result_sdk``
copies the requested type onto ``result.confrontation`` from the tool-call
ledger, and ``narration_apply``'s consumer creates the encounter on the
CANONICAL snapshot IN PLACE — the SAME single mechanism the legacy backend uses.
Actors come from the narrator's sidecar ``npcs_present`` (presentation field on
both backends), exactly as on the legacy path.

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.begin_confrontation.type`` — requested confrontation type.
* ``tool.begin_confrontation.reason`` — narrator audit note (may be empty).
* ``tool.begin_confrontation.signalled`` — bool; True iff a valid, startable
  type was accepted (engagement is then applied by narration_apply). False on a
  rejected call (already-active encounter, unknown type, or wiring fault).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.agents.narrator_guardrails import CONFRONTATION_TRIGGER_CONSTRAINT
from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)


class BeginConfrontationArgs(BaseModel):
    model_config = {"extra": "forbid"}

    confrontation_type: str = Field(
        ...,
        min_length=1,
        description=(
            "The Confrontation Def type to START, spelled exactly as it appears "
            "in AVAILABLE ENCOUNTER TYPES (lowercase, snake_case where compound) "
            "— e.g. 'combat', 'ship_combat', 'dogfight', 'chase', 'negotiation', "
            "'trial', 'auction', 'social_duel', 'scandal'. Pick the MOST SPECIFIC "
            "type the genre offers."
        ),
    )
    reason: str = Field(
        default="",
        description="One-line narrator note for OTEL / GM-panel audit.",
    )


@tool(
    name="begin_confrontation",
    description=(
        "START a structured confrontation when your prose this turn introduces "
        "a stake-binding engagement. The encounter is created from the genre's "
        "Confrontation Def using the npcs_present you emit this turn; use "
        "advance_confrontation / advance_encounter_beat for subsequent rounds "
        "once it is active. Fails (recoverable) if an encounter is already "
        "active or the type is not offered by this genre.\n\n"
        # Story 59-1: the confrontation_trigger guardrail lives on THIS tool's
        # description — the live engagement signal the SDK narrator reads when
        # weighing the call (ADR-111: SDK selection keys on tool descriptions).
        # Relocated off the always-erroring generate_encounter stub. Single
        # source of truth: narrator_guardrails.CONFRONTATION_TRIGGER_CONSTRAINT.
        f"{CONFRONTATION_TRIGGER_CONSTRAINT}"
    ),
    category=ToolCategory.WRITE,
)
async def begin_confrontation(args: BeginConfrontationArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot

    ctx.otel_span.set_attribute("tool.begin_confrontation.type", args.confrontation_type)
    ctx.otel_span.set_attribute("tool.begin_confrontation.reason", args.reason)

    # An active, unresolved encounter already owns the turn — advancing it is
    # advance_confrontation's job. Recoverable so the narrator can re-cast (No
    # Silent Fallbacks: say why, loudly). narration_apply's consumer guards on
    # the same condition, so this is also narrator-feedback, not the authority.
    if snapshot.encounter is not None and not snapshot.encounter.resolved:
        ctx.otel_span.set_attribute("tool.begin_confrontation.signalled", False)
        return ToolResult.error(
            "an encounter is already active — use advance_confrontation to "
            "advance its dial or advance_encounter_beat for beat selections; "
            "begin_confrontation only STARTS a fresh confrontation.",
            recoverable=True,
        )

    pack = ctx.genre_pack
    if pack is None:
        # The pack is wired onto ToolContext at the SDK dispatch site
        # (orchestrator). Its absence is a wiring fault, not a recoverable
        # narrator choice — fail loudly (CLAUDE.md: no silent fallback).
        ctx.otel_span.set_attribute("tool.begin_confrontation.signalled", False)
        return ToolResult.error(
            "begin_confrontation: no genre pack on ToolContext — cannot validate "
            "the confrontation type. This is a server wiring fault.",
            recoverable=False,
        )

    # Validate the type against the genre so a narrator typo gets immediate,
    # recoverable feedback rather than silently failing to engage. The
    # assembler re-checks against the offered types before routing the value
    # to narration_apply, so an unknown type can never reach the consumer.
    from sidequest.server.dispatch.confrontation import find_confrontation_def

    defs = pack.rules.confrontations if pack.rules else []
    if find_confrontation_def(defs, args.confrontation_type) is None:
        ctx.otel_span.set_attribute("tool.begin_confrontation.signalled", False)
        offered = sorted(
            (getattr(d, "confrontation_type", None) or getattr(d, "type", "")) for d in defs
        )
        return ToolResult.error(
            f"confrontation type {args.confrontation_type!r} is not offered by this "
            f"genre. Offered types: {offered!r}. Pick the most specific applicable type.",
            recoverable=True,
        )

    # Accepted. Engagement is applied by narration_apply from result.confrontation
    # (set in _assemble_turn_result_sdk from this tool call); the encounter lands
    # on the canonical snapshot, clobber-free.
    ctx.otel_span.set_attribute("tool.begin_confrontation.signalled", True)
    return ToolResult.ok(
        {
            "confrontation_type": args.confrontation_type,
            "signalled": True,
            "reason": args.reason,
        }
    )
