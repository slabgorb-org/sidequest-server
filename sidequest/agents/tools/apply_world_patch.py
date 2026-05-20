"""Tool: apply_world_patch — narrator escape hatch for world-state mutations.

Phase C Task 27 — WRITE tool. ADR-011 escape hatch.

What this tool is for
---------------------
This is the **last-resort escape hatch** that lets the narrator mutate
world state when no typed tool covers the mutation. Per ADR-011, every
narrator-declared state change should ideally route through a typed,
domain-aware tool (``apply_damage``, ``advance_scene_clue``,
``update_resource_pool``, ...) so that the GM panel sees a structured
record. The escape hatch exists because the Phase C surface is not yet
complete — some narrator intents have no typed home — and because
genre-specific worlds will always grow new state fields faster than
typed tools.

Deprecation criterion
---------------------
Per ADR-011 the deprecation target is **zero ``apply_world_patch`` spans
across 10 consecutive playtests**. Hitting that threshold means every
mutation the narrator wants to make has a typed home. To make that
threshold measurable, **every invocation is heavily OTEL-instrumented**,
including rejections (unsupported paths, type mismatches). The GM panel
counts spans, not successes — a rejected escape-hatch attempt is just
as much a signal as a successful one that the typed surface has a gap.

v1 scope (deliberately narrow)
------------------------------
The narrator's JSON-pointer ``path`` argument is the conceptual
interface; the implementation maps a small allowlist of top-level paths
onto :class:`sidequest.game.session.WorldStatePatch` fields and calls
:meth:`GameSnapshot.apply_world_patch`. Anything outside the allowlist
is a **recoverable error** (the narrator can pick a typed tool instead).

Supported paths (v1):

- ``/location`` → ``WorldStatePatch.location`` (str)
- ``/time_of_day`` → ``WorldStatePatch.time_of_day`` (str)
- ``/atmosphere`` → ``WorldStatePatch.atmosphere`` (str)
- ``/current_region`` → ``WorldStatePatch.current_region`` (str)
- ``/active_stakes`` → ``WorldStatePatch.active_stakes`` (str)

These are the five string-valued top-level world fields that have no
dedicated typed tool yet. Other ``WorldStatePatch`` fields — ``hp_changes``
(use ``apply_damage``), ``npc_attitudes`` (use ``update_npc_disposition``),
``quest_log``/``quest_updates``, ``discovered_regions``, ``npcs_present``,
``lore_established``, ``discovered_facts`` — are intentionally **not**
exposed through the escape hatch because they have, or will have, typed
homes; routing them here would defeat the deprecation telemetry.

Path support widens only when a real narrator playtest demonstrates a
mutation that has no typed home and no path-allowlist entry. Until
then, every "I need to set X" the narrator surfaces is a vote either
for adding X to the allowlist or for building a typed tool for X.

Why "recoverable" for unsupported paths
---------------------------------------
A rejection isn't fatal — the narrator can adjust its plan and call a
typed tool on the next turn. Fatal would force the orchestrator to abort
the whole turn, which is the wrong shape for "this escape hatch can't
serve this mutation".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sidequest.agents.narrator_guardrails import LOCATION_PATCH_CONSTRAINT
from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.session import WorldStatePatch


class ApplyWorldPatchArgs(BaseModel):
    path: str = Field(
        ...,
        min_length=1,
        description=(
            "JSON-pointer-style path to the world field being mutated. "
            "v1 supports: '/location', '/time_of_day', '/atmosphere', "
            "'/current_region', '/active_stakes'. Unsupported paths return "
            "a recoverable error — pick a typed tool instead."
        ),
    )
    value: Any = Field(
        ...,
        description=(
            "New value for the path. Type depends on path — all v1 paths expect a string."
        ),
    )
    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Narrator's justification for using the escape hatch. Required "
            "for deprecation tracking (ADR-011): the GM panel reviews "
            "reasons to find recurring gaps that should become typed tools."
        ),
    )


# Allowlist: JSON-pointer path -> WorldStatePatch field name.
# Keep narrow. Each addition is a deliberate decision that the narrator
# needs this field AND no typed tool covers it.
_SUPPORTED_PATHS: dict[str, str] = {
    "/location": "location",
    "/time_of_day": "time_of_day",
    "/atmosphere": "atmosphere",
    "/current_region": "current_region",
    "/active_stakes": "active_stakes",
}


def _path_kind(path: str) -> str:
    """Extract the first segment of a JSON-pointer path for OTEL tagging.

    ``/location`` → ``location``; malformed → ``invalid``. This is the
    grouping key the GM panel uses to cluster escape-hatch spans across
    a playtest run; we want it set for every span, even on rejection.
    """
    if not path.startswith("/"):
        return "invalid"
    rest = path[1:]
    if not rest:
        return "invalid"
    return rest.split("/", 1)[0]


@tool(
    name="apply_world_patch",
    description=(
        "Apply a JSON-patch-style mutation to world state. Escape hatch "
        "only — prefer a typed tool when one exists. Heavily logged; "
        "deprecation criterion is zero spans across 10 consecutive "
        "playtests.\n\n"
        # ADR-111 (story 57-4): the location_patch guardrail migrated from
        # the per-turn Recency zone into this tool's description, the
        # cache-key surface adjacent to the artifact it governs
        # (``WorldStatePatch.location``). Single source of truth:
        # sidequest.agents.narrator_guardrails.LOCATION_PATCH_CONSTRAINT.
        f"{LOCATION_PATCH_CONSTRAINT}"
    ),
    category=ToolCategory.WRITE,
)
async def apply_world_patch(args: ApplyWorldPatchArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    snapshot = session.snapshot

    path_kind = _path_kind(args.path)
    field_name = _SUPPORTED_PATHS.get(args.path)

    # Heavy OTEL regardless of outcome — the deprecation criterion counts
    # *every* invocation, including rejections. A rejected escape-hatch
    # call is itself a signal that the typed surface has a gap.
    ctx.otel_span.set_attribute("tool.world_patch.path", args.path)
    ctx.otel_span.set_attribute("tool.world_patch.reason", args.reason)
    ctx.otel_span.set_attribute("tool.world_patch.path_kind", path_kind)
    ctx.otel_span.set_attribute("tool.world_patch.supported", field_name is not None)

    if field_name is None:
        return ToolResult.error(
            f"path {args.path!r} not supported by v1 apply_world_patch escape hatch; "
            f"supported paths: {sorted(_SUPPORTED_PATHS.keys())!r}. "
            "Prefer a typed tool when one exists.",
            recoverable=True,
        )

    # v1: all supported paths are string-valued. Reject non-strings with
    # a recoverable error so the narrator can re-cast the call.
    if not isinstance(args.value, str):
        return ToolResult.error(
            f"path {args.path!r} expects a string value; got {type(args.value).__name__}",
            recoverable=True,
        )

    # Explicit dispatch (rather than ``WorldStatePatch(**{field_name: value})``)
    # so pyright can see each branch's kwarg type. Mirror of _SUPPORTED_PATHS.
    if field_name == "location":
        patch = WorldStatePatch(location=args.value)
    elif field_name == "time_of_day":
        patch = WorldStatePatch(time_of_day=args.value)
    elif field_name == "atmosphere":
        patch = WorldStatePatch(atmosphere=args.value)
    elif field_name == "current_region":
        patch = WorldStatePatch(current_region=args.value)
    elif field_name == "active_stakes":
        patch = WorldStatePatch(active_stakes=args.value)
    else:
        # Unreachable: _SUPPORTED_PATHS is the only source of field_name.
        # Fail loudly if the allowlist and dispatch drift.
        return ToolResult.error(
            f"internal: supported path {args.path!r} mapped to unknown field {field_name!r}",
            recoverable=False,
        )
    snapshot.apply_world_patch(patch)
    ctx.store.save(snapshot)

    return ToolResult.ok(
        {
            "path": args.path,
            "value": args.value,
            "reason": args.reason,
            "applied_field": field_name,
        }
    )
