"""Tests for the new NarrationDelta WS message."""

from __future__ import annotations

import json


def test_narration_delta_payload_round_trips():
    from sidequest.protocol.messages import NarrationDelta, NarrationDeltaPayload

    payload = NarrationDeltaPayload(turn_id="t-1", chunk="Hello ", seq=0)
    msg = NarrationDelta(payload=payload)

    dumped = msg.model_dump_json()
    parsed = json.loads(dumped)

    assert parsed["kind"] == "narration.delta"
    assert parsed["payload"]["turn_id"] == "t-1"
    assert parsed["payload"]["chunk"] == "Hello "
    assert parsed["payload"]["seq"] == 0


def test_narration_delta_kind_registered_in_dispatch_table():
    """The message must be registered so emit_event can find it (even though
    deltas don't go through emit_event, the registration is part of the
    canonical protocol surface)."""
    from sidequest.server.session_handler import _KIND_TO_MESSAGE_CLS

    assert "narration.delta" in _KIND_TO_MESSAGE_CLS
