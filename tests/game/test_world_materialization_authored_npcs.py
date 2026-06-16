"""Tests for AuthoredNpc pre-loading at world materialization (Task 13).

Fresh sessions: NPCs land in state.npcs with disposition seeded.
Resumed sessions: pre-loading is SKIPPED.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.world_materialization import preload_authored_npcs
from sidequest.genre.models.authored_npc import AuthoredNpc


def _make_npc(npc_id: str, disposition: int = 0) -> AuthoredNpc:
    return AuthoredNpc(
        id=npc_id,
        name=f"Authored-{npc_id}",
        pronouns="they/them",
        role="crew",
        appearance="brief description",
        initial_disposition=disposition,
    )


def test_fresh_session_preloads_npcs() -> None:
    """Empty state.npcs + interaction == 0 + characters == [] = fresh; pre-load."""
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=0)

    authored = [_make_npc("captain", disposition=60), _make_npc("doc", disposition=50)]

    preload_authored_npcs(state, authored)

    assert len(state.npcs) == 2
    assert state.npcs[0].core.name == "Authored-captain"
    assert int(state.npcs[0].disposition) == 60
    assert int(state.npcs[1].disposition) == 50


def test_resumed_session_skips_preload_when_characters_exist() -> None:
    """Existing characters or interaction > 0 = resumed; do NOT pre-load."""
    state = MagicMock()
    state.npcs = []
    state.characters = [MagicMock()]  # already a character — resumed
    state.turn_manager = MagicMock(interaction=0)

    preload_authored_npcs(state, [_make_npc("captain")])

    assert state.npcs == []  # untouched


def test_past_turn_zero_skips_preload() -> None:
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=5)  # past turn 0

    preload_authored_npcs(state, [_make_npc("captain")])

    assert state.npcs == []


def test_empty_authored_list_is_noop() -> None:
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=0)

    preload_authored_npcs(state, [])

    assert state.npcs == []
