"""action_reveal.composing must reach the live GM-panel push but NEVER persist
to turn_telemetry (perseus_cloud solo write-storm, 2026-05-29).

Playtest forensics (session 894): 507 of 1689 turn_telemetry rows (30%) were
``multiplayer|action_reveal.composing`` — one durable Postgres INSERT per
debounced keystroke, with zero audience in solo. Keith's call: composing is
ephemeral keystroke/UI state, not a forensic or mechanical event, and must NOT
be event-sourced in ANY mode (parallels the "ephemeral streaming delta — not
event-sourced" concept). The live GM-panel push is fine; durable persistence
is the defect.

These tests pin the contract at the persistence seam (``publish_event`` →
``_persist_turn_telemetry``), not the handler→publish wiring (covered by
``test_action_reveal_otel.py``):

- composing is pushed live but produces NO ``TelemetrySink.record`` call
- ``action_reveal.submitted`` (a discrete, diagnostic event) STILL persists
- a generic event STILL persists (the skip is not over-broad)
- end-to-end: the real ActionRevealHandler, through the real publish_event,
  with a bound sink, persists nothing for composing (the wiring test)
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from sidequest.telemetry import watcher_hub as wh
from sidequest.telemetry.watcher_hub import bind_event_store, publish_event


class _RecordingSink:
    """Minimal TelemetrySink double — records durable writes only."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.encounter_events: list[dict] = []

    def record(
        self,
        *,
        round: int | None,
        ts: str,
        component: str,
        event_type: str,
        payload_json: str,
    ) -> None:
        self.records.append(
            {
                "round": round,
                "component": component,
                "event_type": event_type,
                "payload_json": payload_json,
            }
        )

    def append_encounter_event(self, *, kind: str, payload_json: str):  # noqa: ANN201
        self.encounter_events.append({"kind": kind, "payload_json": payload_json})
        return MagicMock()


@pytest.fixture
def sink_and_live(monkeypatch) -> Iterator[tuple[_RecordingSink, list[dict]]]:
    """Bind a recording sink and capture live hub pushes without a bound loop.

    Teardown clears the process-global binding so the module's tests stay
    order-independent.
    """
    sink = _RecordingSink()
    bind_event_store(sink)
    live: list[dict] = []
    monkeypatch.setattr(wh.watcher_hub, "publish", lambda event: live.append(event))
    try:
        yield sink, live
    finally:
        bind_event_store(None)


def test_composing_is_pushed_live_but_not_persisted(sink_and_live) -> None:
    sink, live = sink_and_live

    publish_event(
        "action_reveal.composing",
        {"slug": "s", "player_id": "p1", "round": 3, "seq": 5, "text_length": 42},
        component="multiplayer",
    )

    assert sink.records == [], (
        "action_reveal.composing is ephemeral keystroke state — it must NOT be "
        f"written to turn_telemetry in any mode; got {sink.records}"
    )
    assert len(live) == 1, "composing must STILL push live to the GM panel"
    assert live[0]["event_type"] == "action_reveal.composing"


def test_dropped_rate_limit_in_ephemeral_set() -> None:
    """The rate-limit-drop diagnostic is a sibling of composing: it's a noisy
    per-keystroke UI/throttle signal with no forensic or mechanical value, so it
    must be a member of the ephemeral set and never event-source (71-30)."""
    assert "action_reveal.dropped_rate_limit" in wh._EPHEMERAL_EVENT_TYPES


def test_dropped_rate_limit_is_pushed_live_but_not_persisted(sink_and_live) -> None:
    sink, live = sink_and_live

    publish_event(
        "action_reveal.dropped_rate_limit",
        {"slug": "s", "player_id": "p1", "round": 3},
        component="multiplayer",
    )

    assert sink.records == [], (
        "action_reveal.dropped_rate_limit is an ephemeral throttle signal — it "
        f"must NOT be written to turn_telemetry in any mode; got {sink.records}"
    )
    assert len(live) == 1, "dropped_rate_limit must STILL push live to the GM panel"
    assert live[0]["event_type"] == "action_reveal.dropped_rate_limit"


def test_submitted_still_persists(sink_and_live) -> None:
    sink, _live = sink_and_live

    publish_event(
        "action_reveal.submitted",
        {"slug": "s", "player_id": "p1", "round": 3, "text_length": 42, "aside": False},
        component="multiplayer",
    )

    assert len(sink.records) == 1, (
        "action_reveal.submitted is a discrete, diagnostic event and must keep "
        "persisting (Keith: 'may stay')"
    )
    assert sink.records[0]["event_type"] == "action_reveal.submitted"


def test_generic_event_still_persists(sink_and_live) -> None:
    """Guard against an over-broad skip: a normal mechanical event still
    event-sources."""
    sink, _live = sink_and_live

    publish_event(
        "state_transition",
        {"field": "intent", "label": "explore", "round": 1},
        component="intent",
    )

    assert len(sink.records) == 1
    assert sink.records[0]["event_type"] == "state_transition"


@pytest.mark.asyncio
async def test_handler_composing_reaches_live_but_not_persisted(sink_and_live) -> None:
    """Wiring: the real ActionRevealHandler, through the REAL publish_event
    (not patched), with a bound sink, persists nothing for a composing update
    while still pushing it live. This is the end-to-end proof the fix is wired
    on the production path, not just at the seam."""
    from sidequest.handlers.action_reveal import ActionRevealHandler
    from sidequest.protocol.messages import (
        ActionRevealMessage,
        ActionRevealPayload,
        ActionRevealStatus,
    )

    sink, live = sink_and_live

    session = MagicMock()
    session._socket_id = "s1"
    session._room.slug = "test-slug"
    session._session_data.player_id = "p1"
    session._room.snapshot.turn_manager.round = 7
    session._room.broadcast.return_value = []

    msg = ActionRevealMessage(
        payload=ActionRevealPayload(
            player_id="p1",
            character_name="Alex",
            status=ActionRevealStatus.COMPOSING,
            action="hello world",
            aside=False,
            seq=0,
            round=7,
        ),
        player_id="p1",
    )

    await ActionRevealHandler().handle(session, msg)

    assert sink.records == [], (
        "the handler's composing publish must not durably persist; got "
        f"{sink.records}"
    )
    composing_pushes = [e for e in live if e["event_type"] == "action_reveal.composing"]
    assert len(composing_pushes) == 1, "composing must still reach the live hub"
