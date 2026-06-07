"""Story 84-2 (WI-5) — alias mention in retrieval: drama-gate + WIRING (RED phase).

Two integration-level guarantees on top of the pure resolver
(``tests/game/test_alias_resolution.py``):

  AC-6 (drama-gate interaction) — when a player references an NPC by ALIAS and the
  84-1 drama-gate skips the cosine embed on the strength of the alias-raised
  ``mention``, the alias-referenced NPC must STILL reach the prompt (it rides the
  floor / is not budgeted out). An alias must not silently require an embed to
  surface — otherwise the §A4 win is lost on exactly the cheap turns it targets.

  AC-7 (WIRING) — alias-resolved mention reaches the LIVE retrieval path:
  ``player_referenced_npcs_from_action`` (the seam ``retrieve_for_turn`` feeds into
  ``retrieve_turn_context`` mention) classifies an alias reference as a reference to
  the aliased NPC. Behavior, not source grep.

Both pin that the alias signal flows through the REAL mention seam
(``retrieval_orchestration.py:300-303`` / ``agents/npc_context.py``), not a parallel
path. Span/embed-count assertions: run ``-n0``. Synthetic fixtures only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sidequest.game.creature_core import CreatureCore
from sidequest.game.entity_store import EntityStore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _npc(name: str, last_seen_turn: int, *, aliases: list[str] | None = None) -> Npc:
    kwargs: dict[str, Any] = {
        "core": CreatureCore(name=name, description=f"{name} is a test NPC.", personality="stoic"),
        "last_seen_turn": last_seen_turn,
    }
    if aliases is not None:
        kwargs["aliases"] = aliases
    return Npc(**kwargs)


def _snap(*, current_turn: int, npcs: list[Npc] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=current_turn),
        npcs=npcs or [],
        npc_pool=[],
    )


class _FakeDaemon:
    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        return {"embedding": [1.0, 0.0, 0.0], "model": "fake", "latency_ms": 1}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ===========================================================================
# AC-7 (seam) — the live mention extractor resolves aliases
# ===========================================================================


class TestAliasMentionExtractor:
    def test_player_referenced_npcs_resolves_alias(self) -> None:
        """``player_referenced_npcs_from_action`` (the seam feeding mention) must
        classify an ALIAS reference as a reference to the aliased NPC. Today it is
        name-match only — WI-5 widens it to read ``Npc.aliases``."""
        from sidequest.agents.npc_context import player_referenced_npcs_from_action

        snap = _snap(current_turn=10, npcs=[_npc("Thorn", 3, aliases=["the old man"])])
        referenced = player_referenced_npcs_from_action(snap, "I greet the old man")
        assert "Thorn" in referenced, (
            "an alias reference must register the aliased NPC as referenced "
            "(the live mention seam, §A4)"
        )

    def test_name_reference_still_works_alongside_alias(self) -> None:
        """No regression: the canonical name still registers."""
        from sidequest.agents.npc_context import player_referenced_npcs_from_action

        snap = _snap(current_turn=10, npcs=[_npc("Thorn", 3, aliases=["the old man"])])
        referenced = player_referenced_npcs_from_action(snap, "I attack Thorn")
        assert "Thorn" in referenced


# ===========================================================================
# AC-6 — drama-gate: alias match skips embed, NPC still surfaces
# ===========================================================================


class TestAliasDramaGate:
    def test_alias_match_skips_embed_and_npc_still_surfaces(self) -> None:
        """A scene-present NPC referenced BY ALIAS: the alias-raised mention (+ the
        present floor) clears the 84-1 drama-gate, so the cosine embed is SKIPPED —
        yet the NPC still reaches the prompt via the floor. The §A4 win must hold on
        the cheap (embed-skipped) turn, not require a fallback embed."""
        from sidequest.agents.npc_context import player_referenced_npcs_from_action
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        snap = _snap(current_turn=10, npcs=[_npc("Thorn", 10, aliases=["the old man"])])
        store = EntityStore()
        fake = _FakeDaemon()

        # The production caller derives player_referenced_npcs from the action via
        # the (alias-aware, WI-5) extractor — drive that real seam.
        referenced = player_referenced_npcs_from_action(snap, "I confront the old man")

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I confront the old man",
                current_turn=10,
                player_referenced_npcs=referenced,
                client=fake,
            )
        )

        assert result.embed_skipped is True, (
            "an alias-referenced, present NPC must clear the drama-gate (skip embed)"
        )
        assert fake.calls == [], "the cosine embed must be skipped on an alias-resolved turn"
        floor_names = {n.core.name for n in result.floor.full_profiles}
        assert "Thorn" in floor_names, (
            "the alias-referenced NPC must still surface via the floor when embed is skipped"
        )


# ===========================================================================
# AC-7 (wiring) — the live retrieval delegate resolves the alias
# ===========================================================================


class TestAliasRetrievalWiring:
    def test_live_turn_resolves_alias_to_referenced_npc(self, monkeypatch) -> None:
        """Drive the LIVE ``_retrieve_entities_for_turn`` delegate with an action
        that references an off-stage NPC BY ALIAS, and assert the alias resolution
        reaches the real path: the off-stage NPC renders BRIEF (name+role) — which
        only happens when the turn registered an NPC reference — proving the
        alias-aware mention flowed through ``retrieve_for_turn`` →
        ``player_referenced_npcs_from_action`` → the floor toggle. Behavior, not grep."""
        from sidequest.agents.npc_context import build_npc_working_set

        # Off-stage NPC (last_seen far in the past) with an alias.
        snap = _snap(
            current_turn=20,
            npcs=[_npc("Thorn", last_seen_turn=2, aliases=["the old man"])],
        )

        # The production floor toggle: build_npc_working_set renders off-stage NPCs
        # BRIEF iff the turn registered a reference. Feed the alias-aware extractor.
        from sidequest.agents.npc_context import player_referenced_npcs_from_action

        referenced = player_referenced_npcs_from_action(snap, "what happened to the old man?")
        ws = build_npc_working_set(
            snap, current_turn=20, player_referenced_npcs=referenced
        )

        brief_names = {
            (n.core.name if isinstance(n, Npc) else n.name) for n in ws.brief_entries
        }
        assert "Thorn" in brief_names, (
            "an alias reference must flip the off-stage NPC to BRIEF on the live "
            "working-set path — proving alias-resolved mention is wired in (§A4 / AC-7)"
        )
