"""Tool: begin_confrontation — START a structured confrontation (Story 59-1).

The engagement writer the SDK narrator backend was missing
-----------------------------------------------------------
Engagement = creating a :class:`~sidequest.game.encounter.StructuredEncounter`
from a Confrontation Def when the prose introduces a stake-binding engagement
(physical, social, or reputational). On the legacy ``claude -p`` backend the
narrator populated the ``confrontation`` sidecar field and the server consumed
it in ``narration_apply`` to instantiate the encounter. The default
``anthropic_sdk`` backend (ADR-101/102) had NO tool that could do this:

* ``advance_confrontation`` only ADVANCES an already-active dial and "fails
  fatally if no encounter is active" — it cannot START one.
* ``generate_encounter`` is a hard stub that always returns a fatal error.
* ``apply_world_patch`` carries the sibling game_patch fields but not this one,
  and is a deprecation-targeted escape hatch — wrong home for a load-bearing
  engagement path.

So a tea_and_murder social standoff produced convincing prose with
``confrontation=None`` every turn (2026-05-21 Glenross playtest). This tool
closes that gap: the narrator calls ``begin_confrontation`` on the turn the
trigger appears in fiction, and the handler creates the encounter during the
SDK tool-dispatch loop (mutate + ``ctx.store.save``), the same single-authority
pattern every other SDK WRITE tool follows. ``narration_apply`` then sees an
active encounter and does not double-create.

It reuses ``instantiate_encounter_from_trigger`` — the same helper the legacy
consumer calls — so encounter creation has ONE mechanism across both backends.

OTEL attributes
~~~~~~~~~~~~~~~
* ``tool.begin_confrontation.type`` — requested confrontation type.
* ``tool.begin_confrontation.player`` — perspective PC the encounter seats.
* ``tool.begin_confrontation.created`` — bool; True iff a new encounter was
  written this call. False when an encounter was already active (the narrator
  should ``advance_confrontation`` instead).
"""

from __future__ import annotations

from typing import Any

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
        "a stake-binding engagement. Creates the encounter from the genre's "
        "Confrontation Def; use advance_confrontation / advance_encounter_beat "
        "for subsequent rounds once it is active. Fails (recoverable) if an "
        "encounter is already active.\n\n"
        # Story 59-1: the confrontation_trigger guardrail lives on THIS tool's
        # description — the live engagement writer the SDK narrator reads when
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
    # advance_confrontation's job, not ours. Recoverable so the narrator can
    # re-cast on the next tool call (No Silent Fallbacks: say why, loudly).
    if snapshot.encounter is not None and not snapshot.encounter.resolved:
        ctx.otel_span.set_attribute("tool.begin_confrontation.created", False)
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
        ctx.otel_span.set_attribute("tool.begin_confrontation.created", False)
        return ToolResult.error(
            "begin_confrontation: no genre pack on ToolContext — cannot resolve "
            "the Confrontation Def. This is a server wiring fault.",
            recoverable=False,
        )

    player_name = ctx.perspective_pc
    if not player_name:
        ctx.otel_span.set_attribute("tool.begin_confrontation.created", False)
        return ToolResult.error(
            "begin_confrontation: no perspective PC on ToolContext — cannot seat "
            "the player actor. This is a server wiring fault.",
            recoverable=False,
        )

    ctx.otel_span.set_attribute("tool.begin_confrontation.player", player_name)

    # Bundled-MP turns: seat every other seated PC alongside the submitter,
    # mirroring the narration_apply consumer (playtest 2026-05-03 widget fix).
    additional_pc_names = [
        name for name in snapshot.player_seats.values() if name and name != player_name
    ]

    from sidequest.server.dispatch.encounter_lifecycle import (
        NoOpponentAvailableError,
        instantiate_encounter_from_trigger,
    )

    try:
        # npcs_present=[] lets the lifecycle fall back to registry NPCs at the
        # player's location (the narrator hasn't run npc extraction yet at
        # tool-dispatch time). Unknown-type / bad-side ValueErrors PROPAGATE —
        # those are config/extraction faults the suite asserts crash the turn.
        encounter = instantiate_encounter_from_trigger(
            snapshot=snapshot,
            pack=pack,
            encounter_type=args.confrontation_type,
            player_name=player_name,
            npcs_present=[],
            genre_slug=snapshot.genre_slug,
            additional_player_names=additional_pc_names,
        )
    except NoOpponentAvailableError as exc:
        # Story 45-33 guard: a combat encounter with zero resolvable opponents.
        # The lifecycle already emitted its OTEL span; surface a recoverable
        # error so the turn stays resilient and the narrator can re-cast.
        ctx.otel_span.set_attribute("tool.begin_confrontation.created", False)
        return ToolResult.error(
            f"begin_confrontation: no opponent available for {args.confrontation_type!r}: {exc}",
            recoverable=True,
        )

    if encounter is None:
        # instantiate returns None only when an active encounter already
        # exists — guarded above, so this is defensive against a race.
        ctx.otel_span.set_attribute("tool.begin_confrontation.created", False)
        return ToolResult.error(
            "begin_confrontation: an encounter already exists; not replaced.",
            recoverable=True,
        )

    ctx.store.save(snapshot)
    ctx.otel_span.set_attribute("tool.begin_confrontation.created", True)

    result: dict[str, Any] = {
        "confrontation_type": args.confrontation_type,
        "player_name": player_name,
        "created": True,
        "reason": args.reason,
    }
    return ToolResult.ok(result)
