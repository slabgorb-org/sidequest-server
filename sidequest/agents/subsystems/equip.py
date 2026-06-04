"""equip subsystem dispatch handler — Intent Router live engager (ADR-113).

The Zork-Problem (SOUL): the player articulates an equip in natural language
("lace on the silver shoes", "draw the sword", "take off the cloak") with no
menu, and the engine adjudicates it. The IntentRouter classifies the action
into a ``SubsystemDispatch`` with ``subsystem="equip"`` and
``params={"item": "<name-or-id>", "action": "equip"|"unequip"}``. This handler
resolves the named item against the acting PC's inventory and flips its
``equipped`` flag deterministically BEFORE the narrator runs, so the narrator
sees already-real state.

``equipped`` is the equip axis; the separate ``state`` field is the
Carried/Discarded axis (an equipped item stays ``state="Carried"`` so it
remains in the carried/equipment view — see ``server/views.py``). This handler
touches ONLY ``equipped``.

No fallbacks, fail LOUD: a missing acting PC, a blank item, or an item the PC
isn't carrying emits an ERROR-level ``equip.unresolved`` span + an honest
narrator surface directive and applies NO mutation. A resolved equip emits a
non-error ``equip.resolved`` span (the GM-panel lie-detector).
"""

from __future__ import annotations

import logging

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch, VisibilityTag
from sidequest.telemetry.spans import equip_resolved_span, equip_unresolved_span

logger = logging.getLogger(__name__)


def _match_item(items: list[dict], ref: str) -> tuple[dict | None, str]:
    """Resolve a player-named item reference against the inventory.

    Resolution order (deterministic): exact id → exact case-insensitive name →
    unique case-insensitive substring (either direction). A substring that
    matches more than one item is ambiguous and resolves to None (fail loud
    rather than guess which item the player meant). Returns
    ``(item_dict | None, matched_by)``.
    """
    ref_l = ref.strip().lower()
    if not ref_l:
        return None, ""
    for item in items:
        if str(item.get("id", "")).lower() == ref_l:
            return item, "id"
    for item in items:
        if str(item.get("name", "")).strip().lower() == ref_l:
            return item, "name"
    substring = [
        item
        for item in items
        if (name := str(item.get("name", "")).strip().lower()) and (ref_l in name or name in ref_l)
    ]
    if len(substring) == 1:
        return substring[0], "substring"
    return None, ""


async def run_equip_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    player_name: str,
    turn_number: int | None = None,
) -> SubsystemOutput:
    """Flip the ``equipped`` flag of the player-named item in THIS PC's
    inventory. Returns an empty-directives ``SubsystemOutput`` on success
    (the flag is the engine truth; the narrator describes the act). On a
    recoverable failure returns a single ``must_narrate`` directive surfacing
    the truth + ``data["error"]`` and applies NO mutation.
    """
    requested_item = str(dispatch.params.get("item", "") or "").strip()
    action = str(dispatch.params.get("action", "") or "").strip().lower() or "equip"
    # equip (the default — "lace on / wear / draw") → True; unequip → False.
    target_equipped = action != "unequip"

    character = next((c for c in snapshot.characters if c.core.name == player_name), None)
    # Prefer the effective turn number the dispatch bank threads in (the value
    # turn_complete will emit, == interaction+1 for a player turn); fall back to
    # the snapshot's pre-increment interaction for direct callers that omit it.
    # Reading interaction here alone produced the off-by-one that left the
    # GM-panel inventory row dark on the resolving turn (DRIVER 2026-06-04).
    if turn_number is not None:
        _turn_number = int(turn_number)
    else:
        _tm = getattr(snapshot, "turn_manager", None)
        _turn_number = int(getattr(_tm, "interaction", 0)) if _tm is not None else 0

    if character is None:
        # The acting PC has no seated character — a binding bug. Fail loud.
        return _unresolved(
            pc_name=player_name,
            reason="no_character",
            requested_item=requested_item,
            action=action,
            surface="The way ahead is unknown — your bearings have not been set.",
            turn_number=_turn_number,
        )

    if not requested_item:
        return _unresolved(
            pc_name=player_name,
            reason="no_item_named",
            requested_item="",
            action=action,
            surface=f"{player_name} reaches to {action} something, but names nothing to {action}.",
            turn_number=_turn_number,
        )

    matched, matched_by = _match_item(character.core.inventory.items, requested_item)
    if matched is None:
        return _unresolved(
            pc_name=player_name,
            reason="item_not_found",
            requested_item=requested_item,
            action=action,
            surface=f"{player_name} is not carrying any {requested_item} to {action}.",
            turn_number=_turn_number,
        )

    before = bool(matched.get("equipped", False))
    matched["equipped"] = target_equipped
    # NOTE: ``state`` is intentionally NOT touched — it is the Carried/Discarded
    # axis, and an equipped item must remain "Carried" to stay in the carried
    # view (server/views.py filters carried on state == "Carried").
    changed = before != target_equipped

    item_id = str(matched.get("id", ""))
    item_name = str(matched.get("name", requested_item))
    with equip_resolved_span(
        pc_name=player_name,
        item_id=item_id,
        item_name=item_name,
        action=action,
        equipped_before=before,
        equipped_after=target_equipped,
        changed=changed,
        matched_by=matched_by,
        turn_number=_turn_number,
    ):
        pass
    logger.debug(
        "equip.resolved pc=%s item=%s action=%s before=%s after=%s changed=%s matched_by=%s",
        player_name,
        item_name,
        action,
        before,
        target_equipped,
        changed,
        matched_by,
    )
    return SubsystemOutput(
        data={
            "item_id": item_id,
            "item_name": item_name,
            "equipped": target_equipped,
            "changed": changed,
            "matched_by": matched_by,
        }
    )


def _unresolved(
    *,
    pc_name: str,
    reason: str,
    requested_item: str,
    action: str,
    surface: str,
    turn_number: int = 0,
) -> SubsystemOutput:
    """Emit the ERROR ``equip.unresolved`` span + an honest narrator surface
    directive; apply NO mutation."""
    with equip_unresolved_span(
        pc_name=pc_name,
        reason=reason,
        requested_item=requested_item,
        action=action,
        turn_number=turn_number,
    ):
        pass
    logger.warning(
        "equip.unresolved pc=%s reason=%s requested_item=%r action=%s",
        pc_name,
        reason,
        requested_item,
        action,
    )
    directive = NarratorDirective(
        kind="must_narrate",
        payload=surface,
        visibility=VisibilityTag(visible_to="all"),
    )
    return SubsystemOutput(directives=[directive], data={"error": reason})


__all__ = ["run_equip_dispatch"]
