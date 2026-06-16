"""Predicate catalog — the closed vocabulary of per-player asymmetry.

Adding a new predicate requires: (1) implement below, (2) register in
PREDICATES, (3) add a validator entry (Task 10 signature map), (4) add a
test here, (5) update docs/projection-filter-predicates.md.

Predicates are called with a PredicateContext and an optional field_ref
(string path into payload). Return True => the "unless" clause in a
redact_fields rule is satisfied (so the field stays unmasked). Return
False => the "unless" clause fails and the mask is applied. For
include_if rules, True => include, False => omit.

Predicates never raise on missing fields / unknown relationships. They
return False, which is the conservative (more-restrictive) direction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sidequest.game.projection.view import GameStateView


@dataclass(frozen=True)
class PredicateContext:
    view: GameStateView
    payload: dict
    viewer_player_id: str
    viewer_character_id: str | None


Predicate = Callable[[PredicateContext, "str | None"], bool]


def _read_field(payload: dict, field_ref: str | None) -> object | None:
    if field_ref is None:
        return None
    if field_ref not in payload:
        return None
    return payload[field_ref]


def _is_gm(ctx: PredicateContext, field_ref: str | None) -> bool:
    return ctx.view.is_gm(ctx.viewer_player_id)


def _is_self(ctx: PredicateContext, field_ref: str | None) -> bool:
    value = _read_field(ctx.payload, field_ref)
    if value is None or ctx.viewer_character_id is None:
        return False
    return value == ctx.viewer_character_id


def _is_owner_of(ctx: PredicateContext, field_ref: str | None) -> bool:
    item_id = _read_field(ctx.payload, field_ref)
    if not isinstance(item_id, str):
        return False
    owner = ctx.view.owner_of_item(item_id)
    return owner == ctx.viewer_player_id


def _in_same_zone(ctx: PredicateContext, field_ref: str | None) -> bool:
    target = _read_field(ctx.payload, field_ref)
    if not isinstance(target, str) or ctx.viewer_character_id is None:
        return False
    viewer_zone = ctx.view.zone_of(ctx.viewer_character_id)
    target_zone = ctx.view.zone_of(target)
    if viewer_zone is None or target_zone is None:
        return False
    return viewer_zone == target_zone


def _visible_to(ctx: PredicateContext, field_ref: str | None) -> bool:
    target = _read_field(ctx.payload, field_ref)
    if not isinstance(target, str) or ctx.viewer_character_id is None:
        return False
    return ctx.view.visible_to(ctx.viewer_character_id, target)


def _in_same_party(ctx: PredicateContext, field_ref: str | None) -> bool:
    target_player = _read_field(ctx.payload, field_ref)
    if not isinstance(target_player, str):
        return False
    viewer_party = ctx.view.party_of(ctx.viewer_player_id)
    target_party = ctx.view.party_of(target_player)
    if viewer_party is None or target_party is None:
        return False
    return viewer_party == target_party


PREDICATES: dict[str, Predicate] = {
    "is_gm": _is_gm,
    "is_self": _is_self,
    "is_owner_of": _is_owner_of,
    "in_same_zone": _in_same_zone,
    "visible_to": _visible_to,
    "in_same_party": _in_same_party,
}
