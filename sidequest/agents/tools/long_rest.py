"""Tool: long_rest — narrator-driven WWN party-wide long rest (Effort + casts refresh).

This is the PRODUCTION CALLER that makes the long-rest reclaim reachable in a
real game. It is a THIN wrapper — the reclaim rules live in
WwnRulesetModule.reclaim_day_and_refresh; this tool iterates every PC core and
calls it, then optionally re-prepares spells for named casters.

    narrator: long_rest(comfortable=True, reprepare={"Lyra": ["wind_blast"]})
                    |
                    v
    for each PC core with effort or spellcasting:
        WwnRulesetModule.reclaim_day_and_refresh(core, comfortable, cfg)
    for each named caster in reprepare:
        validate spell ids against pack.wwn_spell_catalog (fail loud on unknown)
        set core.spellcasting.prepared = new list

Guards (fail loud — no silent fallbacks per CLAUDE.md):
- ``ctx.genre_pack.rules.ruleset != "wwn"`` → ValueError (tool is WWN-only)
- no active session → ERROR_FATAL
- reprepare given but pack.wwn_spell_catalog is None → ValueError
- unknown spell id in reprepare → ValueError (no silent fallback)

NOTE: No clock beat is fired here (parity with CWN adjust_system_strain which
does not fire a clock beat). The REST StoryBeat advance is a separate concern;
the REST beat exists in the beat list but is intentionally not wired here —
the beat filter wires it in from dispatch, not from this tool.

Party-wide iteration: snapshot.characters holds the PC list. Each Character's
.core is a CreatureCore; we call reclaim_day_and_refresh on cores that have at
least one effort pool OR a spellcasting state (skipping bare NPCs which share no
CreatureCore via snapshot.characters — that list is PCs only).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.telemetry.spans.wwn import wwn_long_rest_span


class LongRestArgs(BaseModel):
    comfortable: bool = Field(
        default=True,
        description=(
            "Whether the rest was comfortable (safe bed, adequate food). "
            "True: drops both 'scene' and 'day' Effort commitments and refreshes casts. "
            "False: drops only 'scene' Effort; 'day' Effort stays committed "
            "when the pack's day_reclaim_requires_comfort is True. "
            "Casts always refresh on any long rest."
        ),
    )
    reprepare: dict[str, list[str]] | None = Field(
        default=None,
        description=(
            "Optional mapping of caster PC name → new prepared spell id list. "
            "Each id must exist in the pack's WWN spell catalog (fail loud on unknown). "
            "Omit to keep existing prepared lists unchanged. "
            'Example: {"Lyra": ["wind_blast", "stone_shield"]}'
        ),
    )


@tool(
    name="long_rest",
    description=(
        "WWN party-wide long rest: reclaim Effort (scene always; day when comfortable), "
        "refresh daily spell casts, and optionally reprepare spells. "
        "WWN-only tool — raises if the loaded pack is not ruleset 'wwn'. "
        "comfortable: True drops day Effort; False leaves day Effort committed "
        "(when pack.day_reclaim_requires_comfort is True). "
        "reprepare: optional dict mapping caster name → new prepared spell ids "
        "(validated against the pack's spell catalog — unknown ids raise)."
    ),
    category=ToolCategory.WRITE,
    ruleset="wwn",
)
async def long_rest(args: LongRestArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    pack = ctx.genre_pack
    if pack is None or pack.rules is None or pack.rules.ruleset != "wwn":
        ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
        raise ValueError(f"long_rest is wwn-only; loaded pack has ruleset={ruleset!r}")

    # Validate reprepare catalog availability before mutating anything.
    if args.reprepare:
        catalog = getattr(pack, "wwn_spell_catalog", None)
        if catalog is None:
            raise ValueError(
                "long_rest reprepare requires pack.wwn_spell_catalog; "
                "no catalog is loaded for this pack"
            )
        # Pre-validate all spell ids across all named casters before any mutation.
        known_ids = {s.id for s in catalog.spells}
        for _caster_name, spell_ids in args.reprepare.items():
            for sid in spell_ids:
                if sid not in known_ids:
                    raise ValueError(
                        f"long_rest reprepare: spell {sid!r} is not in the pack's "
                        f"WWN spell catalog (have: {sorted(known_ids)!r})"
                    )

    snapshot = session.snapshot
    module = get_ruleset_module(pack.rules.ruleset)
    assert isinstance(module, WwnRulesetModule), (
        f"expected WwnRulesetModule for slug 'wwn', got {type(module).__name__!r}"
    )
    cfg = pack.rules.ruleset_config()

    rested_actors: list[str] = []

    for character in snapshot.characters:
        core = character.core
        has_effort = bool(core.effort)
        has_spellcasting = core.spellcasting is not None
        if not has_effort and not has_spellcasting:
            continue

        day_committed_before = sum(
            c.points
            for pool in core.effort.values()
            for c in pool.commitments
            if c.duration == "day"
        )

        module.reclaim_day_and_refresh(
            core=core,
            comfortable=args.comfortable,
            cfg=cfg,
        )

        # Determine what actually happened for the span.
        day_committed_after = sum(
            c.points
            for pool in core.effort.values()
            for c in pool.commitments
            if c.duration == "day"
        )
        day_effort_reclaimed = day_committed_after < day_committed_before
        casts_refreshed_to = core.spellcasting.casts_remaining if core.spellcasting else 0

        # Apply reprepare for this actor if specified.
        reprepared = False
        if args.reprepare and core.name in args.reprepare and core.spellcasting is not None:
            core.spellcasting.prepared = list(args.reprepare[core.name])
            reprepared = True

        wwn_long_rest_span(
            actor=core.name,
            day_effort_reclaimed=day_effort_reclaimed,
            casts_refreshed_to=casts_refreshed_to,
            reprepared=reprepared,
            comfortable=args.comfortable,
        )
        rested_actors.append(core.name)

    ctx.repository.save(snapshot)

    ctx.otel_span.set_attribute("tool.long_rest.comfortable", args.comfortable)
    ctx.otel_span.set_attribute("tool.long_rest.rested_count", len(rested_actors))
    ctx.otel_span.set_attribute("tool.long_rest.rested_actors", ", ".join(rested_actors))

    return ToolResult.ok(
        {
            "comfortable": args.comfortable,
            "rested": True,
            "rested_actors": rested_actors,
        }
    )
