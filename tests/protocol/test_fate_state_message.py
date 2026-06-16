"""RED tests for Story 118-1 (F3a) — the FATE_STATE wire contract (protocol layer).

ADR-144 F3a promotes the Fate projection onto the wire as a reactive ``FATE_STATE``
message, mirroring ``RELATIONSHIPS`` (ADR-136) and ``QUESTS`` (ADR-137). This file
pins the *protocol* half of the spine:

  * a ``MessageType.FATE_STATE`` enum value,
  * a ``FateStatePayload`` (+ nested per-PC / aspect / stress / consequence / conflict
    models) with ``extra="forbid"`` boundary validation,
  * a ``FateStateMessage`` carrying that payload and the ``FATE_STATE`` type tag, and
  * membership of ``FateStateMessage`` in the ``GameMessage`` discriminated union so the
    client can actually route it.

All FAIL today: none of these symbols exist yet (RED). New symbols are imported INSIDE
each test so the file collects and every gap reports as its own clean ImportError.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _full_payload():
    """A fully-populated payload exercising every nested model (the F3a shape)."""
    from sidequest.protocol.models import (
        FateAspectEntry,
        FateCharacterEntry,
        FateConflictEntry,
        FateConflictParticipant,
        FateConsequenceEntry,
        FateSkillEntry,
        FateStatePayload,
        FateStressBox,
    )

    return FateStatePayload(
        characters=[
            FateCharacterEntry(
                name="Vance",
                fate_points=3,
                refresh=3,
                skills=[FateSkillEntry(name="Fight", rating=3, ladder="Good")],
                aspects=[
                    FateAspectEntry(text="Last Honest Cop in Vega", kind="high_concept", free_invokes=0)
                ],
                stress={"physical": [FateStressBox(value=1, checked=False)]},
                consequences=[FateConsequenceEntry(level="mild", value=2, filled=False, text="")],
            )
        ],
        scene_aspects=[FateAspectEntry(text="Overturned Table", kind="situation", free_invokes=1)],
        conflict=FateConflictEntry(
            active=True,
            participants=[FateConflictParticipant(name="Vance", side="player")],
        ),
    )


# ---------------------------------------------------------------------------
# AC-1 — the message type tag exists and matches its enum value.
# ---------------------------------------------------------------------------


def test_fate_state_message_type_enum_value():
    from sidequest.protocol.enums import MessageType

    assert MessageType.FATE_STATE == "FATE_STATE"


def test_fate_state_message_carries_type_and_payload():
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import FateStateMessage
    from sidequest.protocol.models import FateStatePayload

    msg = FateStateMessage(payload=FateStatePayload())
    assert msg.type == MessageType.FATE_STATE
    # Sibling messages (Relationships/Quests) carry a player_id defaulting to "".
    assert msg.player_id == ""
    assert isinstance(msg.payload, FateStatePayload)


# ---------------------------------------------------------------------------
# AC-2 — the payload is a strict boundary model (extra="forbid"); empty is valid.
# ---------------------------------------------------------------------------


def test_empty_payload_is_well_formed():
    """An unpopulated payload is a clean empty-but-valid snapshot — never None,
    never a throw (mirrors QuestsPayload)."""
    from sidequest.protocol.models import FateStatePayload

    payload = FateStatePayload()
    assert payload.characters == []
    assert payload.scene_aspects == []
    assert payload.conflict is None


def test_payload_forbids_unknown_fields():
    """Boundary validation (lang-review #11 / No Silent Fallbacks): an unknown field
    on the wire payload must fail loud, not be silently dropped."""
    from sidequest.protocol.models import FateStatePayload

    with pytest.raises(ValidationError):
        FateStatePayload(bogus_field=1)


def test_nested_models_forbid_unknown_fields():
    """Every nested model is also a strict boundary model."""
    from sidequest.protocol.models import FateCharacterEntry, FateSkillEntry

    with pytest.raises(ValidationError):
        FateSkillEntry(name="Fight", rating=3, ladder="Good", junk=True)
    with pytest.raises(ValidationError):
        FateCharacterEntry(name="V", fate_points=3, refresh=3, junk=True)


# ---------------------------------------------------------------------------
# AC-3 — the full nested shape round-trips on the wire (every enumerated field).
# ---------------------------------------------------------------------------


def test_full_payload_round_trips_through_json():
    """Pin the complete F3a shape: a fully-populated payload survives a
    model_dump_json -> model_validate_json round-trip unchanged."""
    from sidequest.protocol.models import FateStatePayload

    payload = _full_payload()
    restored = FateStatePayload.model_validate_json(payload.model_dump_json())
    assert restored == payload

    # Spot-check the load-bearing nested fields the UI consumes (Sebastien/Jade math).
    pc = restored.characters[0]
    assert pc.fate_points == 3 and pc.refresh == 3
    assert pc.skills[0].name == "Fight" and pc.skills[0].rating == 3 and pc.skills[0].ladder == "Good"
    assert pc.aspects[0].kind == "high_concept"
    assert pc.stress["physical"][0].value == 1 and pc.stress["physical"][0].checked is False
    assert pc.consequences[0].level == "mild" and pc.consequences[0].filled is False
    assert restored.scene_aspects[0].kind == "situation" and restored.scene_aspects[0].free_invokes == 1
    assert restored.conflict is not None
    assert restored.conflict.participants[0].side == "player"


# ---------------------------------------------------------------------------
# AC-4 — routability: FateStateMessage is a member of the GameMessage union.
# ---------------------------------------------------------------------------


def test_fate_state_message_in_game_message_union():
    """Without union membership the message is unroutable on the client side
    (mirrors test_quests_message_is_in_game_message_union)."""
    from sidequest.protocol.messages import FateStateMessage, GameMessage

    union = GameMessage.model_fields["root"].annotation
    members = getattr(union, "__args__", ())
    assert FateStateMessage in members, (
        "FateStateMessage is not a member of the GameMessage union — it would be "
        "unroutable on the client side"
    )


def test_game_message_parses_fate_state_wire_form():
    """End-to-end discriminator: a raw {"type": "FATE_STATE", ...} wire dict parses
    into a FateStateMessage via the GameMessage discriminated union."""
    from sidequest.protocol.messages import FateStateMessage, GameMessage

    wire = {"type": "FATE_STATE", "payload": _full_payload().model_dump(), "player_id": ""}
    parsed = GameMessage.model_validate(wire)
    assert isinstance(parsed.root, FateStateMessage)
    assert parsed.root.payload.characters[0].name == "Vance"
