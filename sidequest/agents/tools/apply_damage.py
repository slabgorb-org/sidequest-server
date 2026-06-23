"""Tool: apply_damage — narrator-driven HP damage.

Under ADR-114 (supersedes ADR-078) the engine model IS HP — this tool
applies HP damage directly; there is no translation layer. It remains the
narrator's freeform/environmental damage path, complementary to the beat
strike channel (beat_kinds.py).

    narrator: apply_damage(target=Alice, amount=4, ...)
                    |
                    v
    engine:   CreatureCore.apply_hp_delta(-4)

The OTEL attribute name is ``tool.damage.target_hp_after`` (and the
payload field is ``target_hp_after``) — propagating the old "edge" name
into new code after ADR-114 would muddle the model every time a future
reader touches it. The tool-name surface (``apply_damage``) is the only
place the legacy verb survives, because that's the word the narrator
actually uses.

The OTEL span is emitted via the Phase B Registry dispatcher
(``tool.write.apply_damage``); this handler enriches it with the
per-tool ``tool.damage.*`` attributes the GM panel reads.

Story 158-3 (sq-playtest 2026-06-22) adds a confrontation guard: damage to a
non-player creature is rejected when no confrontation is seated
(``snapshot.encounter is None``), because opponent HP is undefined outside a
confrontation (ADR-116). Player characters are exempt — they are the
legitimate target of the freeform/environmental path even with no encounter.
Every decision sets ``tool.damage.guard_rejected`` (and ``guard_reason`` on a
block) so the GM panel can see blocked vs. accepted writes.

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


class ApplyDamageArgs(BaseModel):
    target: str = Field(..., description="Name of the character or NPC to damage.")
    amount: int = Field(
        ...,
        ge=0,
        description="Damage amount. 0 is valid (no-op) and still emits the span.",
    )
    damage_type: str = Field(
        default="untyped",
        description="Genre-flavored damage type (slashing/fire/psychic/etc.).",
    )
    source: str = Field(
        default="",
        description="One-line cause description; surfaces in OTEL for GM-panel review.",
    )


@tool(
    name="apply_damage",
    description=(
        "Apply HP damage to a character or combatant. Use after a roll has "
        "determined the damage amount. `damage_type` is genre-flavored "
        "(slashing/fire/psychic/etc.); `source` is a one-line cause "
        "description."
    ),
    category=ToolCategory.WRITE,
    # WN combat resolution belongs to run_wn_round (ADR-143); withheld from the
    # narrator on a live WN combat. sq-playtest 2026-06-22.
    combat_resolution=True,
)
async def apply_damage(args: ApplyDamageArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    core = snapshot.find_creature_core(args.target)
    if core is None:
        return ToolResult.not_found(f"unknown target: {args.target!r}")

    # Story 158-3: opponent HP is only defined inside a seated confrontation
    # (ADR-116 "A Confrontation Requires an Other"). The narrator's freeform
    # path may damage a player character at any time — environmental hazards
    # (a trap, a fall) are the documented use — but damaging a non-player
    # creature with NO confrontation seated is an unbacked opponent-HP write:
    # there is no Other to take the hit. Reject it loudly and emit a warning
    # span so the GM panel (the lie detector) sees the blocked write rather
    # than a silently-applied, mechanically-backless HP change.
    target_is_player = any(ch.core.name == args.target for ch in snapshot.characters)
    if not target_is_player and snapshot.encounter is None:
        ctx.otel_span.set_attribute("tool.damage.target", args.target)
        ctx.otel_span.set_attribute("tool.damage.amount", args.amount)
        ctx.otel_span.set_attribute("tool.damage.guard_rejected", True)
        ctx.otel_span.set_attribute("tool.damage.guard_reason", "no_confrontation_seated")
        return ToolResult.error(
            f"cannot damage {args.target!r}: no confrontation is seated — opponent HP is "
            "undefined outside a confrontation (ADR-116). Seat the encounter first.",
            recoverable=True,
        )

    # Apply damage as a negative HP delta. amount=0 is a deliberate no-op
    # but we still walk the persistence path so the span lands and any
    # narrator audit trail stays consistent.
    core.apply_hp_delta(-args.amount)
    target_hp_after = core.hp.current

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.damage.target", args.target)
    ctx.otel_span.set_attribute("tool.damage.amount", args.amount)
    ctx.otel_span.set_attribute("tool.damage.damage_type", args.damage_type)
    ctx.otel_span.set_attribute("tool.damage.source", args.source)
    ctx.otel_span.set_attribute("tool.damage.guard_rejected", False)
    ctx.otel_span.set_attribute("tool.damage.target_hp_after", target_hp_after)

    return ToolResult.ok(
        {
            "target": args.target,
            "amount": args.amount,
            "damage_type": args.damage_type,
            "source": args.source,
            "target_hp_after": target_hp_after,
        }
    )
