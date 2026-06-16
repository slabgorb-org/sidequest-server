"""Story 84-1 (WI-1) — unified scorer supersedes the §D4 floor/fill orchestration (RED).

ADR-118 Amendment §A1 replaces the §D4 two-mechanism split (binary floor +
similarity-ranked fill) inside ``retrieve_turn_context`` with ONE scored
selection. These tests pin the orchestration-level behavior of that replacement,
sitting above ``tests/game/test_pertinence_scorer.py`` (which unit-tests the
scorer in isolation):

  1. The result still satisfies the consumers (``session_helpers`` renders
     ``retrieved_npcs/locations/factions`` + ``floor``; ``universal_retrieval``
     reads ``outcome``) — supersession is a REPLACEMENT, not a breaking rewrite of
     the public result shape.
  2. The drama-gate actually fires end-to-end: a named, located action does NOT
     call the daemon ``embed`` — strictly cheaper than the §D4 always-embed fill.
  3. A thin action (named nothing, nowhere specific) DOES embed (cosine fallback).
  4. Each selected card carries its ``PertinenceScore`` decomposition, and the
     result exposes ``embed_skipped`` (the A5 lifecycle signal WI-6 will emit).
  5. The present-scene entity is never absent from the result even under a budget
     that would otherwise evict it.

All net-new symbols are imported inside each test so collection survives and each
failure is a crisp ImportError/AttributeError. Synthetic fixtures only — no
content invariants (those belong in the pack validator per project rule).
"""

from __future__ import annotations

import asyncio
from typing import Any

from sidequest.game.creature_core import CreatureCore
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/game/test_retrieval_orchestration.py
# ---------------------------------------------------------------------------


def _npc(name: str, last_seen_turn: int) -> Npc:
    return Npc(
        core=CreatureCore(name=name, description=f"{name} is a test NPC.", personality="stoic"),
        last_seen_turn=last_seen_turn,
    )


def _snap(*, current_turn: int, npcs: list[Npc] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=current_turn),
        npcs=npcs or [],
        npc_pool=[],
    )


class _FakeDaemon:
    """Records every ``embed`` call so a test can assert the drama-gate SKIPPED
    it (``calls == []``) or required it (``calls == [action]``)."""

    def __init__(
        self,
        *,
        available: bool = True,
        embedding: list[float] | None = None,
    ) -> None:
        self._available = available
        self._embedding = embedding if embedding is not None else [1.0, 0.0, 0.0]
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def embed(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        return {"embedding": list(self._embedding), "model": "fake", "latency_ms": 1}


def _seed_embedded_card(
    store: EntityStore,
    *,
    entity_type: str,
    entity_id: str,
    content: str,
    embedding: list[float],
) -> EntityCard:
    card = EntityCard.new(entity_type, entity_id, content=content)
    store.add(card)
    store.update_embedding(card.id, embedding)
    return card


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ===========================================================================
# Drama-gate fires end-to-end (the strictly-cheaper win)
# ===========================================================================


class TestDramaGateEndToEnd:
    def test_named_present_action_skips_the_daemon_embed(self) -> None:
        """§A1 drama-gate: '"I attack Borin"' — Borin is scene-present and named,
        so mention + here suffice and the cosine embed is SKIPPED. The §D4 code
        ALWAYS embedded; the new orchestration must not call the daemon here."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        snap = _snap(current_turn=10, npcs=[_npc("Borin", 10)])  # scene-present
        store = EntityStore()
        fake = _FakeDaemon()

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I attack Borin",
                current_turn=10,
                player_referenced_npcs={"Borin"},
                client=fake,
            )
        )

        assert fake.calls == [], (
            "a named, scene-present action must SKIP the embed (drama-gate, §A1) — "
            f"the daemon was called with {fake.calls!r}"
        )
        assert result.embed_skipped is True, (
            "the result must report embed_skipped=True so WI-6 can emit "
            "retrieval.embed_skipped honestly"
        )

    def test_thin_action_still_embeds_as_fallback(self) -> None:
        """The complement: an action that names nothing and is nowhere specific
        is THIN — the cosine fallback must run, so the daemon IS called once."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        snap = _snap(current_turn=10, npcs=[])  # nobody present, nobody named
        store = EntityStore()
        _seed_embedded_card(
            store,
            entity_type=EntityType.LOCATION,
            entity_id="black_hart",
            content="The Black Hart — a dockside tavern.",
            embedding=[1.0, 0.0, 0.0],
        )
        fake = _FakeDaemon(embedding=[1.0, 0.0, 0.0])

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I look around and wonder what to do next",
                current_turn=10,
                player_referenced_npcs=set(),
                client=fake,
            )
        )

        assert fake.calls == ["I look around and wonder what to do next"], (
            "a thin action must run the cosine fallback — the daemon must be called once"
        )
        assert result.embed_skipped is False


