"""Story 84-5 (WI-2) — DORMANT-ONLY sync routing + governor cap (RED phase).

ADR-118 §A2: the per-type predicate ROUTES — ACTIVE quests/tropes ride their
EXISTING floor path (state_summary / trope foreground); only DORMANT ones are
projected into the index. So ``sync_entity_cards`` must project a COMPLETED quest
and a DORMANT/RESOLVED trope, and must NOT project an ACTIVE quest or a PROGRESSING
trope (those would double-render — see the §A2 investigation).

THE CONTRACT:
  * ``sync_entity_cards`` gains quest + trope projection loops (dormant-only) and
    ``EntitySyncResult`` gains ``quest_count`` + ``trope_count`` tallies.
  * trope definitions are passed in (``tropes=``) like ``factions=`` — TropeState
    carries only ``id``; the name/description come from the TropeDefinition.

Synthetic fixtures. Run ``-n0`` (sync may emit spans).
"""

from __future__ import annotations

from sidequest.game.entity_store import EntityStore
from sidequest.game.session import QuestEntry, TropeState
from sidequest.genre.models.tropes import TropeDefinition


class _TurnManager:
    interaction = 5


class _Snapshot:
    """Minimal snapshot exposing the quest/trope sources sync reads."""

    def __init__(
        self,
        *,
        quest_log: dict[str, QuestEntry] | None = None,
        active_tropes: list[TropeState] | None = None,
    ) -> None:
        self.npc_pool: list = []
        self.npcs: list = []
        self.quest_log = quest_log or {}
        self.active_tropes = active_tropes or []
        self.turn_manager = _TurnManager()


def _defs(*pairs: tuple[str, str]) -> list[TropeDefinition]:
    return [TropeDefinition(id=i, name=n, description=f"{n} desc") for i, n in pairs]


def _ids(store: EntityStore, entity_type: str) -> set[str]:
    return {c.id for c in store.query_by_type(entity_type)}


# ===========================================================================
# AC-5 — dormant-only: dormant indexed, active NOT indexed
# ===========================================================================


class TestDormantOnlySync:
    def test_sync_indexes_dormant_quest(self) -> None:
        from sidequest.game.entity_card import EntityType
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snap = _Snapshot(
            quest_log={"q1": QuestEntry(title="The Smuggler's Debt", status="completed")}
        )
        sync_entity_cards(store, snap)
        assert "quest:q1" in _ids(store, EntityType.QUEST), (
            "a COMPLETED quest must be projected into the index (dormant note)"
        )

    def test_sync_does_not_index_active_quest(self) -> None:
        """An ACTIVE quest rides the existing state_summary floor — it must NOT be
        indexed (double-render guard)."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snap = _Snapshot(quest_log={"q_live": QuestEntry(title="Live quest", status="active")})
        sync_entity_cards(store, snap)
        assert "quest:q_live" not in _ids(store, EntityType.QUEST), (
            "an ACTIVE quest must NOT be indexed (it rides its existing floor path)"
        )

    def test_sync_indexes_dormant_trope(self) -> None:
        from sidequest.game.entity_card import EntityType
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snap = _Snapshot(active_tropes=[TropeState(id="redemption", status="resolved")])
        sync_entity_cards(store, snap, tropes=_defs(("redemption", "Redemption Arc")))
        assert "trope:redemption" in _ids(store, EntityType.TROPE), (
            "a RESOLVED trope must be projected into the index (dormant note)"
        )

    def test_sync_does_not_index_progressing_trope(self) -> None:
        """A PROGRESSING trope rides the existing trope-foreground floor — NOT
        indexed (double-render guard)."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snap = _Snapshot(active_tropes=[TropeState(id="rising", status="progressing")])
        sync_entity_cards(store, snap, tropes=_defs(("rising", "Rising Action")))
        assert "trope:rising" not in _ids(store, EntityType.TROPE), (
            "a PROGRESSING trope must NOT be indexed (it rides its existing floor path)"
        )

    def test_sync_quest_trope_counts(self) -> None:
        """``quest_count``/``trope_count`` count only the projected (dormant) cards."""
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        snap = _Snapshot(
            quest_log={
                "q_done": QuestEntry(title="Done", status="completed"),
                "q_live": QuestEntry(title="Live", status="active"),
            },
            active_tropes=[
                TropeState(id="resolved_t", status="resolved"),
                TropeState(id="live_t", status="progressing"),
            ],
        )
        result = sync_entity_cards(
            store, snap, tropes=_defs(("resolved_t", "Resolved"), ("live_t", "Live"))
        )
        assert hasattr(result, "quest_count") and hasattr(result, "trope_count")
        assert result.quest_count == 1, "only the completed quest counts"
        assert result.trope_count == 1, "only the resolved trope counts"


# ===========================================================================
# AC-9 — governor cap interplay: the active set is bounded → dormant indexed
# ===========================================================================


class TestGovernorCapInterplay:
    def test_active_trope_set_bounded_by_governor_cap(self) -> None:
        """ADR-128 caps progressing tropes at 3. With 3 progressing + N dormant,
        exactly the N dormant are indexed (the 3 active ride the floor)."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.entity_sync import sync_entity_cards

        store = EntityStore()
        tropes = [
            TropeState(id="p1", status="progressing"),
            TropeState(id="p2", status="progressing"),
            TropeState(id="p3", status="progressing"),
            TropeState(id="d1", status="dormant"),
            TropeState(id="d2", status="resolved"),
        ]
        defs = _defs(
            ("p1", "P1"), ("p2", "P2"), ("p3", "P3"), ("d1", "D1"), ("d2", "D2")
        )
        result = sync_entity_cards(store, _Snapshot(active_tropes=tropes), tropes=defs)

        indexed = _ids(store, EntityType.TROPE)
        assert indexed == {"trope:d1", "trope:d2"}, (
            "exactly the dormant tropes are indexed; the 3 progressing ride the floor"
        )
        assert result.trope_count == 2
