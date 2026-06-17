"""Bridge wiring for the Fate narrator state (Story 116-2 / ADR-144 F2b).

``TurnContext.fate_state`` is declared (orchestrator.py) and consumed (the ``fate_state``
prompt section fires when it is non-None — see tests/agents/test_fate_narrator_prompt.py).
This is the missing-third proof per CLAUDE.md "Every Test Suite Needs a Wiring Test": that
the sole production construction site — ``_build_turn_context`` in ``session_helpers.py`` —
actually POPULATES it from the live snapshot via ``build_fate_projection``, gated on the
bound ruleset.

No Fate pack is bound yet (ADR-144 F4 binds ``ruleset: fate`` on the four packs and is
deferred), so the test loads a real pack and overrides its ruleset to ``"fate"`` to exercise
the bridge — the same gate the session handler reads. Behavioral proof (drive the real
builder, observe the populated field), never a source grep.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader


def _fate_pc(name: str) -> Character:
    sheet = FateSheet(skills={"Fight": 3, "Notice": 2}, fate_points=3)
    sheet.aspects.append(Aspect(text="Last Honest Cop in Vega", kind="high_concept"))
    return Character(
        core=CreatureCore(name=name, description="d", personality="p", fate_sheet=sheet),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


def _make_sd(*, ruleset: str, with_fate_pc: bool):
    """A minimal real ``_SessionData`` with a loaded pack whose ruleset is overridden.

    pulp_noir is native today; ADR-144 F4 (deferred) will bind it to fate. Overriding the
    loaded pack's ruleset lets us exercise the exact gate the bridge reads without waiting
    on F4 content."""
    from sidequest.server.session_handler import _SessionData

    loader = GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS)
    pack = loader.load("pulp_noir")
    pack.rules.ruleset = ruleset

    snap = GameSnapshot(
        genre_slug="pulp_noir",
        world_slug="vega",
        turn_manager=TurnManager(interaction=3),
        characters=[_fate_pc("Vance")] if with_fate_pc else [],
    )
    snap.character_locations["Vance"] = "Back Room"
    snap.player_seats["player:Vance"] = "Vance"
    repo = MagicMock()
    repo.recent_narrative.return_value = []
    sd = _SessionData(
        genre_slug="pulp_noir",
        world_slug="vega",
        player_name="Vance",
        player_id="player:Vance",
        snapshot=snap,
        repository=repo,
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.game_slug = "2026-06-14-pulp_noir_vega-1"
    return sd


def test_build_turn_context_populates_fate_state_for_fate_pack() -> None:
    """The bridge derives ``TurnContext.fate_state`` from the snapshot via
    ``build_fate_projection`` when the pack binds the Fate ruleset."""
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(ruleset="fate", with_fate_pc=True)
    ctx = _build_turn_context(sd)

    assert ctx.fate_state is not None, (
        "_build_turn_context must populate TurnContext.fate_state for a Fate pack"
    )
    assert ctx.fate_state["fate_points"]["Vance"] == 3
    assert "Last Honest Cop in Vega" in ctx.fate_state["character_aspects"]["Vance"]


def test_build_turn_context_leaves_fate_state_none_for_non_fate_pack() -> None:
    """A non-Fate pack carries None — the narrator section never fires and the pack pays
    zero tokens (same single-chokepoint discipline as magic_state / mutation_state)."""
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(ruleset="dial", with_fate_pc=True)
    ctx = _build_turn_context(sd)

    assert ctx.fate_state is None, "non-Fate pack must leave TurnContext.fate_state None"
