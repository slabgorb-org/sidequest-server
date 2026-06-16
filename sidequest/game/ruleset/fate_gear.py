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
``fate.gear_compiled`` span. Gear is loaded at the **genre tier only** today
(``loader._load_gear`` → ``GenrePack.gear`` → ``FateConfig.gear_catalog``); a
world-tier ``gear.yaml`` merge (the ADR-145 §D3 paradigm-neutral by-id merge the
inventory path uses) is a future story — no pack authors world-distinct gear yet,
and the design defers the one example (Oz's silver shoes) to mid-game placement."""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import trace

from sidequest.game.fate_sheet import Aspect, FateSheet, Stunt
from sidequest.genre.models.inventory import GearDef
from sidequest.telemetry.spans.fate import fate_gear_compiled_span

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
