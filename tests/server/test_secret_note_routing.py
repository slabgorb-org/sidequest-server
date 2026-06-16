"""Group G Task 6 — SECRET_NOTE routing from prompt-redacted dispatch entries.

Task 5 strips ``redact_from_narrator_canonical=True`` dispatches from the
narrator prompt and stashes them on ``NarrationTurnResult.secret_routes``.
This test verifies Task 6: that those stashed entries are reified as
SECRET_NOTE events which flow through the same EventLog / ProjectionFilter
pipeline as NARRATION, so Task 3's ``visibility_tag`` rule can route them
per-recipient.
"""

from __future__ import annotations

import json

from sidequest.protocol.dispatch import (
    NarratorDirective,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.server.session_handler import build_secret_note_events


def _redacted_dispatch(key: str, actor: str, payload: dict) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="lethal_strike",
        params=payload,
        idempotency_key=key,
        visibility=VisibilityTag(
            visible_to=[actor],
            perception_fidelity={},
            secrets_for=[actor],
            redact_from_narrator_canonical=True,
        ),
    )


def test_one_secret_note_per_redacted_dispatch():
    removed = [
        _redacted_dispatch("k1", "player:Alice", {"target": "guard_A"}),
    ]
    events = build_secret_note_events(removed, turn_id="t42")
    assert len(events) == 1
    assert events[0].kind == "SECRET_NOTE"
    p = json.loads(events[0].payload_json)
    assert p["turn_id"] == "t42"
    assert p["idempotency_key"] == "k1"
    assert p["subsystem"] == "lethal_strike"
    assert p["_visibility"]["visible_to"] == ["player:Alice"]


def test_empty_input_produces_no_events():
    assert build_secret_note_events([], turn_id="t0") == []


def test_narrator_directive_is_skipped():
    """Only SubsystemDispatch produces SECRET_NOTE — NarratorDirective doesn't route."""
    directive = NarratorDirective(
        kind="must_not_narrate",
        payload="the payload",
        visibility=VisibilityTag(
            visible_to=["player:Alice"],
            perception_fidelity={},
            secrets_for=["player:Alice"],
            redact_from_narrator_canonical=True,
        ),
    )
    events = build_secret_note_events([directive], turn_id="t1")
    assert events == []


def test_visibility_sidecar_carries_fidelity_map():
    d = SubsystemDispatch(
        subsystem="lethal_strike",
        params={},
        idempotency_key="k1",
        visibility=VisibilityTag(
            visible_to=["player:Alice"],
            perception_fidelity={"player:Alice": "audio_only"},
            secrets_for=["player:Alice"],
            redact_from_narrator_canonical=True,
        ),
    )
    events = build_secret_note_events([d], turn_id="t1")
    p = json.loads(events[0].payload_json)
    assert p["_visibility"]["fidelity"] == {"player:Alice": "audio_only"}


# ---------------------------------------------------------------------------
# Integration test — turn driver wiring
#
# Verifies that when the narrator pipeline produces a NarrationTurnResult
# whose secret_routes contains redacted dispatches, the turn driver's
# log-emission hook appends a SECRET_NOTE envelope to the EventLog
# alongside the canonical NARRATION event.
# ---------------------------------------------------------------------------


class _FakeEventLog:
    """Minimal in-memory stand-in for EventLog — just captures appended rows."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def append(self, kind: str, payload_json: str):  # noqa: D401 — signature matches EventLog.append shape used here
        self.rows.append((kind, payload_json))


def test_turn_driver_emits_secret_notes_from_narration_result():
    """When NarrationTurnResult.secret_routes has entries, emit_secret_notes writes them."""
    from sidequest.server.session_handler import emit_secret_notes

    secret_routes = [
        _redacted_dispatch("k1", "player:Alice", {"target": "guard_A"}),
        _redacted_dispatch("k2", "player:Bob", {"target": "guard_B"}),
    ]

    event_log = _FakeEventLog()
    emit_secret_notes(
        secret_routes=secret_routes,
        turn_id="t99",
        event_log=event_log,
    )

    assert len(event_log.rows) == 2
    kinds = [r[0] for r in event_log.rows]
    assert kinds == ["SECRET_NOTE", "SECRET_NOTE"]

    payloads = [json.loads(r[1]) for r in event_log.rows]
    keys = sorted(p["idempotency_key"] for p in payloads)
    assert keys == ["k1", "k2"]
    alice_payload = next(p for p in payloads if p["idempotency_key"] == "k1")
    assert alice_payload["_visibility"]["visible_to"] == ["player:Alice"]


def test_turn_driver_no_secret_notes_when_secret_routes_empty():
    from sidequest.server.session_handler import emit_secret_notes

    event_log = _FakeEventLog()
    emit_secret_notes(secret_routes=[], turn_id="t99", event_log=event_log)
    assert event_log.rows == []
