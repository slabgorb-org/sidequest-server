"""Story 45-23 — wire-first boundary tests for the arc-promotion
chapter writeback pipeline.

The test that catches Felix's bug. Drives the narrator dispatch path
(``_execute_narration_turn``) across a Fresh→Early tier transition with
chapters carrying real content (narrative_log + lore strings) and
asserts that the seeding pipeline:

1. Mutates ``snapshot.narrative_log`` with ``entry_type='arc_
   promotion'`` rows so the next narrator's ``state_summary`` sees them.
2. Mutates ``sd.lore_store`` with ``lore_arc_<chapter_id>_*`` fragments
   carrying ``embedding_pending=True`` so the existing per-turn
   ``_dispatch_embed_worker`` picks them up.
3. Persists each appended ``NarrativeEntry`` via
   ``sd.store.append_narrative`` so the durable narrative_log table
   carries the arc rows across save/reload (AC6).
4. Emits the three lie-detector spans on the GM panel (
   ``arc_embedding_seed`` / ``narrative_log_writeback`` /
   ``lore_writeback``) so Sebastien's panel can verify Lane B's
   throughput on each promotion turn (per CLAUDE.md OTEL principle).

Per the wire-first workflow the test must hit the outermost reachable
layer; here that is the post-``record_interaction()`` site inside the
narration turn. Asserting on the snapshot fields and the OTEL exporter
is equivalent to asserting on the JSON the next narrator will receive
plus the events the panel will plot.

Felix's Playtest 3 (2026-04-19, evropi session) reached turn 71 with
``narrative_log`` carrying only per-turn appends and ``lore_store``
carrying only chargen-time fragments. The bug was a missing call site
on the post-``record_interaction`` seam; the test that catches it must
exercise that seam.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.history_chapter import (
    ChapterNarrativeEntry,
    HistoryChapter,
)
from sidequest.game.lore_store import LoreCategory, LoreSource, LoreStore
from sidequest.game.world_materialization import ARC_RECOMPUTE_INTERVAL
from sidequest.telemetry.setup import init_tracer
from tests.server.conftest import _build_turn_context_for_test


@pytest.fixture
def otel_capture():
    """Install an in-memory span exporter on the current TracerProvider."""

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()
        exporter.clear()


def _content_chapters() -> list[HistoryChapter]:
    """Three-tier synthetic chapters with real content on the ``early``
    tier so the Fresh→Early transition has something to seed.

    Mid + Veteran chapters carry minimal content — they don't promote
    on the round-10 transition tested here, but the helper that consumes
    chapters_added must not double-touch them.
    """

    return [
        HistoryChapter(
            id="early",
            label="Early arc",
            narrative_log=[
                ChapterNarrativeEntry(
                    speaker="narrator",
                    text=(
                        "The keep stirs. A year of empty halls cracked open by a single footfall."
                    ),
                ),
                ChapterNarrativeEntry(
                    speaker="Rux",
                    text="Then we go deeper. The silence has waited long enough.",
                ),
            ],
            lore=[
                "The keep was abandoned in the Year of Black Salt.",
                "Wolves carry the scent of old gold from the cellars.",
            ],
        ),
        HistoryChapter(
            id="mid",
            label="Mid arc",
            narrative_log=[],
            lore=["Mid-tier filler — should not seed at Fresh→Early."],
        ),
        HistoryChapter(
            id="veteran",
            label="Veteran arc",
            narrative_log=[],
            lore=["Veteran-tier filler — should not seed at Fresh→Early."],
        ),
    ]


def _wire_for_fresh_to_early_transition(sd) -> None:
    """Configure session state so the next ``_execute_narration_turn``
    call lands at a cadence boundary AND crosses Fresh→Early.

    Pre-call interaction = ARC_RECOMPUTE_INTERVAL - 1; post-bump value
    is exactly the cadence boundary. round_value = 10 puts the snapshot
    in the Early maturity tier so the recompute crosses Fresh→Early
    (snapshot.world_history starts empty = Fresh).
    """

    sd.cached_history_chapters = _content_chapters()
    sd.snapshot.turn_manager.interaction = ARC_RECOMPUTE_INTERVAL - 1
    sd.snapshot.turn_manager.round = 10
    # The fixture defaults ``sd.lore_store`` to an empty LoreStore from
    # _SessionData's default_factory; explicit re-bind documents intent
    # and isolates the test from any future fixture-side seeding.
    sd.lore_store = LoreStore()


# ---------------------------------------------------------------------------
# AC1 — narrative_log writeback from the dispatch seam.
# ---------------------------------------------------------------------------


class TestNarrativeLogWritebackFromDispatch:
    """Drive ``_execute_narration_turn`` end-to-end and assert that the
    snapshot carries arc-promotion narrative entries after the turn
    returns. The next narrator's ``state_summary`` is built off this
    snapshot, so asserting on snapshot fields is equivalent to
    asserting on the JSON the next turn will receive.
    """

    @pytest.mark.asyncio
    async def test_promotion_appends_arc_entries_to_snapshot_narrative_log(
        self, session_fixture, otel_capture
    ) -> None:
        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        # Felix's bug reproducer: after this turn the in-snapshot
        # narrative_log MUST carry arc-promotion rows. Filter on the
        # entry_type so we don't mistake the per-turn player+narrator
        # appends (entry_type=None) for the arc seeding output.
        arc_entries = [e for e in sd.snapshot.narrative_log if e.entry_type == "arc_promotion"]
        assert len(arc_entries) == 2, (
            "Wire-first failure: dispatch seam ran but no arc-promotion "
            "entries landed on snapshot.narrative_log. Felix's evropi "
            f"reproducer. snapshot.narrative_log={sd.snapshot.narrative_log!r}"
        )
        # The entries must carry the chapter's narrative_log content
        # verbatim (per context-story-45-23.md "Seeding helper" §1).
        contents = [e.content for e in arc_entries]
        assert any("keep stirs" in c for c in contents)
        assert any("deeper" in c for c in contents)

    @pytest.mark.asyncio
    async def test_promotion_persists_arc_entries_via_store(
        self, session_fixture, otel_capture
    ) -> None:
        """Durable write — sd.store.append_narrative MUST be called for
        each arc entry. Felix's bug was a silent absence of this call;
        the test asserts the call count > 0 with the arc-typed payload.
        """

        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        # session_fixture mocks sd.store.append_narrative. The per-turn
        # narration handler also calls it (player + narrator entry); the
        # arc seeding adds 2 more (one per ChapterNarrativeEntry on the
        # Early chapter). Total >= 4 with at least 2 carrying
        # entry_type=arc_promotion.
        all_calls = [c.args[0] for c in sd.store.append_narrative.call_args_list]
        arc_persisted = [
            entry for entry in all_calls if getattr(entry, "entry_type", None) == "arc_promotion"
        ]
        assert len(arc_persisted) == 2, (
            "Each ChapterNarrativeEntry must produce one persistence "
            f"call with entry_type='arc_promotion'. Got: {arc_persisted!r}"
        )


# ---------------------------------------------------------------------------
# AC2 — lore_store writeback + worker-handoff readiness.
# ---------------------------------------------------------------------------


class TestLoreStoreWritebackFromDispatch:
    @pytest.mark.asyncio
    async def test_promotion_mints_lore_arc_fragments(self, session_fixture, otel_capture) -> None:
        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        arc_ids = [fid for fid in sd.lore_store.fragments if fid.startswith("lore_arc_early_")]
        assert len(arc_ids) == 2, (
            "Wire-first failure: dispatch seam ran but no arc lore "
            f"fragments landed on lore_store. Got fragments: "
            f"{list(sd.lore_store.fragments)!r}"
        )
        # Per AC2: category History, source GameEvent.
        for fid in arc_ids:
            frag = sd.lore_store.fragments[fid]
            assert frag.category == LoreCategory.History
            assert frag.source == LoreSource.GameEvent

    @pytest.mark.asyncio
    async def test_seeded_fragments_are_pending_for_next_embed_worker(
        self, session_fixture, otel_capture
    ) -> None:
        """The whole point of seeding into ``lore_store`` is that the
        existing ``_dispatch_embed_worker`` (session_handler.py:2892)
        picks the new fragments up on the immediate next turn via
        ``lore_store.pending_embedding_ids()``. Assert the worker's
        input queue includes the seeded ids — the seam between 45-23
        and the existing embed worker.
        """

        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        pending = sd.lore_store.pending_embedding_ids()
        assert "lore_arc_early_0" in pending, (
            "Worker handoff seam broken: the seeded fragment is not on "
            "the embed-worker's pending queue. _dispatch_embed_worker "
            "reads pending_embedding_ids() and the next turn's worker "
            f"would silently miss the new arc lore. Pending: {pending}"
        )
        assert "lore_arc_early_1" in pending


# ---------------------------------------------------------------------------
# AC3 — OTEL spans fire from the dispatch seam.
# ---------------------------------------------------------------------------


class TestOtelSpansFromDispatch:
    """The lie-detector signal Sebastien needs on the GM panel. Per
    CLAUDE.md OTEL principle, every backend fix that touches a
    subsystem must emit OTEL spans so the panel can verify the path
    is engaged.
    """

    @pytest.mark.asyncio
    async def test_arc_embedding_seed_span_fires_per_promoted_chapter(
        self, session_fixture, otel_capture
    ) -> None:
        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        seeds = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "world_history.arc_embedding_seed"
        ]
        # Fresh→Early promotes exactly the ``early`` chapter; the seed
        # span fires once per promoted chapter (per AC3).
        assert len(seeds) == 1, (
            "arc_embedding_seed must fire once per promoted chapter "
            "on a Fresh→Early transition. Spans seen: "
            f"{[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = seeds[0].attributes or {}
        # Per context-story-45-23.md OTEL §1: the seed span carries
        # counts so the GM panel can chart Lane B throughput.
        assert attrs.get("chapter_id") == "early"
        assert attrs.get("narrative_entries_appended") == 2
        assert attrs.get("lore_fragments_minted") == 2
        assert attrs.get("lore_fragments_skipped_duplicate") == 0
        assert int(attrs.get("content_bytes_seeded", 0)) > 0

    @pytest.mark.asyncio
    async def test_narrative_log_writeback_span_fires_per_chapter(
        self, session_fixture, otel_capture
    ) -> None:
        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        writebacks = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "world_history.narrative_log_writeback"
        ]
        assert len(writebacks) == 1, (
            "narrative_log_writeback must fire once per promoted "
            "chapter; ``early`` carries 2 narrative entries → 1 span "
            "per chapter, attributes carry entries_count=2. Spans: "
            f"{[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = writebacks[0].attributes or {}
        assert attrs.get("chapter_id") == "early"
        assert attrs.get("entries_count") == 2
        assert attrs.get("entry_type") == "arc_promotion"

    @pytest.mark.asyncio
    async def test_lore_writeback_span_fires_per_minted_fragment(
        self, session_fixture, otel_capture
    ) -> None:
        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        lore_writes = [
            s for s in otel_capture.get_finished_spans() if s.name == "world_history.lore_writeback"
        ]
        # ``early`` carries 2 lore strings → 2 fragment writes → 2 spans.
        assert len(lore_writes) == 2, (
            "lore_writeback must fire once per minted fragment "
            "(2 lore strings on ``early``). Spans seen: "
            f"{[s.name for s in otel_capture.get_finished_spans()]}"
        )
        # Each carries the load-bearing pending_embedding=True so the
        # panel can verify the worker handoff seam is intact.
        for span in lore_writes:
            attrs = span.attributes or {}
            assert attrs.get("pending_embedding") is True, (
                "lore_writeback.pending_embedding must be True so the "
                "GM panel confirms the fragment will be picked up by "
                "the next embed worker pass. Got: "
                f"{dict(attrs)!r}"
            )

    @pytest.mark.asyncio
    async def test_arc_embedding_seed_fires_alongside_45_19_arc_promoted(
        self, session_fixture, otel_capture
    ) -> None:
        """Sibling-seam check: 45-19 emits ``arc_promoted`` on the
        same turn 45-23 emits ``arc_embedding_seed``. They must both
        fire on a Fresh→Early transition; if either is silent the
        panel cannot tell whether the upstream ticked or whether the
        downstream consumed.
        """

        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        names = [s.name for s in otel_capture.get_finished_spans()]
        assert "world_history.arc_promoted" in names, (
            "45-19's arc_promoted must fire on the transition turn; "
            "without it the seeding helper has no diff to consume. "
            f"Spans seen: {names}"
        )
        assert "world_history.arc_embedding_seed" in names, (
            "45-23's arc_embedding_seed must fire on the transition "
            "turn; without it the GM panel cannot see Lane B engaged. "
            f"Spans seen: {names}"
        )


# ---------------------------------------------------------------------------
# AC5 — non-promoted chapters do NOT seed (idempotent re-tick safety).
# ---------------------------------------------------------------------------


class TestNonPromotedChaptersAreNotReseeded:
    """The ``mid`` and ``veteran`` chapters in the fixture's chapter
    list are NOT promoted on a Fresh→Early transition (they're above
    the target tier). The seeding helper must consume only the diff,
    never the full applicable list — otherwise a re-tick would
    re-seed the chargen-time Fresh chapter and double-count entries.
    """

    @pytest.mark.asyncio
    async def test_only_promoted_chapter_is_seeded(self, session_fixture, otel_capture) -> None:
        sd, handler = session_fixture
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        _wire_for_fresh_to_early_transition(sd)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

        # Only ``early``'s lore strings should land — never ``mid`` or
        # ``veteran``, which are above the Early tier.
        all_arc_ids = [fid for fid in sd.lore_store.fragments if fid.startswith("lore_arc_")]
        assert all(fid.startswith("lore_arc_early_") for fid in all_arc_ids), (
            "Non-promoted chapters seeded into lore_store. The helper "
            f"must consume chapters_added only. Got: {all_arc_ids}"
        )
