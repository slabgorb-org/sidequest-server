"""SessionGameStateView conservative adapter."""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.game.projection.view import SessionGameStateView


@dataclass
class _FakeSnapshot:
    pass


def test_gm_player_id_is_gm() -> None:
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char", "bob": "bob_char"},
    )
    assert view.is_gm("gm") is True
    assert view.is_gm("alice") is False


def test_character_of_maps_player_to_character() -> None:
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )
    assert view.character_of("alice") == "alice_char"
    assert view.character_of("unknown") is None


def test_zone_of_is_none_when_unknown() -> None:
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )
    assert view.zone_of("alice_char") is None


def test_visible_to_defaults_false_for_unknown_pairs() -> None:
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )
    assert view.visible_to("alice_char", "some_enemy") is False


def test_seat_of_returns_none_when_no_seating() -> None:
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )
    assert view.seat_of("alice") is None


def test_owner_of_item_is_none_when_unknown() -> None:
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char"},
    )
    assert view.owner_of_item("some_item") is None


def test_party_of_returns_party_when_configured() -> None:
    view = SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char", "bob": "bob_char"},
        party_id="party_1",
    )
    assert view.party_of("alice") == "party_1"
    assert view.party_of("unknown") is None
