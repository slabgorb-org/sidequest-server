"""Unit tests for the SessionRoom companion-bond registry (Plan B Task 1).

Covers AC: a PET resolves an owner-private view; a HIRELING and a PEER do not;
unknown relationship strings parse to None so the caller fails CLOSED.
"""

from __future__ import annotations

from sidequest.game.persistence import GameMode
from sidequest.server.session_room import (
    CompanionRelationship,
    SessionRoom,
    parse_companion_relationship,
)


def _room() -> SessionRoom:
    # SessionRoom requires slug + mode; the no-arg form the plan sketched would
    # TypeError before the feature is ever exercised (see Delivery Findings).
    return SessionRoom(slug="companion-registry", mode=GameMode.SOLO)


def test_parse_relationship_exact_match_or_none():
    assert parse_companion_relationship("pet") is CompanionRelationship.PET
    assert parse_companion_relationship("hireling") is CompanionRelationship.HIRELING
    assert parse_companion_relationship("peer") is CompanionRelationship.PEER
    assert parse_companion_relationship("sidekick") is None  # unknown -> None (default-closed)
    assert parse_companion_relationship("") is None  # empty string is not a role
    assert parse_companion_relationship(None) is None


def test_pet_bond_resolves_owner_and_pets():
    room = _room()
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("rex-pid", "alice@home", CompanionRelationship.PET)

    assert room.pets_of("owner-pid") == ["rex-pid"]


def test_hireling_bond_does_not_widen():
    room = _room()
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("gus-pid", "alice@home", CompanionRelationship.HIRELING)

    assert room.pets_of("owner-pid") == []  # hireling is not a pet


def test_peer_bond_does_not_widen():
    # PEER is a full independent seat, not a window into the owner's view.
    # Only PET widens — a PEER must never appear in pets_of (fail-closed
    # invariant: exactly one widening role).
    room = _room()
    room.set_player_identity("owner-pid", "alice@home")
    room.register_companion_bond("kit-pid", "alice@home", CompanionRelationship.PEER)

    assert room.pets_of("owner-pid") == []


def test_pet_without_resolved_owner_identity_is_empty():
    # A pet bonded by identity, but the owner's player_id has no identity mapping
    # yet (e.g. pet connected first). pets_of must fail safe to [] — never widen
    # to a pet whose owner identity is unresolved.
    room = _room()
    room.register_companion_bond("rex-pid", "alice@home", CompanionRelationship.PET)
    assert room.pets_of("owner-pid") == []


def test_pets_of_unknown_owner_is_empty():
    room = _room()
    assert room.pets_of("nobody-pid") == []
