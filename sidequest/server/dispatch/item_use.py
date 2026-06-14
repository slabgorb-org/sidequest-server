"""Story 106-4 Part C — confrontation item-use resolution.

A ``use_item:<slug>`` beat is an AUTO-SUCCESS (no d20) Main Action that consumes
a carried consumable and applies its effect (heal). Shared by the WN sealed-round
walk (``wn_round.run_wn_round``) and the legacy immediate hp_depletion path
(``dice.dispatch_dice_throw``) so the consume+effect lives in ONE place — the
opponent's answer (initiative slot or reprisal) is the caller's concern.

The heal magnitude is authored on the item (``heal_amount``, WWN-SRD ruling) and
rolled by the Part-A ``_apply_consumable_heal`` path, which also emits the
``state_patch.hp`` lie-detector. This module adds the ``confrontation.item_used``
span so the GM panel can see the item leave inventory.
"""

from __future__ import annotations

from sidequest.game.beat_filter import (
    ITEM_USE_BEAT_PREFIX,
    _is_consumable,
    item_slug,
)
from sidequest.game.character import Character
from sidequest.game.session import GameSnapshot
from sidequest.server.dispatch.downed_seam import DiceDispatchError


def resolve_item_use(
    snapshot: GameSnapshot, character_name: str, beat_id: str
) -> tuple[Character, int]:
    """Resolve the acting PC and the inventory index of the usable consumable
    named by an item-use beat.

    Fails loud (``DiceDispatchError``) when the beat is not an item-use beat,
    the actor is not a seated PC, or no carried consumable matches the slug —
    No Silent Fallbacks (a missing item is a client/content bug, never a quiet
    no-op that swallows the player's action).
    """
    if not beat_id.startswith(ITEM_USE_BEAT_PREFIX):
        raise DiceDispatchError(f"{beat_id!r} is not an item-use beat")
    slug = beat_id[len(ITEM_USE_BEAT_PREFIX) :]
    character = next((c for c in snapshot.characters if c.core.name == character_name), None)
    if character is None:
        raise DiceDispatchError(
            f"item-use beat {beat_id!r} committed by {character_name!r}, who is not a "
            "seated PC (No Silent Fallbacks)"
        )
    for idx, item in enumerate(character.core.inventory.items):
        name = str(item.get("name", "") or "")
        if item_slug(name) == slug and _is_consumable(item) and item.get("heal_amount"):
            return character, idx
    carried = ", ".join(
        item_slug(str(i.get("name", "") or "")) for i in character.core.inventory.items
    )
    raise DiceDispatchError(
        f"item-use beat {beat_id!r} names an item not in {character_name!r}'s "
        f"inventory — no usable consumable matches slug {slug!r} (carried: [{carried}])"
    )


def apply_item_use(
    *, character: Character, item_index: int, turn_num: int
) -> tuple[str, int | None]:
    """Apply the indexed consumable's effect to ``character``, consume it, and
    emit the ``confrontation.item_used`` GM-panel span.

    Returns ``(item_name, hp_restored)`` — ``hp_restored`` is the post-clamp HP
    gained, or None when the item carries no heal effect. The
    ``_apply_consumable_heal`` reuse emits ``state_patch.hp``
    (source=consumable_heal); this adds the item-left-inventory signal.
    """
    # Function-level import: narration_apply pulls in heavy server modules and
    # is imported by paths that import this one — defer to dodge a cycle.
    from sidequest.server.narration_apply import _apply_consumable_heal
    from sidequest.telemetry.spans import confrontation_item_used_span

    item = character.core.inventory.items[item_index]
    item_name = str(item.get("name", "") or "")
    healed = _apply_consumable_heal(
        item, character, player_name=character.core.name, turn_num=turn_num
    )
    character.core.inventory.items.pop(item_index)
    confrontation_item_used_span(
        actor=character.core.name,
        item=item_name,
        healed=int(healed or 0),
        hp_after=character.core.hp.current,
    )
    return item_name, healed
