"""Wiring test for ADR-136 site 1 — the engagement-tick disposition beat.

Per server CLAUDE.md "No Source-Text Wiring Tests", the wiring assertion drives
the real ``_apply_npc_mentions`` handler over a synthetic snapshot and asserts on
the mutated ``Npc.disposition_log`` — it does NOT re-implement the call site by
hand. Removing the ``record_disposition_beat`` call in ``narration_apply.py``
(narration_apply.py ~1738) makes ``test_engagement_records_disposition_beat`` fail.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as _spans_module
from sidequest.game.disposition import Disposition
from sidequest.game.npc_development import ACQUAINTANCE_AT, engagement_beat_reason
from sidequest.game.session import Npc
from sidequest.server.session_handler import (
    _KIND_TO_MESSAGE_CLS,
    _REPLAY_SKIP_KINDS,
    _build_message_for_kind,
)
from sidequest.server.websocket_handlers.relationships_emit import _maybe_emit_relationships
from tests.game.test_disposition_beat import _npc
from tests.server.test_npc_development_pipeline import _core, _engage, _snapshot


def test_engagement_reason_names_the_milestone():
    # Ping-pong 2026-06-07: the beat reason is milestone-derived, naming the
    # tier reached — never the old uniform "warmed by your continued attention".
    reason = engagement_beat_reason("acquaintance")
    assert isinstance(reason, str) and "acquaintance" in reason


def test_engagement_records_disposition_beat():
    # Drive the REAL production handler to the first rapport MILESTONE
    # (ping-pong 2026-06-07: bare cites tick interest but write no beat;
    # the ACQUAINTANCE_AT-th engagement escalates the tier and earns one).
    location = "Parlor"
    snap = _snapshot(Npc(core=_core("Boris"), disposition=Disposition(0)), location=location)
    before = int(snap.npcs[0].disposition)

    for t in range(1, ACQUAINTANCE_AT + 1):
        _engage(snap, "Boris", turn=t)

    npc = snap.npcs[0]
    # The disposition actually moved (a beat means the standing moved).
    assert int(npc.disposition) > before
    # The beat was persisted via the seam at the real call site.
    assert npc.disposition_log, "milestone tick recorded no disposition beat"
    beat = npc.disposition_log[-1]
    assert beat.reason == engagement_beat_reason("acquaintance")
    assert beat.turn == ACQUAINTANCE_AT
    assert beat.location == location
    assert beat.delta == int(npc.disposition) - before


# ---------------------------------------------------------------------------
# Task 11 (ADR-136): the load-bearing OTEL wiring test for the RELATIONSHIPS
# emitter.
#
# Two spans must fire end to end: ``relationship.beat_recorded`` when a real
# disposition beat is appended (the engine wrote history, not the narrator),
# and ``relationships.emitted`` when the change-gated emitter pushes a roster
# carrying that beat. The captured message must carry the beat reason through
# to the client payload — proving the beat reaches the wire, not just the log.
#
# Span capture mirrors the canonical fixture in
# tests/agents/subsystems/test_movement_dispatch.py: an InMemorySpanExporter
# monkeypatched onto ``spans.tracer`` (which ``Span.open`` resolves lazily).
# ---------------------------------------------------------------------------


class _SpanCapture:
    """Adapter exposing ``.spans`` (a flushed list) over an exporter."""

    def __init__(self, exporter: InMemorySpanExporter):
        self._exporter = exporter

    @property
    def spans(self):
        return list(self._exporter.get_finished_spans())


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-relationships")
    monkeypatch.setattr(_spans_module, "tracer", lambda: local)
    return _SpanCapture(exporter)


class _Snap:
    def __init__(self, npcs):
        self.npcs = npcs


def test_real_shift_then_emit_carries_beat(capture_spans):
    """A recorded beat fires relationship.beat_recorded AND reaches the message."""
    npc = _npc("Teague")
    npc.disposition = Disposition(8)
    npc.last_seen_turn = 4
    npc.record_disposition_beat(turn=4, delta=2, reason="shared a drink", location="bar")

    assert any(s.name == "relationship.beat_recorded" for s in capture_spans.spans)

    sent = []
    handler = type("H", (), {})()
    _maybe_emit_relationships(handler, snapshot=_Snap([npc]), emit_fn=lambda m, k: sent.append(m))
    assert sent and sent[0].payload.entries[0].beats[0].reason == "shared a drink"
    assert any(s.name == "relationships.emitted" for s in capture_spans.spans)


# ---------------------------------------------------------------------------
# Task 11 (ADR-136) — reconnect/replay coherence (CRITICAL #1).
#
# RELATIONSHIPS is a TRANSIENT broadcast, emitted via the non-durable
# _emit_shared_world_frame path (NOT _emit_event), exactly like its sibling
# LOCATION_DESCRIPTION. It is therefore never written to the events table and
# must never be reconstructed by the reconnect replay walker. These tests drive
# the REAL replay reconstructor (_build_message_for_kind) and the registry
# invariants to prove the prior crash gap is closed:
#
#   - Before: RELATIONSHIPS was in _KIND_TO_MESSAGE_CLS but had NO
#     _build_message_for_kind branch, so a stray persisted RELATIONSHIPS row
#     would fall through every per-kind branch to the terminal ValueError
#     ("no payload constructor") — an opaque crash that aborts the whole
#     reconnect.
#   - After: RELATIONSHIPS is absent from both _KIND_TO_MESSAGE_CLS and
#     _REPLAY_SKIP_KINDS, identical to LOCATION_DESCRIPTION. A stray row now
#     fails loud with the standard "unknown event kind" schema-drift error
#     (No Silent Fallbacks), and on a normal reconnect it simply never appears.
# ---------------------------------------------------------------------------


def test_relationships_is_transient_like_location_description():
    """RELATIONSHIPS matches its transient sibling: neither replayed nor skipped."""
    # LOCATION_DESCRIPTION is the canonical transient sibling — it is emitted via
    # _emit_shared_world_frame and is absent from both replay structures.
    assert "LOCATION_DESCRIPTION" not in _KIND_TO_MESSAGE_CLS
    assert "LOCATION_DESCRIPTION" not in _REPLAY_SKIP_KINDS
    # RELATIONSHIPS must be handled identically.
    assert "RELATIONSHIPS" not in _KIND_TO_MESSAGE_CLS, (
        "RELATIONSHIPS is transient (broadcast via _emit_shared_world_frame); "
        "registering it in _KIND_TO_MESSAGE_CLS without a _build_message_for_kind "
        "branch reopens the reconnect-crash gap."
    )
    assert "RELATIONSHIPS" not in _REPLAY_SKIP_KINDS


def test_replay_relationships_row_fails_loud_not_opaque_crash():
    """A stray persisted RELATIONSHIPS kind fails loud as schema drift.

    Drives the REAL reconnect reconstructor. The transient roster should never
    be persisted; if one ever is, replay surfaces the standard fail-loud
    "unknown event kind" error rather than the opaque "no payload constructor"
    fall-through that the prior (mapped-but-branchless) state produced.
    """
    with pytest.raises(ValueError, match="unknown event kind"):
        _build_message_for_kind(
            kind="RELATIONSHIPS",
            payload_json='{"entries": []}',
            seq=1,
        )
