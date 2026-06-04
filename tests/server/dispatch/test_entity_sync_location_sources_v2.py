"""RED-phase tests for Story 76-11 — location source coverage v2.

Story 76-7 wired only ONE of the three named location sources into the ADR-118
universal-retrieval index: PG ``location_promotions`` (``source="promotion"``).
The two remaining sources stayed deferred (documented Dev/Reviewer follow-up):

  1. **Room-graph rooms** — regions the party has discovered. ``RoomState`` carries
     no prose; the description resolves through ``location_view.get_location_prose``
     against the world's authored cartography. Tag: ``source="room_graph"``.
  2. **world_materialization outputs** — regions a materialized history chapter
     placed the party in. ``snapshot.world_history`` is written ONLY by
     ``world_materialization`` (``materialize_world``), so it is the
     materialization-source signal. Tag: ``source="world_materialization"``.

This story also carries two Reviewer findings from 76-7:

  3. **Batch the promotion read (perf)** — ``_collect_location_views`` runs one
     ``list_location_promotions`` DB round-trip *per discovered region, every
     turn* (O(N)). The read must be batched to a single call.
  4. **Surface read failures (observability)** — a per-region read failure is
     currently ``logger.exception``-logged and ``continue``d: no watcher event,
     no ``result.failed`` increment. The GM panel (the lie detector) is blind to
     a dropped region. Per the OTEL Observability Principle it MUST emit a watcher
     event AND count the failure.

All tests drive the REAL production seam
``sidequest.server.dispatch.entity_sync.sync_for_turn`` (the function
``_execute_narration_turn`` runs every turn) against a frozen-fixture genre pack
— behavior + emitted watcher events only, never source text (project rule "No
Source-Text Wiring Tests"). Because each AC1/AC2/AC4 case flows end-to-end through
``sync_for_turn``, they double as the AC5 wiring proof: an adapter that exists but
is never reached from production cannot turn these green.

The ``tests/server`` autouse fixture ``_fixture_pack_search_paths`` repoints the
loader at ``tests/fixtures/packs``, so ``caverns_and_claudes`` resolves to the
frozen fixture world ``flickering_reach`` (real authored ``cartography.regions``
prose), NOT live ``sidequest-content``.

INTENTIONALLY RED until 76-11 lands:
- ``_collect_location_views`` reads only PG promotions, so neither a discovered
  region nor a materialized-chapter location ever projects a card → AC1/AC2 fail.
- The promotion read is one call per region → AC3's ``call_count == 1`` fails.
- A read failure is swallowed silently → AC4's watcher-event and ``failed``-count
  assertions fail.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.entity_card import EntityType
from sidequest.game.history_chapter import HistoryChapter
from sidequest.server.dispatch import entity_sync as dispatch_entity_sync


def _capture_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch the dispatch watcher (patch where USED — lang-review #6) and return
    the captured payload list."""
    captured: list[dict] = []

    def _record(event_type: str, payload: dict, **kwargs: object) -> None:
        captured.append(payload)

    monkeypatch.setattr(dispatch_entity_sync, "_watcher_publish", _record)
    return captured


# ---------------------------------------------------------------------------
# AC1 — room-graph rooms: a discovered region indexes via authored-prose
# resolution, tagged source="room_graph".
# ---------------------------------------------------------------------------


