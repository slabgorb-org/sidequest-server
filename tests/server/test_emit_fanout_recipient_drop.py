"""Fan-out delivery must surface a mid-broadcast recipient drop LOUDLY.

Playtest 2026-05-27 (space_opera/coyote_star MP): a GameBoard render crash
tore down a client and the in-flight turn appeared to vanish. Root-cause
investigation showed the turn IS persisted before fan-out (the C2 committed
transaction in ``emit_event``), so the state is durable and replays on
reconnect. The genuine remaining gap was OBSERVABILITY: when a recipient that
was INCLUDED in the fan-out set lost its socket/queue mid-broadcast,
``emit_event`` skipped it with a bare ``continue`` — no log, no watcher event.
The GM panel then saw a clean turn while a player received nothing, which is
exactly the blind spot the OTEL lie-detector exists to prevent (No Silent
Fallbacks + CLAUDE.md OTEL Observability Principle).

These tests drive the extracted ``_deliver_fanout`` helper against a REAL
``SessionRoom`` (real socket/queue transport lookups) with hand-built fan-out
tuples — no genre pack, no projection, no DB. ``emit_event`` is the sole
production caller of the helper. Content correctness is the validators' job;
this is pure engine behavior, so it binds to nothing under
``sidequest-content``.
"""

from __future__ import annotations

import asyncio

import pytest

from sidequest.game.persistence import GameMode
from sidequest.game.projection_filter import FilterDecision
from sidequest.server import emitters
from sidequest.server.session_room import RoomRegistry


class _FakeMsg:
    """Minimal stand-in for a typed GameMessage — captures its payload."""

    def __init__(self, payload):
        self.payload = payload


def _room_three_players() -> RoomRegistry:
    """Three connected players. carl + donut have live outbound queues;
    katia is connected but her queue is detached — the precise state
    ``room.detach_outbound`` leaves behind when a socket drops mid-turn."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug="fanout-drop-test", mode=GameMode.MULTIPLAYER)
    room.connect("p_carl", socket_id="sock-carl")
    room.connect("p_donut", socket_id="sock-donut")
    room.connect("p_katia", socket_id="sock-katia")
    room.attach_outbound("sock-carl", asyncio.Queue())
    room.attach_outbound("sock-donut", asyncio.Queue())
    # NOTE: deliberately NO attach_outbound for sock-katia.
    return room


def _fanout_all_included() -> list[tuple[str, FilterDecision, dict]]:
    return [
        ("p_carl", FilterDecision(include=True, payload_json="{}"), {}),
        ("p_donut", FilterDecision(include=True, payload_json="{}"), {}),
        ("p_katia", FilterDecision(include=True, payload_json="{}"), {}),
    ]


def _spy_watcher(monkeypatch) -> list[tuple[str, dict, dict]]:
    """Spy the watcher publish used by emitters (lazily imported from
    session_handler as ``_watcher_publish``). Returns the recording list."""
    from sidequest.server import session_handler as handler_module

    published: list[tuple[str, dict, dict]] = []

    def _record(event_type, fields, **kwargs):
        published.append((event_type, dict(fields), dict(kwargs)))

    monkeypatch.setattr(handler_module, "_watcher_publish", _record)
    return published


def test_dropped_recipient_emits_loud_watcher_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """katia is connected (in the fan-out set) but her queue is detached →
    a ``broadcast.recipient_dropped`` watcher event at WARNING severity must
    fire instead of a silent skip."""
    room = _room_three_players()
    published = _spy_watcher(monkeypatch)

    emitters._deliver_fanout(
        room,
        _fanout_all_included(),
        message_cls=_FakeMsg,
        payload_cls=None,
        kind="NARRATION",
        seq=1,
    )

    drops = [
        (fields, kwargs)
        for (event_type, fields, kwargs) in published
        if event_type == "state_transition" and fields.get("field") == "broadcast.recipient_dropped"
    ]
    assert drops, (
        "a recipient whose queue was detached mid-broadcast must emit a "
        "broadcast.recipient_dropped watcher event — a silent skip is the "
        "exact blind spot that made the orphaned-turn loop undiagnosable"
    )
    fields, kwargs = drops[0]
    assert fields["recipient_player_id"] == "p_katia"
    assert fields["reason"] == "queue_detached"
    assert fields["kind"] == "NARRATION"
    assert kwargs["component"] == "broadcast"
    assert kwargs["severity"] == "warning"


def test_live_recipients_still_receive_their_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drop of one recipient must not starve the others: carl + donut
    (live queues) still get their message enqueued."""
    room = _room_three_players()
    _spy_watcher(monkeypatch)

    emitters._deliver_fanout(
        room,
        _fanout_all_included(),
        message_cls=_FakeMsg,
        payload_cls=None,
        kind="NARRATION",
        seq=7,
    )

    carl_q = room.queue_for_socket("sock-carl")
    donut_q = room.queue_for_socket("sock-donut")
    assert carl_q is not None and donut_q is not None
    assert carl_q.qsize() == 1, "carl (live queue) must receive the frame"
    assert donut_q.qsize() == 1, "donut (live queue) must receive the frame"
    msg = carl_q.get_nowait()
    assert isinstance(msg, _FakeMsg)
    assert msg.payload["seq"] == 7


def test_excluded_recipient_is_not_a_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recipient the projection deliberately EXCLUDED (decision.include is
    False) is not a delivery failure — it must NOT emit recipient_dropped,
    even if their queue is also absent."""
    room = _room_three_players()
    published = _spy_watcher(monkeypatch)

    fanout = [
        ("p_carl", FilterDecision(include=True, payload_json="{}"), {}),
        # katia excluded by perception filtering — correct, silent, not a drop.
        ("p_katia", FilterDecision(include=False, payload_json="{}"), {}),
    ]
    emitters._deliver_fanout(
        room,
        fanout,
        message_cls=_FakeMsg,
        payload_cls=None,
        kind="NARRATION",
        seq=1,
    )

    assert not [
        f for (_t, f, _k) in published if f.get("field") == "broadcast.recipient_dropped"
    ], "an intentionally excluded recipient is not a mid-broadcast drop"
