"""Pure combat-rules helpers — confrontation-def lookup + weapon-damage resolution.

ADR-147 (Honest Layering, story 122-2): these two functions are pure game logic
that historically sat under ``server.dispatch`` by port accident (the admission was
in ``native.py``'s own layer-inversion comment). They force the ruleset modules that
consume them (``native``, ``without_number`` + WN siblings) to import *upward* into
``server/``. Relocating them here — into the game tier, alongside the ``RulesetModule``
implementations — deletes those upward edges and the lazy in-method import workarounds
that dodged the resulting circular imports.

Behaviour is unchanged from the prior ``server.dispatch.confrontation`` /
``server.dispatch.damage_roll`` definitions; those modules now re-export these names
so existing server-tier callers keep working (server → game is the legal direction).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sidequest.genre.models.inventory import DamageSpec

if TYPE_CHECKING:
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import BeatDef, ConfrontationDef

logger = logging.getLogger(__name__)


def find_confrontation_def(
    defs: list[ConfrontationDef],
    encounter_type: str,
) -> ConfrontationDef | None:
    """Return the ConfrontationDef whose ``confrontation_type`` equals ``encounter_type``.

    Exact string match — mirrors Rust's ``iter().find(|d| d.type == ty)``.
    Returns ``None`` when no def matches; callers MUST handle the miss
    (CLAUDE.md: no silent fallback — caller decides whether to error).
    """
    for d in defs:
        if d.confrontation_type == encounter_type:
            return d
    return None


def resolve_damage_spec_from_beat_and_actor(
    *,
    beat: BeatDef,
    actor_core: object | None,
    pack: GenrePack | None,
    world_slug: str | None = None,
) -> DamageSpec | None:
    """Resolve the weapon DamageSpec for a strike beat.

    Resolution priority (CLAUDE.md no-silent-fallback — skip loudly, never fabricate):
    1. ``beat.damage_override`` — explicit spec on the beat (natural attack / creature).
    2. Actor's equipped weapon item dict carrying a ``damage`` dict (from inventory).
    3. Catalog lookup: find the actor's first equipped weapon item by id, then
       read ``CatalogItem.damage`` from the WORLD-RESOLVED item catalog —
       epic 94 moved item catalogs to the world tier for migrated packs, so
       the lookup goes through ``resolve_inventory(pack, world_slug)`` (world
       REPLACES genre; falsy/unknown world falls through to genre tier, which
       packs like caverns_and_claudes still ship). Story 96-1: this seam
       previously read only genre-tier ``pack.inventory``, which silently
       skipped strike damage for every migrated pack.
    4. ``pack.rules.unarmed_damage`` — genre-level unarmed-strike floor so an
       empty-handed hit still deals HP (mirrors ``opponent_damage`` for the
       enemy reprisal). None ⇒ no floor; caller logs and skips.
    5. No match — returns None; caller must log and skip.

    ``actor_core`` is the actor's ``CreatureCore`` (may be None for actors without
    a resolved core). ``pack`` is the live genre pack; ``world_slug`` is the
    session's bound world (drives the world-tier catalog resolution).
    """
    # Priority 1: beat-level override (natural attack, creature).
    if beat.damage_override is not None:
        return beat.damage_override

    # Priority 2 & 3: actor's inventory (skipped when the actor has no core or
    # no items — both fall through to the unarmed floor below).
    inventory_items: list[dict] = getattr(getattr(actor_core, "inventory", None), "items", [])

    # Priority 2: item dict already carries a serialised damage field.
    # (This path fires for materialised NPCs whose item dicts were built
    # with a ``damage`` key.)
    for item_dict in inventory_items:
        dmg_raw = item_dict.get("damage")
        if dmg_raw is not None:
            if isinstance(dmg_raw, dict):
                try:
                    return DamageSpec.model_validate(dmg_raw)
                except Exception:
                    logger.warning(
                        "damage_spec: item %r has unparseable damage dict %r — skipping",
                        item_dict.get("id"),
                        dmg_raw,
                    )
            elif isinstance(dmg_raw, str):
                try:
                    return DamageSpec.model_validate({"dice": dmg_raw})
                except Exception:
                    logger.warning(
                        "damage_spec: item %r has unparseable damage string %r — skipping",
                        item_dict.get("id"),
                        dmg_raw,
                    )

    # Priority 3: world-resolved catalog lookup by item id (epic 94: world
    # inventory REPLACES genre; falls through to genre tier when no world).
    catalog = None
    if pack is not None:
        from sidequest.game.inventory_resolve import resolve_inventory

        inv_config = resolve_inventory(pack, world_slug)
        if inv_config is not None:
            catalog = getattr(inv_config, "item_catalog", None)

    if catalog:
        catalog_by_id = {c.id: c for c in catalog}
        for item_dict in inventory_items:
            item_id = item_dict.get("id")
            if not item_id:
                continue
            catalog_item = catalog_by_id.get(item_id)
            if catalog_item is not None and catalog_item.damage is not None:
                return catalog_item.damage

    # Priority 4: genre-level unarmed-strike floor. Reached only when no weapon
    # resolved above, so an equipped weapon always wins and this never caps an
    # armed actor. None ⇒ no floor (caller logs ``damage_spec_missing`` + skips).
    rules = getattr(pack, "rules", None) if pack is not None else None
    unarmed = getattr(rules, "unarmed_damage", None) if rules is not None else None
    if unarmed is not None:
        return unarmed

    return None
