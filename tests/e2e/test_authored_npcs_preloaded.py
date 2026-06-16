"""Wiring: authored NPCs (Kestrel crew + Dura Mendes) are in ``state.npcs``
before the first narrator turn fires. Ensures pre-loading happened on
fresh sessions.

This is the load-bearing wiring test for the canned-openings story —
the unit tests at ``tests/game/test_world_materialization_authored_npcs.py``
prove ``preload_authored_npcs`` works in isolation against synthetic
fixtures; this test proves the function works against the actual
shipping coyote_star content.

See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md`` §7.3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sidequest.game.world_materialization import preload_authored_npcs
from sidequest.genre.loader import GenreLoader

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


class _StubTurnManager:
    interaction = 0


class _StubState:
    """Minimal duck-typed snapshot satisfying ``preload_authored_npcs``'s
    fresh-session predicate (``not characters and turn_manager.interaction == 0``).
    """

    def __init__(self) -> None:
        self.npcs: list[Any] = []
        self.characters: list[Any] = []
        self.turn_manager = _StubTurnManager()
        self.genre_slug = "space_opera"
        self.world_slug = "coyote_star"


def test_coyote_star_authored_npcs_preload_into_state() -> None:
    """Load coyote_star, run preload, assert all 5 authored NPCs land in
    ``state.npcs`` with seeded disposition.

    Names are namegen-produced, so we assert by count + disposition tier
    + the canon Dura Mendes pre-canon entry from cartography.
    """
    loader = GenreLoader([CONTENT_ROOT])
    pack = loader.load("space_opera")
    world = pack.worlds["coyote_star"]

    state = _StubState()
    preload_authored_npcs(state, world.authored_npcs)

    # Phase 6 ships 4 Kestrel crew + Dura Mendes = 5 authored NPCs.
    assert len(state.npcs) == len(world.authored_npcs)
    assert len(state.npcs) >= 5

    # Crew are firmly friendly per spec §3.2 (initial_disposition 50–60).
    crew_dispositions = sorted(npc.disposition for npc in state.npcs if npc.disposition >= 50)
    assert len(crew_dispositions) >= 4, (
        f"All 4 Kestrel crew should ship at disposition ≥ 50 — got {crew_dispositions!r}"
    )

    # Dura Mendes is the canon pre-canon NPC referenced by cartography.yaml;
    # she ships at neutral (0) since the PC has not interacted with her yet.
    dura = next((n for n in state.npcs if n.core.name == "Dura Mendes"), None)
    assert dura is not None, (
        f"Dura Mendes missing from preloaded npcs: {[n.core.name for n in state.npcs]}"
    )
    assert dura.disposition == 0
