"""Tests for AuthoredNpc pre-loading at world materialization (Task 13).

Fresh sessions (no seated player character): NPCs land in state.npcs with
disposition seeded. Resumed sessions (a player character already present):
pre-loading is SKIPPED.

Story 71-7: the freshness gate keys on the absence of a seated player
character, NOT on ``turn_manager.interaction`` — a real fresh snapshot
baselines at interaction == 1, so the old ``interaction == 0`` clause was
unsatisfiable in production. These fixtures now use the real baseline so they
no longer mask the regression.
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
    """No seated player character = fresh; pre-load. Uses the real fresh
    interaction baseline (1), not the fabricated 0 that masked story 71-7."""
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=1)

    authored = [_make_npc("captain", disposition=60), _make_npc("doc", disposition=50)]

    preload_authored_npcs(state, authored)

    assert len(state.npcs) == 2
    assert state.npcs[0].core.name == "Authored-captain"
    assert int(state.npcs[0].disposition) == 60
    assert int(state.npcs[1].disposition) == 50


def test_resumed_session_skips_preload_when_characters_exist() -> None:
    """A seated player character = resumed; do NOT pre-load."""
    state = MagicMock()
    state.npcs = []
    state.characters = [MagicMock()]  # already a character — resumed
    state.turn_manager = MagicMock(interaction=5)

    preload_authored_npcs(state, [_make_npc("captain")])

    assert state.npcs == []  # untouched


def test_interaction_count_does_not_gate_preload() -> None:
    """Story 71-7 regression guard: interaction count is NOT part of the
    freshness gate. With no seated player character, the crew loads regardless
    of interaction value (a fresh snapshot really baselines at interaction 1,
    and the gate must not key on it)."""
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=5)

    preload_authored_npcs(state, [_make_npc("captain", disposition=60)])

    assert len(state.npcs) == 1
    assert state.npcs[0].core.name == "Authored-captain"


def test_empty_authored_list_is_noop() -> None:
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=0)

    preload_authored_npcs(state, [])

    assert state.npcs == []
