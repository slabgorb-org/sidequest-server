"""Inventory spans — narrator-extracted item mutations."""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_INVENTORY_NARRATOR_EXTRACTED = "inventory.narrator_extracted"
SPAN_ROUTES[SPAN_INVENTORY_NARRATOR_EXTRACTED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "inventory",
        "op": "narrator_extracted",
        "gained": (span.attributes or {}).get("gained_json", "[]"),
        "lost": (span.attributes or {}).get("lost_json", "[]"),
        "discarded": (span.attributes or {}).get("discarded_json", "[]"),
        "consumed": (span.attributes or {}).get("consumed_json", "[]"),
        "preserved": (span.attributes or {}).get("preserved_json", "[]"),
        "gained_count": (span.attributes or {}).get("gained_count", 0),
        "lost_count": (span.attributes or {}).get("lost_count", 0),
        "discarded_count": (span.attributes or {}).get("discarded_count", 0),
        "consumed_count": (span.attributes or {}).get("consumed_count", 0),
        "preserved_count": (span.attributes or {}).get("preserved_count", 0),
        "player_name": (span.attributes or {}).get("player_name", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)


@contextmanager
def inventory_narrator_extracted_span(
    *,
    gained: list[str],
    lost: list[str],
    player_name: str,
    turn_number: int,
    discarded: list[str] | None = None,
    consumed: list[str] | None = None,
    preserved: list[str] | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Lists are JSON-encoded — OTEL silently drops list attribute values.

    ``discarded``: state transitioned out of Carried (still in inventory).
    ``consumed``: removed because used up (one-shot consumables).
    ``preserved``: matched a narrator ``items_consumed`` entry but was NOT
    removed because it is a reusable item (tool/weapon/armor/quest) — the
    consume lane is single-use only, so the GM panel reads a non-empty
    ``preserved`` as "the engine refused to destroy a reusable item the
    narrator described using" (playtest 2026-06-04 handkerchief fix).
    """
    discarded_list = list(discarded or [])
    consumed_list = list(consumed or [])
    preserved_list = list(preserved or [])
    attributes: dict[str, Any] = {
        "gained_json": _json.dumps(list(gained)),
        "lost_json": _json.dumps(list(lost)),
        "discarded_json": _json.dumps(discarded_list),
        "consumed_json": _json.dumps(consumed_list),
        "preserved_json": _json.dumps(preserved_list),
        "gained_count": len(gained),
        "lost_count": len(lost),
        "discarded_count": len(discarded_list),
        "consumed_count": len(consumed_list),
        "preserved_count": len(preserved_list),
        "player_name": player_name,
        "turn_number": turn_number,
        **attrs,
    }
    with Span.open(
        SPAN_INVENTORY_NARRATOR_EXTRACTED,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# Story 82-8 (ADR-021 track 3) — wealth-tier resolution. Fires once per
# player-facing inventory projection when a character's gold balance resolves
# against the pack's authored ``progression.wealth_tiers``. The GM panel reads
# it as proof the wealth tier was engine-resolved (e.g. "stocked") rather than
# the narrator improvising a wealth descriptor. No span fires when the pack
# authors no tiers — there is nothing to resolve, and a fired span would be a
# lie of a resolution that never happened.
# ---------------------------------------------------------------------------

SPAN_INVENTORY_WEALTH_TIER = "inventory.wealth_tier"
SPAN_ROUTES[SPAN_INVENTORY_WEALTH_TIER] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "inventory.wealth_tier",
        "player_name": (span.attributes or {}).get("player_name", ""),
        "gold": (span.attributes or {}).get("gold", 0),
        "label": (span.attributes or {}).get("label", ""),
        "tier_index": (span.attributes or {}).get("tier_index", -1),
        "currency_name": (span.attributes or {}).get("currency_name", ""),
    },
)


@contextmanager
def inventory_wealth_tier_span(
    *,
    player_name: str,
    gold: int,
    label: str,
    tier_index: int,
    currency_name: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """One span per wealth-tier resolution on the player-facing inventory.

    ``tier_index`` is the resolved tier's position in the authored ladder
    (0 = poorest), ``label`` the tier's display label, ``gold`` the balance
    that resolved it. ``currency_name`` is the genre's currency noun for
    context (the GM panel shows "credits"/"Salvage"/"gold").
    """
    with Span.open(
        SPAN_INVENTORY_WEALTH_TIER,
        {
            "player_name": player_name,
            "gold": int(gold),
            "label": label,
            "tier_index": int(tier_index),
            "currency_name": currency_name,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# ---------------------------------------------------------------------------
# Story 71-15 (ADR-055) — movement-consumed resource depletion.
# Fires once per item burned on a room-graph transition (torch model). The
# GM panel reads it as proof the dungeon clock is ablating resources rather
# than the narrator improvising an inexhaustible light source.
# ---------------------------------------------------------------------------

SPAN_ITEM_RESOURCE_DEPLETED = "item.resource_depleted"
SPAN_ROUTES[SPAN_ITEM_RESOURCE_DEPLETED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "inventory",
        "op": "resource_depleted",
        "item": (span.attributes or {}).get("item"),
        "before": (span.attributes or {}).get("before"),
        "after": (span.attributes or {}).get("after"),
        "exhausted": (span.attributes or {}).get("exhausted", False),
        "actor": (span.attributes or {}).get("actor", ""),
    },
)


# ---------------------------------------------------------------------------
# NL-equip (ADR-113 / Zork-Problem) — the IntentRouter classifies a "lace on /
# wear / draw / take off" action into an ``equip`` dispatch; the engine flips
# the item's ``equipped`` flag deterministically. ``equip.resolved`` (INFO)
# fires when an item is resolved + flipped; ``equip.unresolved`` (ERROR) when
# the named item / acting PC can't be resolved (fail-loud, never a silent
# no-op). The GM panel reads these as proof an equip engaged the engine vs.
# the narrator merely describing the PC putting something on.
# ---------------------------------------------------------------------------

SPAN_EQUIP_RESOLVED = "equip.resolved"
SPAN_ROUTES[SPAN_EQUIP_RESOLVED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "inventory",
        "op": "equip.resolved",
        "pc_name": (span.attributes or {}).get("pc_name", ""),
        "item_id": (span.attributes or {}).get("item_id", ""),
        "item_name": (span.attributes or {}).get("item_name", ""),
        "action": (span.attributes or {}).get("action", ""),
        "equipped_before": (span.attributes or {}).get("equipped_before"),
        "equipped_after": (span.attributes or {}).get("equipped_after"),
        "changed": (span.attributes or {}).get("changed", False),
        "matched_by": (span.attributes or {}).get("matched_by", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)

SPAN_EQUIP_UNRESOLVED = "equip.unresolved"
SPAN_ROUTES[SPAN_EQUIP_UNRESOLVED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "inventory",
        "op": "equip.unresolved",
        "pc_name": (span.attributes or {}).get("pc_name", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "requested_item": (span.attributes or {}).get("requested_item", ""),
        "action": (span.attributes or {}).get("action", ""),
        "turn_number": (span.attributes or {}).get("turn_number", 0),
    },
)


@contextmanager
def equip_resolved_span(
    *,
    pc_name: str,
    item_id: str,
    item_name: str,
    action: str,
    equipped_before: bool,
    equipped_after: bool,
    changed: bool,
    matched_by: str,
    turn_number: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """One span per resolved equip/unequip — the item was found in the acting
    PC's inventory and its ``equipped`` flag set to ``equipped_after``.
    ``changed`` is False on an idempotent re-equip (already in target state).
    ``turn_number`` is ``snapshot.turn_manager.interaction`` so the dashboard
    grids this span to the correct turn column (Bug A fix)."""
    with Span.open(
        SPAN_EQUIP_RESOLVED,
        {
            "pc_name": pc_name,
            "item_id": item_id,
            "item_name": item_name,
            "action": action,
            "equipped_before": equipped_before,
            "equipped_after": equipped_after,
            "changed": changed,
            "matched_by": matched_by,
            "turn_number": turn_number,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def equip_unresolved_span(
    *,
    pc_name: str,
    reason: str,
    requested_item: str,
    action: str,
    turn_number: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fail-loud equip span: the named item or acting PC could not be resolved
    (``item_not_found`` / ``no_character`` / ``no_item_named``). ERROR status
    so the GM panel surfaces it as a failure, never a silent stay-as-is.
    ``turn_number`` is ``snapshot.turn_manager.interaction`` so the dashboard
    grids this span to the correct turn column (Bug A fix)."""
    with Span.open(
        SPAN_EQUIP_UNRESOLVED,
        {
            "pc_name": pc_name,
            "reason": reason,
            "requested_item": requested_item,
            "action": action,
            "turn_number": turn_number,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        span.set_status(Status(StatusCode.ERROR, reason))
        yield span


@contextmanager
def item_resource_depleted_span(
    *,
    item: str,
    before: int,
    after: int,
    exhausted: bool,
    actor: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """One span per movement-consumed item burned on a room-graph transition.

    ``before`` / ``after`` are the ``uses_remaining`` count either side of the
    decrement; ``exhausted`` is True when ``after == 0``.
    """
    with Span.open(
        SPAN_ITEM_RESOURCE_DEPLETED,
        {
            "item": item,
            "before": before,
            "after": after,
            "exhausted": exhausted,
            "actor": actor,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


# Chargen armor derivation (story 106-1) — the post-loadout step equips the
# kit-rolled armor and recomputes ``CreatureCore.armor_class`` from the equipped
# armor item's catalog ``armor_class`` (content-sourced, WWN SRD). The GM panel
# reads these as proof the derivation fired (vs. every Warrior silently fighting
# at the unarmored AC 10). ``chargen.armor_equipped`` (INFO) on a successful
# derive; ``chargen.armor_unresolved`` (WARN) when an armor item carries no
# catalog ``armor_class`` to derive from — the No-Silent-Fallback gate. WARN
# (not ERROR) matches the sibling content-gap convention
# (``chargen.starting_equipment_missing``): an unvalued armor entry is a content
# authoring gap surfaced loudly at chargen, not a runtime engine failure like
# ``equip.unresolved``.
SPAN_CHARGEN_ARMOR_EQUIPPED = "chargen.armor_equipped"
SPAN_ROUTES[SPAN_CHARGEN_ARMOR_EQUIPPED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "armor_class",
        "op": "chargen.armor_equipped",
        "pc_name": (span.attributes or {}).get("pc_name", ""),
        "item_id": (span.attributes or {}).get("item_id", ""),
        "item_name": (span.attributes or {}).get("item_name", ""),
        "armor_class": (span.attributes or {}).get("armor_class"),
        "ac_before": (span.attributes or {}).get("ac_before"),
        "ac_after": (span.attributes or {}).get("ac_after"),
        "equipped_after": (span.attributes or {}).get("equipped_after"),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "player_id": (span.attributes or {}).get("player_id", ""),
    },
)

SPAN_CHARGEN_ARMOR_UNRESOLVED = "chargen.armor_unresolved"
SPAN_ROUTES[SPAN_CHARGEN_ARMOR_UNRESOLVED] = SpanRoute(
    event_type="state_transition",
    component="inventory",
    extract=lambda span: {
        "field": "armor_class",
        "op": "chargen.armor_unresolved",
        "pc_name": (span.attributes or {}).get("pc_name", ""),
        "item_id": (span.attributes or {}).get("item_id", ""),
        "item_name": (span.attributes or {}).get("item_name", ""),
        "reason": (span.attributes or {}).get("reason", ""),
        "genre": (span.attributes or {}).get("genre", ""),
        "world": (span.attributes or {}).get("world", ""),
        "player_id": (span.attributes or {}).get("player_id", ""),
    },
)


@contextmanager
def chargen_armor_equipped_span(
    *,
    item_id: str,
    item_name: str,
    armor_class: int,
    ac_before: int,
    ac_after: int,
    equipped_after: bool,
    pc_name: str = "",
    genre: str = "",
    world: str = "",
    player_id: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """One span per kit armor item equipped + AC-derived at chargen. ``armor_class``
    is the equipped item's catalog value; ``ac_before``/``ac_after`` bracket the
    recompute of ``CreatureCore.armor_class`` (10 → derived)."""
    with Span.open(
        SPAN_CHARGEN_ARMOR_EQUIPPED,
        {
            "item_id": item_id,
            "item_name": item_name,
            "armor_class": armor_class,
            "ac_before": ac_before,
            "ac_after": ac_after,
            "equipped_after": equipped_after,
            "pc_name": pc_name,
            "genre": genre,
            "world": world,
            "player_id": player_id,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def chargen_armor_unresolved_span(
    *,
    item_id: str,
    item_name: str,
    reason: str,
    pc_name: str = "",
    genre: str = "",
    world: str = "",
    player_id: str = "",
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Fail-loud chargen armor span: an armor item carries no catalog ``armor_class``
    to derive from. Logged at WARNING (the GM panel surfaces the content gap at
    chargen rather than the PC silently fighting unarmored) — WARN, not ERROR, to
    match the sibling content-gap span ``chargen.starting_equipment_missing``. An
    unvalued armor entry is a content authoring gap, not a runtime engine failure
    like ``equip.unresolved`` (which is ERROR)."""
    with Span.open(
        SPAN_CHARGEN_ARMOR_UNRESOLVED,
        {
            "item_id": item_id,
            "item_name": item_name,
            "reason": reason,
            "pc_name": pc_name,
            "genre": genre,
            "world": world,
            "player_id": player_id,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
