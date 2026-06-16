"""Story 71-10 (RED) — PlayerActionPayload carries the exact round.

The MP peer-action transcript anchors peer entries positionally today, which
drifts whenever a round is skipped/empty/late (2026-05-27 coyote_star MP
playtest). The structural fix is to carry the **exact round** on the
player-action wire payload so the UI can anchor by matching round instead of
arrival position.

These tests pin the SERVER half of the contract:
  - ``PlayerActionPayload`` gains a required, non-negative ``round: int``.
  - ``round = 0`` (session start) is a valid value — distinct from "missing".
  - A payload missing ``round`` fails LOUD (ValidationError), never silently
    defaulting to 0 — per the No Silent Fallbacks doctrine (CLAUDE.md). A
    legacy/malformed inbound action must surface, not anchor to the wrong turn.
  - Negative rounds are rejected.
  - The field survives the real inbound parse boundary
    (``GameMessage.model_validate_json`` — websocket.py:88).

RED: ``PlayerActionPayload`` has no ``round`` field yet and ProtocolBase sets
``extra="forbid"``, so constructing with ``round=`` currently raises and the
"missing round" case currently succeeds — both opposite to the asserted
contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.messages import GameMessage, PlayerActionPayload


class TestPlayerActionPayloadRound:
    """AC-1 — PlayerActionPayload carries an integer round (server)."""

    def test_round_zero_is_valid(self) -> None:
        """round=0 (the opening round) is a legitimate value, NOT 'missing'."""
        payload = PlayerActionPayload(action="I look around", round=0)
        assert payload.round == 0

    def test_positive_round_is_retained(self) -> None:
        payload = PlayerActionPayload(action="I draw my blade", round=7)
        assert payload.round == 7

    def test_round_round_trips_through_model_dump(self) -> None:
        """The round must serialize (numeric fields are never dropped) and
        re-validate with the value intact — the event-log replay contract."""
        dumped = PlayerActionPayload(action="I hold the line", round=3).model_dump()
        assert dumped["round"] == 3
        restored = PlayerActionPayload.model_validate(dumped)
        assert restored.round == 3

    def test_round_zero_is_serialized_not_dropped(self) -> None:
        """round=0 must appear on the wire — a dropped 0 is indistinguishable
        from 'missing' downstream, which is exactly the drift we are killing."""
        dumped = PlayerActionPayload(action="I wait", round=0).model_dump()
        assert "round" in dumped
        assert dumped["round"] == 0

    def test_missing_round_fails_loud(self) -> None:
        """No Silent Fallbacks: an action without a round is a validation error,
        not a silent anchor-to-round-0."""
        with pytest.raises(ValidationError):
            PlayerActionPayload(action="I sneak past")

    def test_negative_round_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlayerActionPayload(action="I rewind time", round=-1)


class TestPlayerActionRoundWiring:
    """Wiring — the round survives the real inbound parse boundary.

    ``websocket.py`` parses every inbound frame via
    ``GameMessage.model_validate_json``. This proves the new field flows through
    the discriminated-union path the server actually uses, not just the payload
    model in isolation.
    """

    def test_round_survives_gamemessage_json_parse(self) -> None:
        raw = (
            '{"type": "PLAYER_ACTION", '
            '"payload": {"action": "go north", "round": 4}, '
            '"player_id": "p1"}'
        )
        msg = GameMessage.model_validate_json(raw)
        payload = msg.root.payload
        assert isinstance(payload, PlayerActionPayload)
        assert payload.round == 4

    def test_inbound_action_without_round_is_rejected_at_the_boundary(self) -> None:
        """A wire frame missing round must be refused at parse time, so the
        websocket handler raises rather than feeding the engine a roundless
        action that the transcript would mis-anchor."""
        raw = '{"type": "PLAYER_ACTION", "payload": {"action": "go north"}, "player_id": "p1"}'
        with pytest.raises(ValidationError):
            GameMessage.model_validate_json(raw)
