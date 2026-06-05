"""Story 84-5 (WI-2) — lifecycle render wiring + active-not-double-rendered + e2e (RED).

Two halves the projector unit tests do NOT cover (the 84-3 lessons):

  AC-7 — DORMANT → narrator prompt: a retrieved dormant quest/trope must RENDER
  into the assembled prompt (a `retrieved_quests`/`retrieved_tropes` Valley section,
  the 84-3 seam) — or it is dead code.

  AC-8 — ACTIVE → existing floor, NOT double-rendered, NOT indexed: an ACTIVE quest
  already reaches the narrator via `state_summary`; a PROGRESSING trope via the trope
  foreground path. 84-5 must NOT add a second active-render path and must NOT index
  the active ones. (The INVERTED 84-3 trap: double-render instead of dead-render.)

  AC-11 — the two e2e wiring paths driven through the real `_build_turn_context` /
  `sync_for_turn` seams.

Behavior + assembled-prompt sections only (No Source-Text Wiring Tests). Run `-n0`.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.agents.npc_context import build_npc_working_set
from sidequest.agents.prompt_framework.types import AttentionZone
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.game.turn import TurnManager
from tests._helpers.doubles import make_orchestrator


def _empty_floor() -> Any:
    snap = GameSnapshot(genre_slug="caverns_and_claudes", turn_manager=TurnManager(interaction=1))
    return build_npc_working_set(snap, current_turn=1)


def _retrieved(*, quests=None, tropes=None) -> Any:
    """A ``RetrievedEntities`` carrying dormant quest/trope cards (the §A2 recall
    output), everything else empty."""
    from sidequest.game.retrieval_orchestration import RetrievedEntities

    return RetrievedEntities(
        floor=_empty_floor(),
        retrieved_npcs=None,
        retrieved_locations=None,
        retrieved_factions=None,
        budget_total=4000,
        floor_count=0,
        floor_token_cost=0,
        fill_candidate_count=0,
        fill_selected_count=0,
        fill_token_cost=0,
        rejected_below_similarity=0,
        dimension_mismatch_count=0,
        outcome="success",
        retrieved_quests=quests,
        retrieved_tropes=tropes,
    )


def _quest_card() -> EntityCard:
    return EntityCard.new(EntityType.QUEST, "q_smuggler", content="The Smuggler's Debt — completed")


def _trope_card() -> EntityCard:
    return EntityCard.new(EntityType.TROPE, "redemption", content="Redemption Arc — a second chance")


# ===========================================================================
# AC-7 — dormant quest/trope renders into the narrator prompt
# ===========================================================================


class TestDormantRendersIntoPrompt:
    @pytest.mark.asyncio
    async def test_dormant_quest_renders_into_prompt(self, session_handler_factory) -> None:
        from sidequest.server.session_helpers import _build_turn_context

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        context = _build_turn_context(sd, entity_retrieval=_retrieved(quests=[_quest_card()]))

        orch = make_orchestrator()
        _prompt, registry = await orch.build_narrator_prompt("what about the smuggler?", context)
        agent_name = orch._narrator.name()
        quest_sections = [
            s
            for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)
            if s.name == "retrieved_quests"
        ]
        assert len(quest_sections) == 1, (
            "a retrieved dormant quest must register a retrieved_quests Valley section "
            "(the render seam — or the card is dead code)"
        )
        assert "Smuggler" in quest_sections[0].content

    @pytest.mark.asyncio
    async def test_dormant_trope_renders_into_prompt(self, session_handler_factory) -> None:
        from sidequest.server.session_helpers import _build_turn_context

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        context = _build_turn_context(sd, entity_retrieval=_retrieved(tropes=[_trope_card()]))

        orch = make_orchestrator()
        _prompt, registry = await orch.build_narrator_prompt("the redemption thread?", context)
        agent_name = orch._narrator.name()
        trope_sections = [
            s
            for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)
            if s.name == "retrieved_tropes"
        ]
        assert len(trope_sections) == 1, (
            "a retrieved dormant trope must register a retrieved_tropes Valley section"
        )
        assert "Redemption" in trope_sections[0].content

    @pytest.mark.asyncio
    async def test_no_quest_section_when_empty(self, session_handler_factory) -> None:
        """Zero-byte-leak: no dormant quest/trope → no section."""
        from sidequest.server.session_helpers import _build_turn_context

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        context = _build_turn_context(sd, entity_retrieval=_retrieved())

        orch = make_orchestrator()
        _prompt, registry = await orch.build_narrator_prompt("I look around", context)
        agent_name = orch._narrator.name()
        names = {s.name for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)}
        assert "retrieved_quests" not in names and "retrieved_tropes" not in names


# ===========================================================================
# AC-8 — ACTIVE reaches prompt via EXISTING path, NOT indexed, NOT double-rendered
# ===========================================================================


class TestActiveNotDoubleRendered:
    def test_active_quest_reaches_prompt_via_existing_path_not_index(
        self, session_handler_factory
    ) -> None:
        """An ACTIVE quest is in ``state_summary`` (its EXISTING floor path) and is
        NOT projected into the index — no double-render. This pins the routing: the
        active quest reaches the narrator the way it already does, and 84-5 does not
        add a second copy."""
        from sidequest.game.entity_card import EntityType
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        sd.snapshot.quest_log["q_live"] = QuestEntry(
            title="The Live Quest", objective="do the thing", status="active"
        )
        # Dormant CONTROL: a completed quest in the same snapshot MUST be indexed,
        # so "active not indexed" can't pass vacuously by quest-indexing being dead.
        sd.snapshot.quest_log["q_done"] = QuestEntry(
            title="Done Quest", objective="finished", status="completed"
        )

        # The live sync sweep must index ONLY the dormant quest, NOT the active one.
        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]
        indexed = {c.id for c in sd.entity_store.query_by_type(EntityType.QUEST)}
        assert "quest:q_done" in indexed, (
            "the dormant control quest MUST be indexed (routing is live, not dead)"
        )
        assert "quest:q_live" not in indexed, (
            "an ACTIVE quest must NOT be indexed (it rides the existing state_summary path)"
        )

        # And it still reaches the prompt via the existing state_summary section.
        from sidequest.server.session_helpers import _build_turn_context

        context = _build_turn_context(sd)
        assert context.state_summary is not None
        assert "q_live" in context.state_summary or "Live Quest" in context.state_summary, (
            "the active quest must still reach the prompt via the existing state_summary path"
        )

    def test_progressing_trope_renders_via_existing_path_not_retrieval(
        self, session_handler_factory
    ) -> None:
        """A PROGRESSING trope is NOT indexed (it rides the existing trope-foreground
        path). No ``retrieved_tropes`` duplicate from the index."""
        from sidequest.game.entity_card import EntityType
        from sidequest.game.session import TropeState
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        # Use REAL pack trope ids so the TropeDefinition join (name/description)
        # resolves on the live dispatch path.
        sd.snapshot.active_tropes.append(
            TropeState(id="the_keeper_stirs", status="progressing")
        )
        # Dormant CONTROL: a resolved trope MUST be indexed, so "progressing not
        # indexed" can't pass vacuously by trope-indexing being dead.
        sd.snapshot.active_tropes.append(
            TropeState(id="extraction_panic", status="resolved")
        )

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]
        indexed = {c.id for c in sd.entity_store.query_by_type(EntityType.TROPE)}
        assert "trope:extraction_panic" in indexed, (
            "the dormant control trope MUST be indexed (routing is live, not dead)"
        )
        assert "trope:the_keeper_stirs" not in indexed, (
            "a PROGRESSING trope must NOT be indexed (it rides the existing foreground path)"
        )


# ===========================================================================
# AC-10 — OTEL: the routing decision is observable
# ===========================================================================


class TestRoutingObservable:
    def test_sync_emits_quest_trope_counts_on_span(
        self, session_handler_factory, monkeypatch
    ) -> None:
        """The dormant-projection routing is observable: the entity_sync watcher
        event carries quest_count + trope_count so the GM panel sees how many notes
        were indexed."""
        from sidequest.game.session import TropeState
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        sd.snapshot.quest_log["q_done"] = QuestEntry(title="Done", status="completed")
        sd.snapshot.active_tropes.append(TropeState(id="extraction_panic", status="resolved"))

        captured: list[dict] = []
        monkeypatch.setattr(
            dispatch_entity_sync,
            "_watcher_publish",
            lambda event_type, payload, **kw: captured.append(payload),
        )
        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        synced = [p for p in captured if p.get("field") == "entity_sync"]
        assert synced, "the sync must publish an entity_sync watcher event"
        assert "quest_count" in synced[-1] and "trope_count" in synced[-1], (
            "the entity_sync event must carry quest_count + trope_count (routing observability)"
        )

    def test_sync_emits_active_quest_trope_counts(
        self, session_handler_factory, monkeypatch
    ) -> None:
        """OTEL nit (Reviewer): the routing DECISION is the active-vs-dormant SPLIT.
        Today only the DORMANT-indexed count is emitted; the ACTIVE count (items
        riding the floor, NOT indexed) is invisible, so the GM panel can't see
        "N active riding floor vs M dormant indexed." The watcher event AND the
        entity_sync span must ALSO carry ``active_quest_count`` / ``active_trope_count``.

        Snapshot: 1 active quest + 1 completed (dormant) quest; 1 progressing trope
        + 1 resolved (dormant) trope → active counts must be 1 each, dormant 1 each."""
        from sidequest.game.session import TropeState
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        sd.snapshot.quest_log["q_live"] = QuestEntry(title="Live", status="active")
        sd.snapshot.quest_log["q_done"] = QuestEntry(title="Done", status="completed")
        sd.snapshot.active_tropes.append(TropeState(id="the_keeper_stirs", status="progressing"))
        sd.snapshot.active_tropes.append(TropeState(id="extraction_panic", status="resolved"))

        captured: list[dict] = []
        monkeypatch.setattr(
            dispatch_entity_sync,
            "_watcher_publish",
            lambda event_type, payload, **kw: captured.append(payload),
        )
        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        synced = [p for p in captured if p.get("field") == "entity_sync"]
        assert synced, "the sync must publish an entity_sync watcher event"
        ev = synced[-1]
        assert "active_quest_count" in ev and "active_trope_count" in ev, (
            "the entity_sync event must carry active_quest_count + active_trope_count so the "
            "GM panel sees the active-vs-dormant routing split, not just the dormant side"
        )
        assert ev["active_quest_count"] == 1, "one active quest rides the floor"
        assert ev["active_trope_count"] == 1, "one progressing trope rides the floor"
        # And the dormant side is the existing counters (sanity: the split is honest).
        assert ev["quest_count"] == 1 and ev["trope_count"] == 1

    def test_active_counts_on_entity_sync_span(self, session_handler_factory) -> None:
        """The active counts must also ride the ``accretion.entity_sync`` OTEL span
        (parity with quest_count/trope_count), so Jaeger sees the routing split too."""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from sidequest.game.session import TropeState
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Patch the dispatch module's tracer so the entity_sync span lands in-memory.
        import opentelemetry.trace as _t

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        sd.snapshot.quest_log["q_live"] = QuestEntry(title="Live", status="active")
        sd.snapshot.active_tropes.append(TropeState(id="the_keeper_stirs", status="progressing"))

        orig_get_tracer = _t.get_tracer
        try:
            _t.get_tracer = lambda *a, **k: provider.get_tracer("test-84-5-active")  # type: ignore[assignment]
            dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]
        finally:
            _t.get_tracer = orig_get_tracer

        spans = [s for s in exporter.get_finished_spans() if "entity_sync" in s.name]
        assert spans, "an entity_sync span must fire"
        attrs = dict(spans[-1].attributes or {})
        assert "entity_sync.active_quest_count" in attrs, (
            "the entity_sync span must carry entity_sync.active_quest_count"
        )
        assert "entity_sync.active_trope_count" in attrs, (
            "the entity_sync span must carry entity_sync.active_trope_count"
        )
        assert attrs["entity_sync.active_quest_count"] == 1
        assert attrs["entity_sync.active_trope_count"] == 1


# ===========================================================================
# Routing completeness (Reviewer Should-fix) — failed/resolved quests index live
# ===========================================================================


class TestTerminalQuestRoutingLive:
    def test_failed_and_resolved_quests_indexed_via_live_sync(
        self, session_handler_factory
    ) -> None:
        """The full Should-fix, end-to-end: failed AND resolved quests, driven
        through the live ``sync_for_turn``, ARE indexed (terminal → dormant →
        recall-able). An active quest control is NOT — so the routing is live, not
        dead. Currently failed/resolved are NOT indexed (predicate only matched
        'completed') — that's the RED."""
        from sidequest.game.entity_card import EntityType
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        sd.snapshot.quest_log["q_failed"] = QuestEntry(title="Save the village", status="failed")
        sd.snapshot.quest_log["q_resolved"] = QuestEntry(title="Broker the truce", status="resolved")
        sd.snapshot.quest_log["q_active"] = QuestEntry(title="Find the heir", status="active")

        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]
        indexed = {c.id for c in sd.entity_store.query_by_type(EntityType.QUEST)}

        assert "quest:q_failed" in indexed, "a FAILED quest must be indexed (terminal → dormant)"
        assert "quest:q_resolved" in indexed, "a RESOLVED quest must be indexed (terminal → dormant)"
        assert "quest:q_active" not in indexed, "an ACTIVE quest must NOT be indexed (rides floor)"


