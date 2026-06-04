"""Unit tests for sidequest.server.dispatch (PLAYER_ACTION → NARRATION path).

Tests the dispatch layer in isolation using mocked ClaudeClient.
No real Claude CLI calls.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.session import GameSnapshot
from sidequest.server.session_handler import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# _apply_narration_result_to_snapshot unit tests
# ---------------------------------------------------------------------------


def _make_result(**kwargs) -> NarrationTurnResult:
    defaults = {
        "narration": "The wind howls outside.",
        "is_degraded": False,
    }
    defaults.update(kwargs)
    return NarrationTurnResult(**defaults)


def test_apply_location_update():
    """location from game_patch is applied to snapshot.character_locations (Wave 2B)."""
    snapshot = GameSnapshot(
        genre_slug="test", world_slug="test", character_locations={"Protagonist": "Old Place"}
    )
    result = _make_result(narration="You arrive.", location="New Dungeon")

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot),
        acting_character_name="Protagonist",
    )

    assert snapshot.character_locations["Protagonist"] == "New Dungeon"
    assert "New Dungeon" in snapshot.discovered_regions


def test_apply_location_added_to_discovered_regions_once():
    """Location is only added to discovered_regions once (no duplicates)."""
    snapshot = GameSnapshot(
        genre_slug="test",
        world_slug="test",
        character_locations={"Protagonist": "Town"},
        discovered_regions=["Town"],
    )
    result = _make_result(narration="You stay in town.", location="Town")

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot),
        acting_character_name="Protagonist",
    )

    assert snapshot.discovered_regions.count("Town") == 1


def test_apply_quest_updates():
    """A stale ``quest_updates`` key on the raw game_patch is auto-forwarded into
    snapshot.quest_log (Story 77-4 No-Silent-Fallbacks guard).

    The typed ``quest_updates`` lane was retired (ADR-137 AC-3); record_quest is
    the clean home. A narrator that still emits the key has its status update
    forwarded — never dropped — via the narration-apply guard, landing as a
    status-bearing QuestEntry. (Full guard contract incl. the loud
    ``quest.updates.legacy_emitted`` span lives in
    tests/game/test_quest_updates_retirement.py.)"""
    snapshot = GameSnapshot(genre_slug="test", world_slug="test")
    result = _make_result(
        narration="Quest started.",
        game_patch_dict={"quest_updates": {"find_crystal": "active"}},
    )

    _apply_narration_result_to_snapshot(snapshot, result, "player", room=room_for(snapshot))

    assert snapshot.quest_log["find_crystal"].status == "active"


def test_apply_lore_established_no_duplicates():
    """Lore established items are appended without duplicates."""
    snapshot = GameSnapshot(
        genre_slug="test",
        world_slug="test",
        lore_established=["The ruins are ancient."],
    )
    result = _make_result(
        narration="You learn more.",
        lore_established=["The ruins are ancient.", "The crystal glows at night."],
    )

    _apply_narration_result_to_snapshot(snapshot, result, "player", room=room_for(snapshot))

    assert snapshot.lore_established.count("The ruins are ancient.") == 1
    assert "The crystal glows at night." in snapshot.lore_established


def test_apply_npc_pool_new_npc():
    """Wave 2A (story 45-47): new NPCs from npcs_present are added to
    ``snapshot.npc_pool`` with ``drawn_from='narrator_invented'`` (replaces
    the pre-Wave-2A npc_registry write path)."""
    from sidequest.agents.orchestrator import NpcMention

    snapshot = GameSnapshot(
        genre_slug="test", world_slug="test", character_locations={"Protagonist": "Tavern"}
    )
    result = _make_result(
        narration="A stranger approaches.",
        npcs_present=[NpcMention(name="Zara", role="barkeep", pronouns="she/her")],
    )

    _apply_narration_result_to_snapshot(snapshot, result, "player", room=room_for(snapshot))

    assert len(snapshot.npc_pool) == 1
    assert snapshot.npc_pool[0].name == "Zara"
    assert snapshot.npc_pool[0].role == "barkeep"
    assert snapshot.npc_pool[0].drawn_from == "narrator_invented"


def test_apply_npc_pool_existing_overwrites_role_and_fills_pronouns():
    """Wave 2A (story 45-47): existing pool members are not duplicated.

    Story 72-7 REVERSES the old 37-44 "canonical frozen" discipline: a
    disagreeing narrator re-mention now **overwrites** the canonical role /
    pronouns of a *narrator-sourced* member (``drawn_from`` !=
    ``world_authored``) — the authoritative-drift fix for the Frandrew /
    session-894 Sitä-minutta path. Drift is still detected and emitted as
    ``npc.reinvented`` (now carrying ``applied=True``).

    Fields still empty on the existing member are filled additively
    (first-time population is not drift).
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.npc_pool import NpcPoolMember

    snapshot = GameSnapshot(
        genre_slug="test",
        world_slug="test",
        character_locations={"Protagonist": "Tavern"},
        npc_pool=[NpcPoolMember(name="Zara", role="stranger", drawn_from="legacy_registry")],
    )
    result = _make_result(
        narration="Zara speaks.",
        npcs_present=[NpcMention(name="Zara", role="barkeep", pronouns="she/her")],
    )

    _apply_narration_result_to_snapshot(snapshot, result, "player", room=room_for(snapshot))

    # Still 1 entry (no duplicate)
    assert len(snapshot.npc_pool) == 1
    member = snapshot.npc_pool[0]
    # 72-7: canonical role is overwritten by the disagreeing re-mention.
    assert member.role == "barkeep"
    # Pronouns were empty on the existing member, so the additive-update
    # path fills them in on first assertion.
    assert member.pronouns == "she/her"


def test_apply_no_mutation_on_empty_result():
    """Empty NarrationTurnResult does not mutate snapshot."""
    snapshot = GameSnapshot(
        genre_slug="test",
        world_slug="test",
        character_locations={"Protagonist": "Start"},
        quest_log={"q1": "active"},
    )
    original_locations = dict(snapshot.character_locations)
    original_quest_log = dict(snapshot.quest_log)

    result = _make_result(narration="Nothing happens.")

    _apply_narration_result_to_snapshot(snapshot, result, "player", room=room_for(snapshot))

    assert snapshot.character_locations == original_locations
    assert snapshot.quest_log == original_quest_log


def test_apply_non_narration_result_is_noop():
    """Non-NarrationTurnResult argument is a no-op (type guard)."""
    snapshot = GameSnapshot(
        genre_slug="test", world_slug="test", character_locations={"Protagonist": "X"}
    )
    _apply_narration_result_to_snapshot(snapshot, object(), "player", room=room_for(snapshot))
    assert snapshot.character_locations["Protagonist"] == "X"


# ---------------------------------------------------------------------------
# Dispatch module re-exports
# ---------------------------------------------------------------------------


def test_session_handler_exports_handler():
    """session_handler is the canonical home for the WebSocket handler."""
    from sidequest.server.session_handler import WebSocketSessionHandler

    assert WebSocketSessionHandler is not None


def test_session_handler_exports_apply_fn():
    """_apply_narration_result_to_snapshot is importable from session_handler."""
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot as fn

    assert callable(fn)
