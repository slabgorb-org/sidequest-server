"""Promote a significant item gained in play to an invokable Fate aspect (spec
2026-06-18-significant-items-invokable-fate-aspects-design.md).

The runtime sibling of chargen's ``compile_gear_onto_sheet``. It is deliberately
SEPARATE: the chargen compiler rebuilds the whole refresh/fate-point economy from
scratch (``sheet.fate_points = refresh_after``), which is destructive mid-session.
This promoter appends authored aspects/permissions onto a LIVE sheet WITHOUT
touching ``refresh``/``fate_points``. Matched-gear STUNTS are deferred (they debit
refresh — a milestone-advancement decision) and only counted, never applied.

The match mirrors ``resolve_gained_item_dict``'s conservative exact discipline
(id → slug → case-folded name); a partial name never binds. The back-link is the
inventory item's id on ``Aspect.source_gear`` (and the dedup key), so the GM panel
traces the aspect to the item and a re-grant is a logged no-op."""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import trace

from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.item_catalog_resolution import _slugify
from sidequest.genre.models.inventory import GearDef
from sidequest.telemetry.spans.fate import fate_item_promoted_span


@dataclass(frozen=True)
class ItemPromotionResult:
    """Outcome of a promotion attempt. ``promoted`` is True iff an aspect was
    actually appended (the inventory ``promoted`` flag tracks this)."""

    promoted: bool
    aspects_added: int
    source: str
    stunts_deferred: int
    deduped: bool


def match_gained_gear(item_id: str, item_name: str, gear_defs: list[GearDef]) -> GearDef | None:
    """Conservative exact match of a gained item against the Fate gear set —
    mirrors ``resolve_gained_item_dict`` (id → slug → case-folded name). Never
    fuzzy: "Silver" must not bind "Silver Shoes"."""
    if not gear_defs:
        return None
    name = " ".join(str(item_name or "").split())
    raw_id = str(item_id or "").strip()
    if raw_id.startswith("narrator:"):
        raw_id = raw_id[len("narrator:") :]
    by_id = {g.id: g for g in gear_defs}
    match = by_id.get(raw_id) if raw_id else None
    if match is None and name:
        slug = _slugify(name)
        match = by_id.get(slug)
    if match is None and name:
        by_name = {g.name.strip().casefold(): g for g in gear_defs}
        match = by_name.get(name.casefold())
    return match


def promote_gained_item(
    *,
    sheet: FateSheet,
    item_id: str,
    item_name: str,
    gear_defs: list[GearDef],
    actor: str = "",
    _tracer: trace.Tracer | None = None,
) -> ItemPromotionResult:
    """Promote a just-gained item to aspect(s) on ``sheet`` (Phase 1: catalog).

    Dedup first: a second grant of an already-promoted item is a logged no-op.
    Then match against ``gear_defs`` and append each ``grants_aspects`` entry as a
    character/permission Aspect (free_invokes 0, source_gear=item_id). Stunts on
    the matched gear are counted (``stunts_deferred``) but NOT applied."""
    # Dedup — never re-promote the same inventory item; log it (No Silent Fallbacks).
    if any(a.source_gear == item_id for a in sheet.aspects):
        fate_item_promoted_span(
            actor=actor,
            item_id=item_id,
            item_name=item_name,
            aspect_text="",
            source="",
            aspects_added=0,
            stunts_deferred=0,
            deduped=True,
            _tracer=_tracer,
        )
        return ItemPromotionResult(
            promoted=False, aspects_added=0, source="", stunts_deferred=0, deduped=True
        )

    gear = match_gained_gear(item_id, item_name, gear_defs)
    if gear is None:
        return ItemPromotionResult(
            promoted=False, aspects_added=0, source="", stunts_deferred=0, deduped=False
        )

    added = 0
    first_text = ""
    for grant in gear.grants_aspects:
        sheet.aspects.append(Aspect(text=grant.text, kind=grant.kind, source_gear=item_id))
        if not first_text:
            first_text = grant.text
        added += 1
    stunts_deferred = len(gear.grants_stunts)

    if added == 0 and stunts_deferred == 0:
        # Matched pure-flavor gear (a hat is a hat) — nothing to grant, no span.
        return ItemPromotionResult(
            promoted=False, aspects_added=0, source="catalog", stunts_deferred=0, deduped=False
        )

    fate_item_promoted_span(
        actor=actor,
        item_id=item_id,
        item_name=item_name,
        aspect_text=first_text,
        source="catalog",
        aspects_added=added,
        stunts_deferred=stunts_deferred,
        deduped=False,
        _tracer=_tracer,
    )
    return ItemPromotionResult(
        promoted=added > 0,
        aspects_added=added,
        source="catalog",
        stunts_deferred=stunts_deferred,
        deduped=False,
    )