class TestRoomGraphLocationFlowsEndToEnd:
    def test_discovered_region_indexes_as_room_graph_sourced_card(
        self, session_handler_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A region the party has discovered projects into the index as a
        ``LOCATION`` card tagged ``source="room_graph"``, carrying the region's
        REAL authored cartography prose (No Stubbing — the description comes from
        ``location_view``, never a synthesized placeholder), and the GM-panel
        ``location_count`` reflects it. Today ``_collect_location_views`` reads
        only PG promotions → no room-graph card → RED."""
        captured = _capture_watcher(monkeypatch)
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        sd.world_slug = "flickering_reach"
        sd.snapshot.discovered_regions = ["glass_flat"]
        # No promotions for this region — isolate the room-graph source so the
        # only thing that can produce a card is the new adapter.
        sd.repository.list_location_promotions = MagicMock(return_value=[])

        dispatch_entity_sync.sync_for_turn(handler, sd)

        loc_cards = sd.entity_store.query_by_type(EntityType.LOCATION)
        room_cards = [c for c in loc_cards if c.metadata.get("source") == "room_graph"]
        assert room_cards, (
            "a discovered region must index as a room_graph-sourced LOCATION card; "
            f"got {[(c.id, c.metadata) for c in loc_cards]!r}"
        )
        # The card embeds the region's authored cartography description, not a stub.
        assert any("fused black glass" in c.content for c in room_cards), (
            "the room_graph card must carry the region's real authored prose; "
            f"got {[c.content for c in room_cards]!r}"
        )
        synced = [p for p in captured if p.get("op") == "synced"]
        assert synced and synced[0]["location_count"] >= 1

    def test_unchanged_room_graph_location_does_not_rearm_on_resync(
        self, session_handler_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-118 §D2 zero-byte-leak: a second sync over an unchanged discovered
        region re-arms no card (the embedding the worker computed survives) —
        mirrors the 76-7 faction idempotency guard for the new room-graph source."""
        _capture_watcher(monkeypatch)
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        sd.world_slug = "flickering_reach"
        sd.snapshot.discovered_regions = ["glass_flat"]
        sd.repository.list_location_promotions = MagicMock(return_value=[])
        dispatch_entity_sync.sync_for_turn(handler, sd)
        room_cards = [
            c
            for c in sd.entity_store.query_by_type(EntityType.LOCATION)
            if c.metadata.get("source") == "room_graph"
        ]
        assert room_cards, "precondition: the room-graph card must index on the first sync"
        card = room_cards[0]
        # Pretend the embed worker ran so the card is settled.
        card.embedding = [1.0, 0.0]
        card.embedding_pending = False

        dispatch_entity_sync.sync_for_turn(handler, sd)

        assert sd.entity_store.cards[card.id].embedding_pending is False


# ---------------------------------------------------------------------------
# AC2 — world_materialization: a materialized chapter's location indexes,
# tagged source="world_materialization".
# ---------------------------------------------------------------------------


class TestMaterializationLocationFlowsEndToEnd:
    def test_materialized_chapter_location_indexes_as_materialization_sourced_card(
        self, session_handler_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A region brought in by ``world_materialization`` projects into the
        index as a ``LOCATION`` card tagged ``source="world_materialization"``,
        carrying the region's real authored prose, and the GM-panel
        ``location_count`` reflects it. ``snapshot.world_history`` is written ONLY
        by ``materialize_world``, so a located chapter there is the
        materialization-source signal. Today nothing reads it → RED."""
        captured = _capture_watcher(monkeypatch)
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        sd.world_slug = "flickering_reach"
        # Materialization output: an applied history chapter that placed the
        # party in an authored region. discovered_regions stays empty so the
        # room-graph source cannot also claim this region (source isolation).
        sd.snapshot.world_history = [HistoryChapter(id="early", location="vault_echo")]
        sd.snapshot.discovered_regions = []
        sd.repository.list_location_promotions = MagicMock(return_value=[])

        dispatch_entity_sync.sync_for_turn(handler, sd)

        loc_cards = sd.entity_store.query_by_type(EntityType.LOCATION)
        mat_cards = [c for c in loc_cards if c.metadata.get("source") == "world_materialization"]
        assert mat_cards, (
            "a materialized chapter location must index as a "
            "world_materialization-sourced LOCATION card; "
            f"got {[(c.id, c.metadata) for c in loc_cards]!r}"
        )
        assert any("Vaultborn bunker" in c.content for c in mat_cards), (
            "the materialization card must carry the region's real authored prose; "
            f"got {[c.content for c in mat_cards]!r}"
        )
        synced = [p for p in captured if p.get("op") == "synced"]
        assert synced and synced[0]["location_count"] >= 1


# ---------------------------------------------------------------------------
# AC3 — the per-region promotion read is batched (Reviewer perf finding).
# ---------------------------------------------------------------------------


class TestPromotionReadIsBatched:
    def test_promotion_read_does_not_scale_with_region_count(
        self, session_handler_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reviewer perf finding: ``_collect_location_views`` reads
        ``list_location_promotions`` once PER discovered region, every turn. With
        three discovered regions that is three DB round-trips; it must collapse to
        a single batched read. The spy accepts any argument shape (Dev may switch
        ``region_id=`` to a batched ``region_ids=``) — only the call COUNT is
        pinned. Today: 3 calls → RED."""
        _capture_watcher(monkeypatch)
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        sd.world_slug = "flickering_reach"
        sd.snapshot.discovered_regions = ["glass_flat", "vault_echo", "bone_wind"]
        spy = MagicMock(return_value=[])
        sd.repository.list_location_promotions = spy

        dispatch_entity_sync.sync_for_turn(handler, sd)

        assert spy.call_count == 1, (
            "expected a single batched promotion read for 3 discovered regions, "
            f"got {spy.call_count} (one round-trip per region is the O(N) cost "
            "the Reviewer flagged)"
        )


# ---------------------------------------------------------------------------
# AC4 — a per-region read failure is observable: watcher event + failed count
# (Reviewer observability finding / OTEL Observability Principle).
# ---------------------------------------------------------------------------


class TestPromotionReadFailureIsObservable:
    def test_read_failure_publishes_a_watcher_event_referencing_the_region(
        self, session_handler_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OTEL Observability Principle: when a region's promotion read fails, the
        GM panel (lie detector) must SEE that the region was dropped. Today the
        failure is a silent ``logger.exception`` + ``continue`` — no watcher event
        → RED. The failure event must signal failure (``op`` contains "fail") AND
        reference the failed region, distinguishing a per-region drop from a
        whole-sweep failure."""
        captured = _capture_watcher(monkeypatch)
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        sd.world_slug = "flickering_reach"
        sd.snapshot.discovered_regions = ["vault_echo"]

        def _boom(*args: object, **kwargs: object) -> list:
            raise RuntimeError("stale foreign key in location_promotions")

        sd.repository.list_location_promotions = _boom

        # A bad region read must NOT cost the player their turn (ADR-006).
        dispatch_entity_sync.sync_for_turn(handler, sd)

        failure_events = [
            p
            for p in captured
            if "fail" in str(p.get("op", "")).lower() and "vault_echo" in repr(p)
        ]
        assert failure_events, (
            "a per-region promotion read failure must publish a watcher event "
            f"referencing the dropped region; captured={captured!r}"
        )

    def test_read_failure_increments_the_failed_count(
        self, session_handler_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reviewer finding: a dropped region must increment ``result.failed`` so
        the GM-panel count reflects the under-report instead of silently reading
        a healthy 0. The sweep must still publish its summary event (the turn
        survives). Today the collection failure is swallowed and uncounted →
        ``failed`` is 0 → RED."""
        captured = _capture_watcher(monkeypatch)
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        sd.world_slug = "flickering_reach"
        sd.snapshot.discovered_regions = ["vault_echo"]
        sd.repository.list_location_promotions = MagicMock(
            side_effect=RuntimeError("stale foreign key in location_promotions")
        )

        dispatch_entity_sync.sync_for_turn(handler, sd)

        synced = [p for p in captured if p.get("op") == "synced"]
        assert synced, (
            "the sweep must still publish its summary event after a degraded "
            f"location read (the turn survives); captured={captured!r}"
        )
        assert synced[0]["failed"] >= 1, (
            "a per-region read failure must count toward 'failed' so the GM panel "
            f"sees the dropped region; got {synced[0]!r}"
        )
