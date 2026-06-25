"""SessionEventPayload carries optional companion-bond metadata (Plan B Task 2).

ProtocolBase is ``extra="forbid"``, so before the fields exist these
constructions raise ValidationError — the correct RED signal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.protocol.messages import SessionEventPayload


def test_connect_payload_defaults_have_no_companion_fields():
    p = SessionEventPayload(event="connect", game_slug="abc", player_name="Alice")
    assert p.companion_of is None
    assert p.relationship is None


def test_connect_payload_accepts_companion_fields():
    p = SessionEventPayload(
        event="connect",
        game_slug="abc",
        player_name="Donut",
        companion_of="alice@home",
        relationship="pet",
    )
    assert p.companion_of == "alice@home"
    assert p.relationship == "pet"


def test_relationship_is_transport_opaque_string():
    # The protocol layer carries the relationship verbatim — interpretation and
    # fail-closed handling live server-side (Task 3), not here. An unknown value
    # must still round-trip as a plain string, never be rejected at the wire.
    p = SessionEventPayload(
        event="connect",
        game_slug="abc",
        player_name="X",
        companion_of="alice@home",
        relationship="overlord",
    )
    assert p.relationship == "overlord"


def test_companion_of_is_length_bounded_at_the_boundary():
    # lang-review #11: a crafted client must not stuff an unbounded string into
    # the room dict / telemetry via companion_of.
    with pytest.raises(ValidationError):
        SessionEventPayload(
            event="connect", game_slug="abc", player_name="X", companion_of="a" * 255
        )


def test_relationship_is_length_bounded_at_the_boundary():
    with pytest.raises(ValidationError):
        SessionEventPayload(
            event="connect", game_slug="abc", player_name="X", relationship="x" * 33
        )
