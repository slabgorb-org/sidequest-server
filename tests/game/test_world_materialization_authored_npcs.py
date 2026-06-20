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


def test_preload_skips_npc_already_seeded_by_chapter_materialization() -> None:
    """Story 150-3 (sq-playtest 2026-06-20, five_points): a canonical figure
    named by BOTH a history.yaml chapter and npcs.yaml (Isaiah Rynders) is
    seeded into state.npcs by ``materialize_from_genre_pack`` BEFORE preload
    runs. Preload must NOT re-append the same canonical name — pre-fix it
    double-seeded, listing the figure twice in /snapshot npcs.
    """
    state = MagicMock()
    # The chapter materialization already placed "Isaiah Rynders" (maturity-aware
    # disposition picked from the Fresh chapter). Model it as an existing entry.
    chapter_seeded = MagicMock()
    chapter_seeded.core.name = "Isaiah Rynders"
    chapter_seeded.disposition = 10
    state.npcs = [chapter_seeded]
    state.characters = []
    state.turn_manager = MagicMock(interaction=1)

    authored = [
        AuthoredNpc(id="isaiah_rynders", name="Isaiah Rynders", initial_disposition=-30),
        _make_npc("morrissey", disposition=0),  # distinct name → still preloads
    ]

    preload_authored_npcs(state, authored)

    rynders_entries = [n for n in state.npcs if n.core.name == "Isaiah Rynders"]
    assert len(rynders_entries) == 1, (
        "Isaiah Rynders must appear exactly once — preload must skip the authored "
        f"copy of a name already seeded by chapter materialization; got "
        f"{len(rynders_entries)} entries"
    )
    # The maturity-aware chapter seed is kept (disposition 10), not clobbered by
    # the static npcs.yaml baseline (-30).
    assert rynders_entries[0] is chapter_seeded
    assert int(rynders_entries[0].disposition) == 10
    # A distinct authored name with no chapter overlap still preloads normally.
    assert any(n.core.name == "Authored-morrissey" for n in state.npcs)


def test_preload_dedups_within_authored_list() -> None:
    """Defense in depth: id-uniqueness is validated at load, but two authored
    ids that share a NAME must not both seed. The seen-name guard catches it."""
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=1)

    authored = [
        AuthoredNpc(id="rynders_a", name="Isaiah Rynders", initial_disposition=10),
        AuthoredNpc(id="rynders_b", name="Isaiah Rynders", initial_disposition=-5),
    ]

    preload_authored_npcs(state, authored)

    rynders_entries = [n for n in state.npcs if n.core.name == "Isaiah Rynders"]
    assert len(rynders_entries) == 1, (
        f"two authored entries sharing a name must seed once; got {len(rynders_entries)}"
    )
    assert int(rynders_entries[0].disposition) == 10, "first wins"
