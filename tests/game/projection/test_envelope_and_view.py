"""Envelope + view types."""

from dataclasses import FrozenInstanceError

import pytest

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import GameStateView


def test_envelope_is_frozen_dataclass() -> None:
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=3)
    assert env.kind == "NARRATION"
    assert env.origin_seq == 3
    with pytest.raises(FrozenInstanceError):
        env.kind = "CONFRONTATION"  # type: ignore[misc]


def test_envelope_allows_none_origin_seq_for_non_event_log_messages() -> None:
    env = MessageEnvelope(kind="PLAYER_PRESENCE", payload_json="{}", origin_seq=None)
    assert env.origin_seq is None


def test_game_state_view_is_protocol() -> None:
    class _Stub:
        def is_gm(self, player_id: str) -> bool:
            return player_id == "gm"

        def seat_of(self, player_id: str) -> str | None:
            return None

        def character_of(self, player_id: str) -> str | None:
            return player_id + "_char"

        def zone_of(self, character_id: str) -> str | None:
            return None

        def visible_to(self, viewer_character_id: str, target_character_id: str) -> bool:
            return True

        def owner_of_item(self, item_id: str) -> str | None:
            return None

        def party_of(self, player_id: str) -> str | None:
            return None

    def _takes_view(v: GameStateView) -> bool:
        return v.is_gm("gm")

    assert _takes_view(_Stub()) is True
