"""Tool: record_quest — mint or evolve a campaign-spine quest.

Story 77-2 (ADR-137 Option B) — WRITE tool
------------------------------------------
The narrator-facing create/evolve affordance for the campaign spine. On a
fresh ``quest_id`` it MINTS a structured quest (``QuestEntry`` — title +
objective + status + optional anchor) and fires ``quest.created``. On an
existing id it UPDATES the entry (primarily status) and fires
``quest.updated`` — the behavioural successor to the legacy
``quest_updates`` lane (77-4 retired that lane onto this tool; the legacy
``SPAN_QUEST_UPDATE`` no longer fires from the quest-update path, though it
survives as the GM-panel surface for the separate trope-resolution handshake).

State-bloat guardrail (the explicit YAML AC): minting is capped at
``_QUEST_LOG_CARDINALITY_CAP`` quests so a runaway narrator cannot grow
``quest_log`` unbounded in Postgres. The cap blocks *minting* only — evolving
an existing quest is always allowed. The per-call payload is bound tightly at
the args schema (ADR-102 / SOUL §Cost Scales with Drama) so a quiet town walk
cannot smuggle a giant entry.

Anchor sub-feature (77-3 sequencing): until ``quest_anchors`` is promoted to a
first-class ``WorldStatePatch`` field (story 77-3), an anchor is written
directly onto the loaded snapshot's ``quest_anchors`` list and recorded on the
QuestEntry's ``anchor_id``.

Modelled on ``update_npc_disposition`` (load -> mutate -> save -> OTEL); the
Registry's per-session WRITE lock serialises concurrent calls.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.session import QuestEntry
from sidequest.telemetry.spans import quest_created_span, quest_updated_span

# Max number of quests in quest_log. A campaign spine plus sub-quests stays
# well under this; the cap exists purely to bound the Postgres state-bloat
# vector (32 small entries ~= 16 KB). Story 77-2.
_QUEST_LOG_CARDINALITY_CAP = 32


class RecordQuestArgs(BaseModel):
    quest_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "Stable quest identifier. A NEW id mints a quest; an EXISTING id "
            "updates it (status evolution). Mint as soon as a concrete objective "
            "forms; only a quiet scene with no objective should be left unminted."
        ),
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="Short player-facing quest title.",
    )
    objective: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="What the player must do — the concrete objective.",
    )
    status: str = Field(
        default="active",
        min_length=1,
        max_length=32,
        description="Quest status, e.g. active / completed / failed / resolved.",
    )
    anchor: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional beat/location id anchoring the quest (feeds the orbital "
            "course planner via quest_anchors)."
        ),
    )


@tool(
    name="record_quest",
    description=(
        "Mint a new quest or evolve an existing one for the campaign spine. "
        "Use a NEW quest_id to create a quest (title + objective); reuse an "
        "EXISTING quest_id to update its status. Optionally anchor the quest to "
        "a beat/location id. MINT when the story gains a real objective — when a "
        "giver names a task the player takes up, or the player commits to a goal "
        "(find X, settle a debt, rescue Y, reach Z). Promoting an objective in "
        "PROSE ALONE is not enough: the engine only tracks — and the player only "
        "sees in their quest log — quests minted through this tool. If you wrote a "
        "concrete objective into the narration, mint it here the same turn. (The "
        "only restraint: a quiet scene with no objective does not need a quest — "
        "do not mint mood or scenery.)"
    ),
    category=ToolCategory.WRITE,
)
async def record_quest(args: RecordQuestArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.repository.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot
    existing = snapshot.quest_log.get(args.quest_id)

    if existing is None:
        # MINT. Refuse loudly past the cardinality cap (No Silent Fallbacks) —
        # never silently drop the quest, never grow state unbounded.
        if len(snapshot.quest_log) >= _QUEST_LOG_CARDINALITY_CAP:
            return ToolResult.error(
                f"quest_log cardinality cap reached "
                f"({_QUEST_LOG_CARDINALITY_CAP}); cannot mint {args.quest_id!r}. "
                "Resolve or consolidate existing quests first."
            )
        anchor_count = 0
        snapshot.quest_log[args.quest_id] = QuestEntry(
            title=args.title,
            objective=args.objective,
            status=args.status,
            anchor_id=args.anchor,
        )
        if args.anchor:
            if args.anchor not in snapshot.quest_anchors:
                snapshot.quest_anchors.append(args.anchor)
            anchor_count = 1

        ctx.repository.save(snapshot)
        quest_created_span(
            quest_id=args.quest_id,
            title=args.title,
            source="narrator",
            anchor_count=anchor_count,
        )
        ctx.otel_span.set_attribute("tool.quest.quest_id", args.quest_id)
        ctx.otel_span.set_attribute("tool.quest.mode", "created")
        ctx.otel_span.set_attribute("tool.quest.anchor_count", anchor_count)
        return ToolResult.ok(
            {
                "quest_id": args.quest_id,
                "mode": "created",
                "title": args.title,
                "objective": args.objective,
                "status": args.status,
                "anchor": args.anchor,
            }
        )

    # UPDATE (evolve an existing quest). Allowed even at the cardinality cap.
    old_status = existing.status
    existing.title = args.title
    existing.objective = args.objective
    existing.status = args.status
    if args.anchor:
        existing.anchor_id = args.anchor
        if args.anchor not in snapshot.quest_anchors:
            snapshot.quest_anchors.append(args.anchor)

    ctx.repository.save(snapshot)
    quest_updated_span(
        quest_id=args.quest_id,
        old_status=old_status,
        new_status=args.status,
    )
    ctx.otel_span.set_attribute("tool.quest.quest_id", args.quest_id)
    ctx.otel_span.set_attribute("tool.quest.mode", "updated")
    ctx.otel_span.set_attribute("tool.quest.old_status", old_status)
    ctx.otel_span.set_attribute("tool.quest.new_status", args.status)
    return ToolResult.ok(
        {
            "quest_id": args.quest_id,
            "mode": "updated",
            "old_status": old_status,
            "new_status": args.status,
            "title": args.title,
            "objective": args.objective,
        }
    )