# ===========================================================================
# AC-11 — the two e2e wiring paths
# ===========================================================================


class TestLifecycleE2EWiring:
    def test_e2e_active_quest_to_prompt(self, session_handler_factory) -> None:
        """E2E ACTIVE path: an active quest on the live snapshot reaches the narrator
        prompt via the existing path, with NO index projection — driven through the
        live `_build_turn_context` + `sync_for_turn` seams."""
        from sidequest.game.entity_card import EntityType
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync
        from sidequest.server.session_helpers import _build_turn_context

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        sd.snapshot.quest_log["q_active"] = QuestEntry(
            title="Find the Heir", objective="locate the lost prince", status="active"
        )
        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]

        assert "quest:q_active" not in {
            c.id for c in sd.entity_store.query_by_type(EntityType.QUEST)
        }, "active quest must not be indexed"
        context = _build_turn_context(sd)
        assert context.state_summary and (
            "q_active" in context.state_summary or "Find the Heir" in context.state_summary
        ), "active quest must reach the prompt via the existing state_summary path"

    @pytest.mark.asyncio
    async def test_e2e_dormant_quest_index_to_prompt(self, session_handler_factory) -> None:
        """E2E DORMANT path: a completed quest is indexed by the live sync, then
        surfaces in retrieval and RENDERS into the narrator prompt — index →
        retrieve → render, no dead layer."""
        from sidequest.game.entity_card import EntityType
        from sidequest.server.dispatch import entity_sync as dispatch_entity_sync
        from sidequest.server.session_helpers import _build_turn_context

        sd, _h = session_handler_factory(genre="caverns_and_claudes")
        sd.snapshot.quest_log["q_done"] = QuestEntry(
            title="The Smuggler's Debt", objective="recover the ledger", status="completed"
        )

        # (1) live sync indexes the dormant quest.
        dispatch_entity_sync.sync_for_turn(handler=None, sd=sd)  # type: ignore[arg-type]
        assert "quest:q_done" in {
            c.id for c in sd.entity_store.query_by_type(EntityType.QUEST)
        }, "completed quest must be indexed by the live sync sweep"

        # (2) a retrieved dormant quest renders into the prompt (render seam).
        context = _build_turn_context(sd, entity_retrieval=_retrieved(quests=[_quest_card()]))
        orch = make_orchestrator()
        _prompt, registry = await orch.build_narrator_prompt("what about the smuggler?", context)
        agent_name = orch._narrator.name()
        quest_sections = [
            s
            for s in registry.get_sections(agent_name, zone=AttentionZone.Valley)
            if s.name == "retrieved_quests"
        ]
        assert len(quest_sections) == 1 and "Smuggler" in quest_sections[0].content, (
            "the dormant quest must reach the narrator prompt end-to-end (index→retrieve→render)"
        )
