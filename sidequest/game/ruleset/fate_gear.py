"""Fate gear compilation — gear.yaml -> FateSheet at chargen (ADR-144 gear model,
story 114-10; design 2026-06-15-fate-gear-model-design.md).

Fate "gear" is a thin authoring shim, not a carried inventory: a piece of gear
compiles into the existing ``FateSheet`` at character creation. Each
``grants_aspects`` entry becomes an ``Aspect`` (carrying its ``kind`` +
``source_gear``); each ``grants_stunts`` entry a ``Stunt`` (carrying
``source_gear``). Then ``refresh`` is recomputed from the invariant:

    refresh == base_refresh − max(0, total_stunts − free_stunts)   (SRD floor 1)

The whole balance story (SOUL → Bind the Ruleset, Don't Balance It):
  - aspect-gear and permission-gear are FREE — they never debit refresh.
  - stunt-gear MUST debit refresh once the free-stunt allotment is exhausted.

``compile_gear_onto_sheet`` does the chargen materialization + refresh invariant +
``fate.gear_compiled`` span. Genre-tier gear loads via ``loader._load_gear`` →
``GenrePack.gear`` → ``FateConfig.gear_catalog``. World-tier gear
(``worlds/<slug>/gear.yaml`` → ``World.gear``) loads alongside it (story 126-25);
at runtime ``resolve_fate_gear_catalog`` UNIONS the two by id (world wins — the
ADR-145 §D3 paradigm-neutral by-id merge the inventory path uses) so the #945
item-promoter sees a world's found-items (e.g. Oz's silver shoes) when that world
is active."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.game.fate_sheet import Aspect, FateSheet, Stunt
from sidequest.genre.models.inventory import GearDef
from sidequest.telemetry.spans.fate import fate_gear_compiled_span

if TYPE_CHECKING:
    from sidequest.genre.models.pack import GenrePack

#: SRD floor — a character's refresh can never drop below this, no matter how
#: much stunt-gear an archetype bundles.
REFRESH_FLOOR = 1


class GearCompileError(ValueError):
    """A gear-compile operation referenced a gear id absent from the genre-tier
    GearDef set. Fail loud — No Silent Fallbacks (SOUL.md / ADR-144)."""


@dataclass(frozen=True)
class GearCompileResult:
    """The outcome of compiling an archetype's gear onto a FateSheet."""

    gear_ids: tuple[str, ...]
    aspects_placed: int
    stunts_added: int
    permission_aspects: int
    refresh_before: int
    refresh_after: int
    refresh_debited: int


def compile_gear_onto_sheet(
    sheet: FateSheet,
    *,
    archetype: str,
    gear_ids: list[str],
    gear_defs: list[GearDef],
    base_refresh: int,
    free_stunts: int,
    actor: str = "",
    _tracer: trace.Tracer | None = None,
) -> GearCompileResult:
    """Materialize an archetype's starting gear onto ``sheet`` and apply the
    refresh invariant. Mutates ``sheet`` (appends aspects/stunts, sets refresh).

    Fails loud BEFORE any mutation if a gear id is not in ``gear_defs`` (No Silent
    Fallbacks) — a failed compile leaves the sheet untouched."""
    by_id = {gear.id: gear for gear in gear_defs}

    # Resolve ALL ids first — fail loud before mutating so a bad id never leaves
    # a half-applied sheet.
    resolved: list[GearDef] = []
    for gear_id in gear_ids:
        gear = by_id.get(gear_id)
        if gear is None:
            raise GearCompileError(
                f"archetype {archetype!r} references unknown gear id {gear_id!r} "
                f"(available: {sorted(by_id)})"
            )
        resolved.append(gear)

    authored_stunts = len(sheet.stunts)
    aspects_placed = 0
    permission_aspects = 0
    stunts_added = 0
    for gear in resolved:
        for grant in gear.grants_aspects:
            sheet.aspects.append(Aspect(text=grant.text, kind=grant.kind, source_gear=gear.id))
            aspects_placed += 1
            if grant.kind == "permission":
                permission_aspects += 1
        for grant in gear.grants_stunts:
            sheet.stunts.append(
                Stunt(name=grant.name, description=grant.description, source_gear=gear.id)
            )
            stunts_added += 1

    total_stunts = authored_stunts + stunts_added
    refresh_after = max(REFRESH_FLOOR, base_refresh - max(0, total_stunts - free_stunts))
    # refresh_debited is the ACTUAL refresh removed (post-SRD-floor), i.e.
    # refresh_before − refresh_after — not the unclamped stunt overrun. When the
    # floor clamps refresh_after to REFRESH_FLOOR, the debit reported is the real
    # reduction the sheet took, which is what the GM panel should see.
    refresh_debited = base_refresh - refresh_after
    sheet.refresh = refresh_after
    # SRD: a character starts a session with fate points == refresh (the seed set
    # this from base_refresh; keep it consistent after the stunt-gear debit).
    sheet.fate_points = refresh_after

    fate_gear_compiled_span(
        archetype=archetype,
        actor=actor,
        gear_ids=",".join(gear_ids),
        aspects_placed=aspects_placed,
        stunts_added=stunts_added,
        permission_aspects=permission_aspects,
        refresh_before=base_refresh,
        refresh_after=refresh_after,
        refresh_debited=refresh_debited,
        _tracer=_tracer,
    )
    return GearCompileResult(
        gear_ids=tuple(gear_ids),
        aspects_placed=aspects_placed,
        stunts_added=stunts_added,
        permission_aspects=permission_aspects,
        refresh_before=base_refresh,
        refresh_after=refresh_after,
        refresh_debited=refresh_debited,
    )


