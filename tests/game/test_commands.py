"""Tests for sidequest.game.commands.

Phase 1: /status, /inventory, /quests, /map, /save, /gm commands.
"""

from __future__ import annotations

from sidequest.game.commands import (
    BUILTIN_COMMANDS,
    DisplayResult,
    ErrorResult,
    GmCommand,
    InventoryCommand,
    MapCommand,
    QuestsCommand,
    SaveCommand,
    StateMutationResult,
    StatusCommand,
)
from sidequest.game.session import GameSnapshot
from tests.game.test_character import make_test_character


def _make_state() -> GameSnapshot:
    """Build a minimal GameSnapshot for command testing."""
    return GameSnapshot(
        genre_slug="test",
        world_slug="test",
        characters=[make_test_character()],
        character_locations={"Thorn Ironhide": "The Iron Mines"},
        current_region="Ironhold Mountains",
        discovered_regions=["Ironhold Mountains", "Sunken Vale"],
        discovered_routes=["Mountains → Vale"],
        quest_log={
            "Find the Warden": "active: search the lower mines",
            "Rescue the miners": "completed — rescued all three",
        },
    )


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


def test_status_displays_character_info():
    state = _make_state()
    cmd = StatusCommand()
    result = cmd.handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "Thorn Ironhide" in result.text
    assert "Level 3" in result.text
    assert "Fighter" in result.text
    assert "The Iron Mines" in result.text


def test_status_no_character_returns_error():
    state = GameSnapshot()
    result = StatusCommand().handle(state, "")
    assert isinstance(result, ErrorResult)


def test_status_includes_stats():
    state = _make_state()
    result = StatusCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "STR" in result.text


# ---------------------------------------------------------------------------
# /inventory
# ---------------------------------------------------------------------------


def test_inventory_empty():
    state = _make_state()
    result = InventoryCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "nothing of note" in result.text.lower() or "Gold" in result.text


def test_inventory_no_character_returns_error():
    state = GameSnapshot()
    result = InventoryCommand().handle(state, "")
    assert isinstance(result, ErrorResult)


# ---------------------------------------------------------------------------
# /quests
# ---------------------------------------------------------------------------


def test_quests_shows_active_and_completed():
    state = _make_state()
    result = QuestsCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "ACTIVE" in result.text
    assert "COMPLETED" in result.text
    assert "Find the Warden" in result.text


def test_quests_empty():
    state = GameSnapshot()
    result = QuestsCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "just beginning" in result.text.lower()


# ---------------------------------------------------------------------------
# /map
# ---------------------------------------------------------------------------


def test_map_shows_regions():
    state = _make_state()
    result = MapCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "Ironhold Mountains" in result.text
    assert "Sunken Vale" in result.text
    assert "(current)" in result.text


def test_map_shows_routes():
    state = _make_state()
    result = MapCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "Mountains" in result.text


def test_map_empty_state():
    state = GameSnapshot()
    result = MapCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "No regions" in result.text


# ---------------------------------------------------------------------------
# /save
# ---------------------------------------------------------------------------


def test_save_returns_confirmation():
    state = _make_state()
    result = SaveCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "Thorn Ironhide" in result.text
    assert "saved" in result.text.lower()


def test_save_no_character():
    state = GameSnapshot()
    result = SaveCommand().handle(state, "")
    assert isinstance(result, DisplayResult)
    assert "saved" in result.text.lower()


# ---------------------------------------------------------------------------
# /gm set
# ---------------------------------------------------------------------------


def test_gm_set_location():
    state = _make_state()
    result = GmCommand().handle(state, "set location The Deep Caverns")
    assert isinstance(result, StateMutationResult)
    assert result.patch.location == "The Deep Caverns"


def test_gm_set_atmosphere():
    state = _make_state()
    result = GmCommand().handle(state, "set atmosphere Ominous and foreboding")
    assert isinstance(result, StateMutationResult)
    assert result.patch.atmosphere == "Ominous and foreboding"


def test_gm_set_unknown_field_returns_error():
    state = _make_state()
    result = GmCommand().handle(state, "set unknown_field value")
    assert isinstance(result, ErrorResult)


def test_gm_set_missing_value_returns_error():
    state = _make_state()
    result = GmCommand().handle(state, "set location")
    assert isinstance(result, ErrorResult)


# ---------------------------------------------------------------------------
# /gm teleport
# ---------------------------------------------------------------------------


def test_gm_teleport():
    state = _make_state()
    result = GmCommand().handle(state, "teleport SunkenVale The Flooded Plaza")
    assert isinstance(result, StateMutationResult)
    assert result.patch.location == "The Flooded Plaza"
    assert result.patch.current_region == "SunkenVale"
    assert result.patch.discover_regions == ["SunkenVale"]


def test_gm_teleport_missing_args_returns_error():
    state = _make_state()
    result = GmCommand().handle(state, "teleport OnlyOneArg")
    assert isinstance(result, ErrorResult)


# ---------------------------------------------------------------------------
# /gm spawn — REMOVED (user deprecation 2026-07-11, Green Room follow-up).
# A removed subcommand must fall through to the unknown-subcommand error.
# ---------------------------------------------------------------------------


def test_gm_spawn_is_gone():
    state = _make_state()
    result = GmCommand().handle(state, "spawn Grog Bouncer gruff and mean")
    assert isinstance(result, ErrorResult)
    assert "Unknown GM subcommand" in result.message


# ---------------------------------------------------------------------------
# /gm dmg
# ---------------------------------------------------------------------------


def test_gm_dmg_creates_hp_patch():
    state = _make_state()
    result = GmCommand().handle(state, "dmg Thorn Ironhide 5")
    assert isinstance(result, StateMutationResult)
    assert result.patch.hp_changes is not None
    assert result.patch.hp_changes.get("Thorn Ironhide") == -5


def test_gm_dmg_invalid_amount_returns_error():
    state = _make_state()
    result = GmCommand().handle(state, "dmg Goblin notanumber")
    assert isinstance(result, ErrorResult)


def test_gm_dmg_missing_args_returns_error():
    state = _make_state()
    result = GmCommand().handle(state, "dmg")
    assert isinstance(result, ErrorResult)


# ---------------------------------------------------------------------------
# /gm unknown subcommand
# ---------------------------------------------------------------------------


def test_gm_unknown_subcommand():
    state = _make_state()
    result = GmCommand().handle(state, "frobnicate foo")
    assert isinstance(result, ErrorResult)


def test_gm_empty_subcommand():
    state = _make_state()
    result = GmCommand().handle(state, "")
    assert isinstance(result, ErrorResult)


# ---------------------------------------------------------------------------
# BUILTIN_COMMANDS registry
# ---------------------------------------------------------------------------


def test_builtin_commands_has_all_six():
    names = {cmd.name for cmd in BUILTIN_COMMANDS}
    assert names == {"status", "inventory", "quests", "map", "save", "gm"}
