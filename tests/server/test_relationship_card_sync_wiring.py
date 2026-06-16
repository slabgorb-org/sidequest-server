"""Story 84-3 (WI-4) — relationship card projection via entity-sync + WIRING (RED).

ADR-118 §A2/§D3: relationship cards are STORED — projected into the index by the
per-turn ``entity_sync`` sweep alongside the NPC card (not on-demand at retrieval).
This suite proves:

  AC-7 — ``sync_entity_cards`` (the pure game-tier sweep) projects a ``rel:<slug>``
  card for a stateful NPC with disposition history, and counts it (a
  ``relationship_count`` reproject tally) so the GM panel can verify it.

  AC-8 — WIRING: the PRODUCTION dispatch ``sync_for_turn`` (called every turn from
  ``_execute_narration_turn``) lands the relationship card in the live
  ``entity_store`` and the published watcher event reflects it. Behavior + watcher
  event only (No Source-Text Wiring Tests).

Mirrors ``tests/server/dispatch/test_entity_sync_stateful_wiring.py`` (the
duck-typed sd harness). Run ``-n0`` if any span-count assertion is added.
"""

from __future__ import annotations

import pytest

from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition, DispositionBeat
from sidequest.game.entity_card import EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.session import Npc


def _npc_with_history(name: str = "Borin", *, disposition: int = 40) -> Npc:
    """A stateful NPC carrying a non-trivial disposition_log — the source a
    relationship card projects from."""
    return Npc(
        core=CreatureCore(name=name, description="A wandering smith.", personality="gruff"),
        disposition=Disposition(disposition),
        last_seen_turn=1,
        disposition_log=[
            DispositionBeat(turn=1, delta=15, reason="saved the party", location=None),
            DispositionBeat(turn=2, delta=-5, reason="argued over loot", location=None),
        ],
    )


class _TurnManager:
    interaction = 2


class _Snapshot:
    def __init__(self, npcs: list[Npc]) -> None:
        self.npc_pool: list = []
        self.npcs = npcs
        self.turn_manager = _TurnManager()


class _SessionData:
    def __init__(self, snapshot: _Snapshot) -> None:
        self.snapshot = snapshot
        self.entity_store = EntityStore()


# ===========================================================================
# AC-7 — the pure sweep projects + counts the relationship card
# ===========================================================================


class TestSyncProjectsRelationshipCard:
    def test_sync_indexes_a_relationship_card_for_stateful_npc(self) -> None:
        """``sync_entity_cards`` must project a ``rel:<slug>`` card for a stateful
        NPC with disposition history — alongside the ``npc:<slug>`` card."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        sync_entity_cards(store, _Snapshot([_npc_with_history("Borin")]))

        rel_ids = {c.id for c in store.query_by_type(EntityType.RELATIONSHIP)}
        assert "rel:borin" in rel_ids, (
            "the per-turn sync must project a relationship card for an NPC with "
            "disposition history"
        )
        # The NPC card is still projected too (the two coexist, distinct ids).
        npc_ids = {c.id for c in store.query_by_type(EntityType.NPC)}
        assert "npc:borin" in npc_ids

    def test_sync_counts_relationship_reprojects(self) -> None:
        """The sync result must carry a ``relationship_count`` reproject tally so
        the GM panel (lie-detector) can verify relationship cards are
        engine-projected, not improvised (AC-7 OTEL)."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        result = sync_entity_cards(store, _Snapshot([_npc_with_history("Borin")]))

        assert hasattr(result, "relationship_count"), (
            "EntitySyncResult must expose a relationship_count tally"
        )
        assert result.relationship_count == 1


# ===========================================================================
# AC-8 — WIRING: the live dispatch sweep lands the relationship card
# ===========================================================================


class TestRelationshipCardSyncWiring:
    def test_relationship_card_indexed_by_live_sync_for_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the PRODUCTION ``sync_for_turn`` (the per-turn sweep that runs from
        ``_execute_narration_turn``): a stateful NPC with disposition history lands a
        ``rel:<slug>`` card in the live ``entity_store``.

        SCOPE NOTE (Reviewer correction): this proves INDEXING only — the card is
        stored and the watcher event counts it. It does NOT prove the card reaches
        the narrator. The §A2 retrieval (named/present → retrieved_relationships) is
        in ``tests/game/test_relationship_retrieval.py`` and the render-to-prompt
        wiring is in ``tests/server/test_relationship_card_render_wiring.py``. The
        three together cover index → retrieve → render."""
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

        captured: list[dict] = []

        def _record(event_type: str, payload: dict, **kwargs: object) -> None:
            captured.append(payload)

        monkeypatch.setattr(dispatch_entity_sync, "_watcher_publish", _record)

        sd = _SessionData(_Snapshot([_npc_with_history("Borin")]))
        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        # (1) The relationship card reached the LIVE store via the production sweep.
        rel_ids = {c.id for c in sd.entity_store.query_by_type(EntityType.RELATIONSHIP)}
        assert "rel:borin" in rel_ids, (
            "the production sync_for_turn must land the relationship card in the "
            "live entity_store (so retrieval can recall it)"
        )

        # (2) The GM-panel watcher event reflects the relationship reproject — the
        #     lie-detector must see the card was engine-projected.
        synced = [p for p in captured if p.get("field") == "entity_sync"]
        assert synced, "the sync must publish an entity_sync watcher event"
        assert "relationship_count" in synced[-1], (
            "the published entity_sync event must carry relationship_count so the "
            "GM panel does not under-report the relationship index"
        )
        assert synced[-1]["relationship_count"] == 1
