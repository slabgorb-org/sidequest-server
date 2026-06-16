"""Story 45-20 — wire-first boundary tests for the trope-resolution handshake.

These tests drive the narrator dispatch path
(``_execute_narration_turn``) and assert that the resolved-trope
durable-record handshake fires from the actual seam — *not* from an
isolated unit test on the diff helper. Per the wire-first workflow the
test must hit the outermost reachable layer; here that is the post-
``record_interaction()`` site inside the narration turn so the OTEL
panel can see resolution handshakes alongside every other turn-scoped
span.

Orin's Playtest 3 (2026-04-19, evropi session) reached turn ~40 with
``extraction_panic`` Resolved and ``hireling_mutiny`` mid-arc, yet
``snapshot.quest_log == {}`` and ``snapshot.active_stakes == ""``. The
narrator's state_summary advertised "no active stakes" three turns
after a major trope resolved. The bug was a missing call site (no
applier observed the resolved-status flip), not a missing helper — so
the test that catches it must exercise the call site, which is what
these tests do.

Two boundary seams are exercised:

1. Trope-state-change → resolution-handshake seam — fired from inside
   ``_execute_narration_turn`` immediately after the snapshot mutation
   phase and ``record_interaction()``. Baseline is captured at the top
   of the function (before any apply step) so any in-turn mutation is
   visible to the diff.
2. Handshake → state_summary seam — the new ``quest_log`` entry and
   updated ``active_stakes`` must be on the snapshot before the next
   narrator turn's ``state_summary_payload`` is built. The next turn is
   what consumes the output; skipping this seam leaves the fix dead.
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
from sidequest.game.session import TropeState
from sidequest.telemetry.setup import init_tracer
from tests._helpers.session_room import room_for
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


def _seed_active_trope(sd, trope_id: str, status: str) -> None:
    """Pre-seed ``sd.snapshot.active_tropes`` with one trope. Mirrors
    the state Orin's evropi save reached before the chapter-promotion
    path flipped a trope's status to ``"resolved"``.
    """

    sd.snapshot.active_tropes.append(
        TropeState(id=trope_id, status=status, progress=0.5, beats_fired=0)
    )


def _flipping_orchestrator(sd, trope_id: str) -> AsyncMock:
    """Return an AsyncMock whose side_effect mutates the trope's status
    to ``"resolved"`` during the orchestrator call. Simulates any
    future upstream (chapter promotion, engine tick, narrator extraction)
    that flips status mid-turn — the handshake's contract is that it
    observes the diff regardless of who did the flip.

    The side_effect runs AFTER the baseline is captured at the top of
    ``_execute_narration_turn`` and BEFORE the post-``record_interaction``
    handshake site. That ordering is exactly what the wire-first seam
    requires; if the implementation captures the baseline late (after
    the orchestrator call) the diff vanishes and these tests fail.
    """

    async def _side_effect(*_args, **_kwargs):
        for trope in sd.snapshot.active_tropes:
            if trope.id == trope_id:
                trope.status = "resolved"
        return NarrationTurnResult(
            narration="The chamber falls silent.",
            is_degraded=False,
            agent_duration_ms=1,
        )

    return AsyncMock(side_effect=_side_effect)


# ---------------------------------------------------------------------------
# AC1 + AC2 — quest_log entry + active_stakes update from the dispatch seam.
# ---------------------------------------------------------------------------


class TestDispatchSeamWritesDurableRecord:
    """Drive ``_execute_narration_turn`` end-to-end and assert that the
    snapshot carries the durable record after the turn returns. The
    next narrator's state_summary is built off this snapshot, so
    asserting on the snapshot fields is equivalent to asserting on the
    JSON the next turn will receive.
    """

    @pytest.mark.asyncio
    async def test_trope_resolution_writes_quest_log_entry(self, session_fixture) -> None:
        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")

        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context)

        assert "trope_extraction_panic" in sd.snapshot.quest_log, (
            "Wire-first failure: the dispatch seam ran but no quest_log "
            "entry was written. Orin's evropi save reproducer. "
            f"quest_log={dict(sd.snapshot.quest_log)}"
        )
        # Turn-marker — interaction is bumped during the turn, so the
        # entry references the post-bump value.
        entry = sd.snapshot.quest_log["trope_extraction_panic"]
        assert str(sd.snapshot.turn_manager.interaction) in entry, (
            "quest_log entry must reference the interaction count so "
            "the next narrator can anchor the resolution in time; got "
            f"entry={entry!r} interaction={sd.snapshot.turn_manager.interaction}"
        )

    @pytest.mark.asyncio
    async def test_trope_resolution_appends_active_stakes_marker(self, session_fixture) -> None:
        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        sd.snapshot.active_stakes = ""
        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")

        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context)

        assert "extraction_panic" in sd.snapshot.active_stakes, (
            "Wire-first failure: active_stakes did not gain a resolution "
            f"marker; got active_stakes={sd.snapshot.active_stakes!r}"
        )

    @pytest.mark.asyncio
    async def test_trope_resolution_preserves_existing_active_stakes(self, session_fixture) -> None:
        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        sd.snapshot.active_stakes = "Find the courier."
        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")

        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context)

        assert "Find the courier." in sd.snapshot.active_stakes, (
            "Pre-existing active_stakes content lost; got "
            f"active_stakes={sd.snapshot.active_stakes!r}"
        )
        assert "extraction_panic" in sd.snapshot.active_stakes


# ---------------------------------------------------------------------------
# AC4 — handshake span fires from the dispatch seam (lie-detector).
# ---------------------------------------------------------------------------


class TestDispatchSeamEmitsHandshakeSpan:
    """The GM panel needs the handshake span. Without it, Sebastien
    (mechanical-first) cannot tell whether a "Resolved" indicator on
    the panel came from an actual handshake or whether the narrator
    fabricated a closure beat that never wrote back.
    """

    @pytest.mark.asyncio
    async def test_dispatch_seam_emits_handshake_span_with_full_payload(
        self, session_fixture, otel_capture
    ) -> None:
        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context)

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert len(spans) == 1, (
            "Dispatch seam must emit exactly one handshake span per "
            "freshly resolved trope; "
            f"got {[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("trope_id") == "extraction_panic"
        assert attrs.get("prior_status") == "progressing"
        assert attrs.get("new_status") == "resolved"
        assert attrs.get("active_stakes_appended") is True
        assert attrs.get("quest_log_key") == "trope_extraction_panic"
        # interaction is the post-record_interaction value because the
        # handshake fires AFTER record_interaction.
        assert attrs.get("interaction") == sd.snapshot.turn_manager.interaction

    @pytest.mark.asyncio
    async def test_no_handshake_span_when_no_trope_resolved(
        self, session_fixture, otel_capture
    ) -> None:
        """Negative: a normal turn with no trope status flips emits no
        handshake span. The bug failure mode (silent-never-fires) is
        the opposite test; this guards against false-positives.
        """

        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        # Orchestrator does NOT flip the status this turn.
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="Calm settles.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I wait.", turn_context)

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert spans == [], (
            "Handshake span fired when no trope transitioned to "
            "resolved — false-positive emit. "
            f"Spans seen: {[s.name for s in otel_capture.get_finished_spans()]}"
        )


# ---------------------------------------------------------------------------
# AC4 — idempotent re-detect on the second turn (the lie-detector).
# ---------------------------------------------------------------------------


class TestIdempotentReDetectAcrossTurns:
    """After a trope resolves on turn N, every subsequent turn fires
    the handshake span with ``active_stakes_appended=False`` (because
    the write is a no-op) so the GM panel can distinguish "handshake
    correctly idempotent" from "handshake never engaged after turn N".
    """

    @pytest.mark.asyncio
    async def test_second_turn_emits_idempotent_handshake_span(
        self, session_fixture, otel_capture
    ) -> None:
        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")

        # Turn 1: resolution happens.
        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")
        turn_context_1 = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context_1)

        first_quest_log_value = sd.snapshot.quest_log.get("trope_extraction_panic")
        first_active_stakes = sd.snapshot.active_stakes

        # Turn 2: trope already resolved; orchestrator does NOT flip
        # anything (and the trope is still "resolved" on the snapshot).
        otel_capture.clear()
        sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="The aftermath drifts.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )
        turn_context_2 = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I look at the wreckage.", turn_context_2)

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert len(spans) == 1, (
            "Idempotent re-detect must STILL emit exactly one handshake "
            "span on every subsequent turn — the lie-detector signal. "
            f"Spans seen: {[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("active_stakes_appended") is False, (
            "Idempotent re-detect must report active_stakes_appended=False "
            f"so the panel can distinguish first-write from re-detect. "
            f"Attrs: {attrs}"
        )

        assert sd.snapshot.quest_log["trope_extraction_panic"] == (first_quest_log_value), (
            "Idempotent re-detect must NOT rewrite the quest_log entry."
        )
        assert sd.snapshot.active_stakes == first_active_stakes, (
            "Idempotent re-detect must NOT append a second resolution marker."
        )


# ---------------------------------------------------------------------------
# AC1 — handshake-to-state_summary timing seam — the next narrator's
# state_summary JSON must contain the entry. The state_summary is built
# from ``snapshot.model_dump_json()``; asserting on the JSON proves the
# entry is reachable to the next turn's narrator prompt and not, e.g.,
# stashed on a transient _SessionData field that never serializes.
# ---------------------------------------------------------------------------


class TestStateSummaryTimingSeam:
    @pytest.mark.asyncio
    async def test_quest_log_entry_appears_in_snapshot_json(self, session_fixture) -> None:
        import json

        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")

        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context)

        # state_summary_payload at session_helpers.py:312 is built via
        # ``json.loads(snapshot.model_dump_json())``. Asserting on the
        # same call here proves the next narrator turn will see the
        # entry — the bug Orin saw was a snapshot field that never
        # serialized into the prompt JSON.
        payload = json.loads(sd.snapshot.model_dump_json())
        assert "quest_log" in payload
        assert "trope_extraction_panic" in payload["quest_log"], (
            "quest_log entry must serialize into snapshot.model_dump_json() "
            "so the next narrator's state_summary carries it. "
            f"payload['quest_log']={payload['quest_log']}"
        )
        assert "extraction_panic" in payload.get("active_stakes", ""), (
            "active_stakes resolution marker must serialize into "
            "snapshot.model_dump_json() so the next narrator sees it."
        )


# ---------------------------------------------------------------------------
# AC3 — durability across save/reload. The diff baseline is taken from
# the live snapshot, so the same ``resolved`` status looks unchanged on
# the next post-reload turn — the idempotency span fires but no second
# write occurs.
# ---------------------------------------------------------------------------


class TestSaveReloadDurability:
    @pytest.mark.asyncio
    async def test_quest_log_entry_survives_save_reload(self, session_fixture, tmp_path) -> None:
        from sidequest.game.persistence import SqliteStore

        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")

        # Use a real on-disk store so we can close + re-open it.
        store_path = str(tmp_path / "save.db")
        sd.store = SqliteStore.open(store_path)
        sd.snapshot.world_slug = "test_world"

        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context)

        # Capture the entry that was written this turn.
        entry_before = sd.snapshot.quest_log.get("trope_extraction_panic")
        active_stakes_before = sd.snapshot.active_stakes
        assert entry_before is not None, (
            "Pre-condition: turn 1 must have written the quest_log entry "
            "before save/reload is meaningful."
        )

        # Persist explicitly — the dispatch already saves once via
        # sd.store.save(snapshot), but we save again to be defensive.
        sd.store.save(sd.snapshot)

        # Reload via a fresh store handle on the same DB.
        reloaded_store = SqliteStore.open(store_path)
        saved = reloaded_store.load()
        assert saved is not None, (
            "Persistence path returned None on reload — save did not round-trip the snapshot."
        )
        reloaded = saved.snapshot

        assert reloaded.quest_log.get("trope_extraction_panic") == entry_before, (
            "AC3 failure: quest_log entry did not survive save/reload. "
            f"Pre-save: {entry_before!r}; reloaded: "
            f"{reloaded.quest_log.get('trope_extraction_panic')!r}"
        )
        assert "extraction_panic" in reloaded.active_stakes, (
            "AC3 failure: active_stakes resolution marker did not survive "
            f"save/reload. Pre-save: {active_stakes_before!r}; reloaded: "
            f"{reloaded.active_stakes!r}"
        )

    @pytest.mark.asyncio
    async def test_post_reload_first_turn_does_not_double_write(
        self, session_fixture, otel_capture, tmp_path
    ) -> None:
        """Story context paranoia check: the diff baseline is captured
        from the live snapshot at the top of ``_execute_narration_turn``,
        so on the post-reload first turn the baseline status is already
        ``"resolved"`` — the diff predicate sees no transition and does
        NOT rewrite. The handshake span still fires (idempotent
        re-detect) so the panel sees the path engaged.
        """

        from sidequest.game.persistence import SqliteStore
        from sidequest.server.session_handler import (
            WebSocketSessionHandler,
            _SessionData,
        )

        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        store_path = str(tmp_path / "save.db")
        sd.store = SqliteStore.open(store_path)
        sd.snapshot.world_slug = "test_world"

        # Turn 1: resolution.
        sd.orchestrator.run_narration_turn = _flipping_orchestrator(sd, "extraction_panic")
        turn_context_1 = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I push the door open.", turn_context_1)
        entry_after_turn_1 = sd.snapshot.quest_log["trope_extraction_panic"]
        active_stakes_after_turn_1 = sd.snapshot.active_stakes

        # Persist + reload into a fresh _SessionData/handler.
        sd.store.save(sd.snapshot)
        reloaded_store = SqliteStore.open(store_path)
        saved = reloaded_store.load()
        assert saved is not None
        reloaded_snap = saved.snapshot
        new_sd = _SessionData(
            genre_slug=sd.genre_slug,
            world_slug=sd.world_slug,
            player_name=sd.player_name,
            player_id=sd.player_id,
            snapshot=reloaded_snap,
            store=reloaded_store,
            genre_pack=sd.genre_pack,
            orchestrator=sd.orchestrator,
        )
        new_handler = WebSocketSessionHandler(save_dir=tmp_path)
        new_handler._session_data = new_sd
        # Task E.2 wiring: ``_apply_narration_result_to_snapshot``
        # requires ``sd._room``. Bind a fresh room over the reloaded
        # snapshot — this mirrors what the slug-connect path would do
        # post-reload in production.
        new_sd._room = room_for(reloaded_snap)
        new_sd.orchestrator.run_narration_turn = AsyncMock(
            return_value=NarrationTurnResult(
                narration="The dust settles further.",
                is_degraded=False,
                agent_duration_ms=1,
            )
        )

        otel_capture.clear()
        turn_context_2 = _build_turn_context_for_test(new_sd)
        await new_handler._execute_narration_turn(new_sd, "I survey the room.", turn_context_2)

        # Quest log entry must NOT have been rewritten (baseline ==
        # current == "resolved" → idempotent re-detect).
        assert new_sd.snapshot.quest_log["trope_extraction_panic"] == entry_after_turn_1, (
            "Post-reload first turn rewrote the quest_log entry — the "
            "baseline must equal the live snapshot so the diff sees no "
            "transition. Got "
            f"{new_sd.snapshot.quest_log['trope_extraction_panic']!r}"
        )
        # active_stakes must NOT have grown a second resolution marker.
        assert new_sd.snapshot.active_stakes == active_stakes_after_turn_1, (
            "Post-reload first turn appended a second resolution marker "
            "to active_stakes — false-positive duplicate write. "
            f"Got {new_sd.snapshot.active_stakes!r}"
        )

        # But the idempotent re-detect span DID fire (panel-engaged signal).
        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert len(spans) == 1, (
            "Post-reload first turn must still emit one handshake span "
            "(idempotent re-detect) so the GM panel can distinguish "
            "still-resolved from never-engaged. "
            f"Spans seen: {[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("active_stakes_appended") is False
        assert attrs.get("prior_status") == "resolved"
        assert attrs.get("new_status") == "resolved"


# ---------------------------------------------------------------------------
# Multi-trope concurrent resolution — Orin's exact playtest reproducer.
# ---------------------------------------------------------------------------


class TestOrinPlaytestReproducer:
    """Orin's evropi save had two tropes resolve at the same progress
    threshold (extraction_panic + hireling_mutiny, both 0.255). The
    handshake must observe BOTH on the same turn.
    """

    @pytest.mark.asyncio
    async def test_two_concurrent_resolutions_write_both_entries(
        self, session_fixture, otel_capture
    ) -> None:
        sd, handler = session_fixture
        _seed_active_trope(sd, "extraction_panic", "progressing")
        _seed_active_trope(sd, "hireling_mutiny", "progressing")

        async def _flip_both(*_args, **_kwargs):
            for trope in sd.snapshot.active_tropes:
                if trope.id in {"extraction_panic", "hireling_mutiny"}:
                    trope.status = "resolved"
            return NarrationTurnResult(
                narration="Two threads conclude at once.",
                is_degraded=False,
                agent_duration_ms=1,
            )

        sd.orchestrator.run_narration_turn = AsyncMock(side_effect=_flip_both)

        otel_capture.clear()
        turn_context = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I make the final move.", turn_context)

        assert "trope_extraction_panic" in sd.snapshot.quest_log
        assert "trope_hireling_mutiny" in sd.snapshot.quest_log
        assert "extraction_panic" in sd.snapshot.active_stakes
        assert "hireling_mutiny" in sd.snapshot.active_stakes

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        trope_ids = {dict(s.attributes or {}).get("trope_id") for s in spans}
        assert trope_ids == {"extraction_panic", "hireling_mutiny"}, (
            "Two concurrent resolutions must each fire a handshake span "
            f"from the dispatch seam; got trope_ids={trope_ids}"
        )
