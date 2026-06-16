"""Story 45-20 — diff predicate unit tests for the trope-resolution handshake.

These are focused unit tests on the ``_handshake_resolved_tropes`` helper
that wires the durable record path. The wire-first boundary
counterpart (``test_45_20_trope_resolution_wire.py``) drives the actual
``_execute_narration_turn`` seam; this file pins the predicate's behavior
on its own so future refactors of the dispatch path do not silently
weaken the diff logic.

The predicate's contract:

- A trope whose baseline status was anything OTHER than ``"resolved"``
  and whose current status is ``"resolved"`` is a "freshly resolved"
  trope. The handshake writes a quest_log entry and an active_stakes
  marker for each.
- A trope whose baseline AND current status are both ``"resolved"`` is
  an idempotent re-detect — the handshake span fires (so the GM panel
  sees the path engaged) but the quest_log/active_stakes are NOT
  rewritten (no double-write, ``active_stakes_appended=False``).
- A trope whose status changed but did NOT land on ``"resolved"`` is a
  no-op: no span, no write.
- The quest_log key is namespaced ``f"trope_{trope_id}"`` so a future
  quest engine cannot collide.
- ``active_stakes`` is appended to (or set when empty) and trimmed at a
  guardrail length so runaway growth does not pollute the prompt.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.session import GameSnapshot, TropeState
from sidequest.telemetry.setup import init_tracer


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


def _snapshot_with_tropes(tropes: list[tuple[str, str]]) -> GameSnapshot:
    """Build a snapshot whose ``active_tropes`` is populated from
    ``[(id, status), ...]``. Other fields default.
    """

    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    for trope_id, status in tropes:
        snap.active_tropes.append(
            TropeState(id=trope_id, status=status, progress=0.0, beats_fired=0)
        )
    snap.turn_manager.interaction = 17
    return snap


def _baseline_from_snapshot(snap: GameSnapshot) -> dict[str, str]:
    """The handshake takes a baseline mapping (trope_id → prior_status).
    Mirrors how the dispatch loop captures the snapshot at the top of
    ``_execute_narration_turn`` before the apply step mutates statuses.
    """

    return {t.id: t.status for t in snap.active_tropes}


def _import_helper() -> Any:
    """Lazy import — RED phase confirms the helper does not yet exist."""

    from sidequest.server.narration_apply import _handshake_resolved_tropes

    return _handshake_resolved_tropes


# ---------------------------------------------------------------------------
# Fresh resolution — the bug Orin's evropi save exposed.
# ---------------------------------------------------------------------------


class TestFreshResolution:
    """Baseline ``progressing|active|dormant`` → current ``resolved`` is a
    freshly resolved trope. Quest_log entry written, active_stakes
    appended, span emitted with ``active_stakes_appended=True``.
    """

    def test_progressing_to_resolved_writes_quest_log_entry(self, otel_capture) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        baseline = _baseline_from_snapshot(snap)
        # Mutate to simulate the apply step (chapter promotion / future
        # engine) flipping the status to "resolved".
        snap.active_tropes[0].status = "resolved"

        helper(
            snap,
            baseline,
            player_name="Rux",
            source="chapter_promotion",
        )

        assert "trope_extraction_panic" in snap.quest_log, (
            "Fresh resolution must write a namespaced quest_log entry. "
            f"Got keys: {list(snap.quest_log)}"
        )
        # The entry text is deterministic — must reference the
        # interaction so the next narrator's state_summary anchors the
        # resolution in time.
        assert "17" in snap.quest_log["trope_extraction_panic"], (
            "quest_log entry text must include the interaction number (turn marker)."
        )

    def test_active_to_resolved_writes_quest_log_entry(self) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("hireling_mutiny", "active")])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "trope_hireling_mutiny" in snap.quest_log

    def test_dormant_to_resolved_writes_quest_log_entry(self) -> None:
        """Edge case — chapter promotion can flip a dormant trope
        directly to resolved (campaign-level setup) without it ever
        being active. The handshake must still observe it.
        """

        helper = _import_helper()
        snap = _snapshot_with_tropes([("ancient_pact", "dormant")])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "trope_ancient_pact" in snap.quest_log

    def test_fresh_resolution_appends_to_empty_active_stakes(self) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"
        assert snap.active_stakes == ""

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        # When active_stakes was empty, the resolution marker is the
        # entire field. The exact format is implementation-defined but
        # the trope id must appear so the panel and the next narrator
        # both see what resolved.
        assert "extraction_panic" in snap.active_stakes, (
            f"Resolution marker must include the trope_id; got active_stakes={snap.active_stakes!r}"
        )
        assert "Resolved" in snap.active_stakes or "resolved" in snap.active_stakes, (
            "Resolution marker must indicate resolution explicitly."
        )

    def test_fresh_resolution_preserves_existing_active_stakes(self) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        snap.active_stakes = "Find the courier."
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "Find the courier." in snap.active_stakes, (
            "Existing active_stakes content must be preserved when a "
            "resolution marker is appended; got "
            f"active_stakes={snap.active_stakes!r}"
        )
        assert "extraction_panic" in snap.active_stakes

    def test_fresh_resolution_emits_handshake_span_with_appended_true(self, otel_capture) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        otel_capture.clear()
        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert len(spans) == 1, (
            "Fresh resolution must emit exactly one handshake span; "
            f"got {[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("trope_id") == "extraction_panic"
        assert attrs.get("prior_status") == "progressing"
        assert attrs.get("new_status") == "resolved"
        assert attrs.get("active_stakes_appended") is True, (
            "Fresh resolution must report active_stakes_appended=True "
            "(this is the lie-detector flag Sebastien needs)."
        )
        assert attrs.get("quest_log_key") == "trope_extraction_panic"
        assert attrs.get("source") == "chapter_promotion"

    def test_quest_log_write_emits_quest_update_span(self, otel_capture) -> None:
        """The story context says the quest_log mutation must wrap in
        the existing ``quest_update_span`` helper — do NOT author a
        parallel quest-write span. The GM panel's existing
        ``SPAN_QUEST_UPDATE`` route surfaces the entry alongside any
        narrator-driven quest updates.
        """

        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        otel_capture.clear()
        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        quest_spans = [s for s in otel_capture.get_finished_spans() if s.name == "quest_update"]
        assert quest_spans, (
            "quest_log mutation must wrap in quest_update_span — the "
            "existing SPAN_QUEST_UPDATE route is what the GM panel reads."
        )


# ---------------------------------------------------------------------------
# Idempotent re-detect — the lie-detector signal.
# ---------------------------------------------------------------------------


class TestIdempotentReDetect:
    """Baseline ``resolved`` AND current ``resolved`` is a re-detect.
    Span fires (panel sees engaged path) but quest_log/active_stakes are
    NOT rewritten and ``active_stakes_appended=False``.
    """

    def test_resolved_to_resolved_does_not_rewrite_quest_log(self) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "resolved")])
        snap.quest_log["trope_extraction_panic"] = "Resolved at turn 12"
        baseline = _baseline_from_snapshot(snap)

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert snap.quest_log["trope_extraction_panic"] == "Resolved at turn 12", (
            "Idempotent re-detect must NOT rewrite the quest_log entry — "
            "the original turn-marker is the canonical record."
        )

    def test_resolved_to_resolved_does_not_append_active_stakes(self) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "resolved")])
        snap.active_stakes = "Find the courier.\n[Resolved: extraction_panic on turn 12]"
        baseline = _baseline_from_snapshot(snap)
        original = snap.active_stakes

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert snap.active_stakes == original, (
            "Idempotent re-detect must NOT append a second resolution "
            f"marker; got {snap.active_stakes!r}"
        )

    def test_idempotent_re_detect_still_emits_handshake_span(self, otel_capture) -> None:
        """The lie-detector requirement: even when the write is a no-op,
        the span fires so the GM panel can distinguish "handshake
        correctly idempotent" from "handshake never engaged after turn N".
        """

        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "resolved")])
        snap.quest_log["trope_extraction_panic"] = "Resolved at turn 12"
        baseline = _baseline_from_snapshot(snap)

        otel_capture.clear()
        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert len(spans) == 1, (
            "Idempotent re-detect must STILL emit exactly one handshake "
            "span — the panel needs the path-engaged signal even when "
            "the write is a no-op."
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("active_stakes_appended") is False, (
            "Idempotent re-detect must report active_stakes_appended=False "
            "so the panel can distinguish first-write from re-detect."
        )
        assert attrs.get("prior_status") == "resolved"
        assert attrs.get("new_status") == "resolved"


# ---------------------------------------------------------------------------
# Negative cases — non-resolution status changes are no-ops.
# ---------------------------------------------------------------------------


class TestNonResolutionTransitionsAreNoops:
    """The diff predicate must be scoped strictly to transitions INTO
    ``"resolved"``. Other status changes (activation, progression,
    downgrade) leave quest_log untouched and emit no handshake span.
    """

    @pytest.mark.parametrize(
        "prior, current",
        [
            ("dormant", "active"),
            ("active", "progressing"),
            ("progressing", "active"),  # downgrade
            ("resolved", "progressing"),  # re-activation (out of scope per ctx)
            ("dormant", "dormant"),  # no change
        ],
    )
    def test_non_resolution_transition_does_not_write_quest_log(
        self, otel_capture, prior, current
    ) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("some_trope", prior)])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = current

        otel_capture.clear()
        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "trope_some_trope" not in snap.quest_log, (
            f"Transition {prior}->{current} must NOT write a quest_log "
            f"entry; got keys: {list(snap.quest_log)}"
        )

    @pytest.mark.parametrize(
        "prior, current",
        [
            ("dormant", "active"),
            ("active", "progressing"),
            ("progressing", "active"),
            ("resolved", "progressing"),
        ],
    )
    def test_non_resolution_transition_does_not_emit_handshake_span(
        self, otel_capture, prior, current
    ) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("some_trope", prior)])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = current

        otel_capture.clear()
        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert spans == [], (
            f"Transition {prior}->{current} must NOT emit a handshake span; "
            f"got {[s.name for s in otel_capture.get_finished_spans()]}"
        )

    def test_non_resolution_transition_does_not_modify_active_stakes(
        self,
    ) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("some_trope", "dormant")])
        snap.active_stakes = "Find the courier."
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "active"

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert snap.active_stakes == "Find the courier."


# ---------------------------------------------------------------------------
# Multi-trope diff — concurrent resolutions and a mix of states.
# ---------------------------------------------------------------------------


class TestMultiTropeDiff:
    def test_two_concurrent_resolutions_both_write_entries(self) -> None:
        """Orin's playtest had two tropes resolve at progress 0.255 in
        the same turn (extraction_panic + hireling_mutiny). The
        handshake must observe both.
        """

        helper = _import_helper()
        snap = _snapshot_with_tropes(
            [
                ("extraction_panic", "progressing"),
                ("hireling_mutiny", "progressing"),
            ]
        )
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"
        snap.active_tropes[1].status = "resolved"

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "trope_extraction_panic" in snap.quest_log
        assert "trope_hireling_mutiny" in snap.quest_log

    def test_two_concurrent_resolutions_emit_two_handshake_spans(self, otel_capture) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes(
            [
                ("extraction_panic", "progressing"),
                ("hireling_mutiny", "progressing"),
            ]
        )
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"
        snap.active_tropes[1].status = "resolved"

        otel_capture.clear()
        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        trope_ids = {dict(s.attributes or {}).get("trope_id") for s in spans}
        assert trope_ids == {"extraction_panic", "hireling_mutiny"}, (
            f"Expected one span per resolved trope; got trope_ids={trope_ids}"
        )

    def test_mixed_one_resolution_one_progression_writes_only_resolution(
        self,
    ) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes(
            [
                ("extraction_panic", "progressing"),
                ("hireling_mutiny", "progressing"),
            ]
        )
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"
        # hireling_mutiny stays at "progressing" (no transition into resolved)

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "trope_extraction_panic" in snap.quest_log
        assert "trope_hireling_mutiny" not in snap.quest_log


# ---------------------------------------------------------------------------
# active_stakes guardrail — runaway growth pollutes the prompt.
# ---------------------------------------------------------------------------


class TestActiveStakesGuardrail:
    """The story context flags the 512-char guardrail: runaway growth in
    active_stakes pollutes the next narrator's state_summary. The exact
    cap is implementation-defined but must exist and the resolution
    marker must always be included in the trimmed result.
    """

    def test_long_active_stakes_is_trimmed_below_guardrail(self) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        # Pad active_stakes well past the guardrail so the trim path fires.
        snap.active_stakes = "x" * 600
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert len(snap.active_stakes) <= 1024, (
            "active_stakes guardrail prevents runaway growth from "
            "polluting the prompt; got length="
            f"{len(snap.active_stakes)}"
        )

    def test_long_active_stakes_still_includes_resolution_marker(self) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        snap.active_stakes = "x" * 600
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "extraction_panic" in snap.active_stakes, (
            "Even with the trim guardrail engaged, the resolution marker "
            "MUST be in the post-trim active_stakes — that is the field's "
            "load-bearing content for the next narrator."
        )


# ---------------------------------------------------------------------------
# Source attribute — the path-of-origin field.
# ---------------------------------------------------------------------------


class TestSourceAttribute:
    """The handshake span carries a ``source`` string identifying which
    upstream wrote the resolved status. ``"chapter_promotion"`` is the
    only live source today (45-19 recompute); ``"narrator_extraction"``
    and ``"engine_tick"`` are reserved for future paths and must be
    accepted without renaming the existing route.
    """

    @pytest.mark.parametrize(
        "source",
        ["chapter_promotion", "narrator_extraction", "engine_tick"],
    )
    def test_helper_accepts_known_sources(self, otel_capture, source) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([("extraction_panic", "progressing")])
        baseline = _baseline_from_snapshot(snap)
        snap.active_tropes[0].status = "resolved"

        otel_capture.clear()
        helper(snap, baseline, player_name="Rux", source=source)

        spans = [
            s for s in otel_capture.get_finished_spans() if s.name == "trope.resolution_handshake"
        ]
        assert spans, "Helper must accept the source string and still emit."
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("source") == source


# ---------------------------------------------------------------------------
# New-trope-resolved-on-creation edge — a trope that didn't exist on the
# baseline but is resolved on the current snapshot. The chapter-promotion
# path can append a new TropeState directly with status="resolved" if the
# chapter declares one (world_materialization.py:511 path).
# ---------------------------------------------------------------------------


class TestNewlyAppearingResolvedTrope:
    def test_brand_new_resolved_trope_writes_quest_log_entry(
        self,
    ) -> None:
        helper = _import_helper()
        snap = _snapshot_with_tropes([])  # baseline empty
        baseline = _baseline_from_snapshot(snap)
        # Apply step appended a new trope already in resolved state
        # (chapter declared status: resolved as the campaign opens).
        snap.active_tropes.append(
            TropeState(
                id="ancient_pact",
                status="resolved",
                progress=1.0,
                beats_fired=0,
            )
        )

        helper(snap, baseline, player_name="Rux", source="chapter_promotion")

        assert "trope_ancient_pact" in snap.quest_log, (
            "A trope that appears already-resolved (no baseline entry) "
            "must still be observed by the handshake — baseline-absent "
            "is treated as 'never resolved before'."
        )