# ===========================================================================
# Result carries the per-card score decomposition (feeds WI-6 / A5)
# ===========================================================================


class TestScoreDecompositionExposed:
    def test_selected_cards_carry_pertinence_scores(self) -> None:
        """Each selected card's ``PertinenceScore`` must be reachable on the
        result so WI-6 can emit ``retrieval.card.reason`` per card. The §D4 result
        had only counts; the unified result must expose the per-card breakdown."""
        from sidequest.game.pertinence import PertinenceScore
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        snap = _snap(current_turn=10, npcs=[])
        store = EntityStore()
        _seed_embedded_card(
            store,
            entity_type=EntityType.LOCATION,
            entity_id="black_hart",
            content="The Black Hart — a dockside tavern.",
            embedding=[1.0, 0.0, 0.0],
        )
        fake = _FakeDaemon(embedding=[1.0, 0.0, 0.0])

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I wander toward a tavern somewhere",
                current_turn=10,
                player_referenced_npcs=set(),
                client=fake,
            )
        )

        scores = result.card_scores  # net-new: list[PertinenceScore] (one per selected card)
        assert scores, "the unified result must expose per-card pertinence scores"
        assert all(isinstance(s, PertinenceScore) for s in scores)
        assert any(s.card_id == "loc:black_hart" for s in scores), (
            "the selected location's score must be present in the decomposition"
        )


# ===========================================================================
# Back-compat: the result still satisfies the live consumers
# ===========================================================================


class TestResultShapeBackCompat:
    def test_result_still_exposes_consumer_fields(self) -> None:
        """``session_helpers`` reads ``retrieved_npcs/locations/factions`` +
        ``floor``; ``universal_retrieval`` reads ``outcome``. Superseding §D4 must
        not break those — the fill still lands in the typed buckets."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        snap = _snap(current_turn=10, npcs=[_npc("Borin", 10)])
        store = EntityStore()
        _seed_embedded_card(
            store,
            entity_type=EntityType.LOCATION,
            entity_id="black_hart",
            content="The Black Hart — a dockside tavern.",
            embedding=[1.0, 0.0, 0.0],
        )
        fake = _FakeDaemon(embedding=[1.0, 0.0, 0.0])

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I look around for somewhere to drink",
                current_turn=10,
                player_referenced_npcs=set(),
                client=fake,
            )
        )

        # Floor still present and typed.
        assert {n.core.name for n in result.floor.full_profiles} == {"Borin"}
        # Fill still bucketed by type for the renderer.
        assert result.retrieved_locations is not None
        assert [c.id for c in result.retrieved_locations] == ["loc:black_hart"]
        assert result.outcome == "success"


# ===========================================================================
# Present-scene invariant holds at the orchestration level
# ===========================================================================


class TestPresentSceneInvariantOrchestration:
    def test_present_npc_never_dropped_under_tight_budget(self) -> None:
        """A tiny budget that fits NEITHER the floor NPC nor any fill must still
        surface the scene-present NPC — the hard invariant (§A1) overrides the
        token ceiling. The §D4 code charged the floor first and could starve it;
        the unified scorer must exempt the present scene entirely."""
        from sidequest.game.retrieval_orchestration import retrieve_turn_context

        snap = _snap(current_turn=10, npcs=[_npc("Borin", 10)])
        store = EntityStore()
        fake = _FakeDaemon()

        result = _run(
            retrieve_turn_context(
                store,
                snap,
                "I confront Borin",
                current_turn=10,
                player_referenced_npcs={"Borin"},
                client=fake,
                budget_tokens=1,  # smaller than any real card
            )
        )

        floor_names = {n.core.name for n in result.floor.full_profiles}
        assert "Borin" in floor_names, (
            "the scene-present NPC must survive a budget that cannot afford it — "
            "present-scene hard invariant (§A1)"
        )
        # And the named/present action must have ridden the new scorer's drama-gate
        # (embed skipped) — this pins that the invariant held via the §A1 mechanism,
        # not merely §D4's incidental always-floor behavior.
        assert result.embed_skipped is True, (
            "a named, scene-present confrontation must skip the embed (§A1) — "
            "proves the invariant held through the unified scorer, not legacy §D4"
        )