def resolve_fate_gear_catalog(pack: GenrePack | None, world_slug: str | None) -> list[GearDef]:
    """Resolve the effective Fate gear catalog for the active world (story 126-25).

    The genre-tier ``rules.fate.gear_catalog`` UNIONED with the active world's
    ``World.gear`` by id — world wins on a shared id (ADR-145 §D3, the same by-id
    rule ``resolve_inventory`` applies to the item catalog). A falsy/unknown world,
    or a world that authors no gear, resolves to the pure genre baseline. World
    gear loads regardless of ruleset; this merge is the fate-gated step (the only
    caller invokes it for a character that has a FateSheet). The runtime sibling of
    the chargen-tier ``compile_gear_onto_sheet`` lookup — both read GearDefs, but
    the promoter (#945) needs the WORLD's found-items in scope when that world is
    active, which the genre-only ``gear_catalog`` could never supply."""
    fate_cfg = pack.rules.fate if pack is not None else None
    genre_gear: list[GearDef] = list(fate_cfg.gear_catalog) if fate_cfg is not None else []

    world = pack.worlds.get(world_slug) if (pack is not None and world_slug) else None
    world_gear: list[GearDef] = list(getattr(world, "gear", []) or []) if world is not None else []
    if not world_gear:
        # Pure genre baseline (no world, unknown world, or world ships no gear).
        return genre_gear

    merged_by_id: dict[str, GearDef] = {g.id: g for g in genre_gear}
    overridden = sum(1 for g in world_gear if g.id in merged_by_id)
    for world_def in world_gear:
        merged_by_id[world_def.id] = world_def  # world wins on a shared id
    merged = list(merged_by_id.values())

    _emit_fate_gear_merged(
        world_slug=world_slug or "",
        genre_count=len(genre_gear),
        world_gear_ids=[g.id for g in world_gear],
        overridden=overridden,
        merged_count=len(merged),
    )
    return merged


def _emit_fate_gear_merged(
    *,
    world_slug: str,
    genre_count: int,
    world_gear_ids: list[str],
    overridden: int,
    merged_count: int,
) -> None:
    """Emit a ``state_transition`` watcher event for the world∪genre Fate gear
    merge (OTEL Observability Principle + ADR-145 §D3): the GM panel can confirm a
    world's authored gear was WIRED into the effective catalog — naming the active
    world and the world gear ids that merged — not merely authored. Mirrors
    ``inventory_resolve._emit_inventory_merged``."""
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "resolved_fate_gear",
            "op": "merged",
            "world_slug": world_slug,
            "tier": "world",
            "genre_count": genre_count,
            "world_gear_ids": world_gear_ids,
            "overridden": overridden,
            "merged_count": merged_count,
        },
        component="genre",
    )
