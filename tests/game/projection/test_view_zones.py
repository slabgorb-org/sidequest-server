from sidequest.game.projection.view import SessionGameStateView


def test_zone_of_returns_configured_zone():
    view = SessionGameStateView(
        gm_player_id="gm1",
        player_id_to_character={"p1": "char_alice", "p2": "char_bob"},
        character_zones={"char_alice": "warehouse", "char_bob": "inn"},
    )
    assert view.zone_of("char_alice") == "warehouse"
    assert view.zone_of("char_bob") == "inn"
    assert view.zone_of("char_unknown") is None


def test_visible_to_true_when_same_zone_and_not_hidden():
    view = SessionGameStateView(
        gm_player_id="gm1",
        player_id_to_character={"p1": "char_alice", "p2": "char_bob"},
        character_zones={"char_alice": "inn", "char_bob": "inn"},
    )
    assert view.visible_to("char_alice", "char_bob") is True


def test_visible_to_false_when_different_zones():
    view = SessionGameStateView(
        gm_player_id="gm1",
        player_id_to_character={"p1": "char_alice", "p2": "char_bob"},
        character_zones={"char_alice": "warehouse", "char_bob": "inn"},
    )
    assert view.visible_to("char_alice", "char_bob") is False


def test_visible_to_false_when_target_hidden_even_same_zone():
    view = SessionGameStateView(
        gm_player_id="gm1",
        player_id_to_character={"p1": "char_alice", "p2": "char_bob"},
        character_zones={"char_alice": "inn", "char_bob": "inn"},
        hidden_characters={"char_bob"},
    )
    assert view.visible_to("char_alice", "char_bob") is False


def test_visible_to_false_on_unknown_character():
    view = SessionGameStateView(
        gm_player_id="gm1",
        player_id_to_character={"p1": "char_alice"},
        character_zones={"char_alice": "inn"},
    )
    assert view.visible_to("char_alice", "char_ghost") is False
    assert view.visible_to("char_ghost", "char_alice") is False
