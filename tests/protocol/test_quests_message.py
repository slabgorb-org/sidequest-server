"""Wire shape + parity for the QUESTS projection (Story 77-8, ADR-137).

RED-phase contract (TEA). Mirrors ``tests/protocol/test_relationships_message.py``
and the wire-parity conventions in ``test_wire_parity.py``. The QUESTS projection
is the RELATIONSHIPS-snapshot analog for the quest spine: a dedicated message type
carrying quest_log + quest_anchors + active_stakes together, NOT the thin legacy
``StateDelta.quests: dict[str, str]`` field.

These import symbols that do not exist yet — they FAIL until Dev (77-8 GREEN) adds:
  - ``MessageType.QUESTS``
  - ``QuestLogEntry`` / ``QuestAnchorEntry`` / ``QuestsPayload`` (protocol/models.py)
  - ``QuestsMessage`` (protocol/messages.py)
"""

from __future__ import annotations

import json

from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import QuestsMessage
from sidequest.protocol.models import (
    QuestAnchorEntry,
    QuestLogEntry,
    QuestsPayload,
)


def test_quests_message_type_exists() -> None:
    """A dedicated QUESTS message type exists (not piggybacked on StateDelta)."""
    assert MessageType.QUESTS == "QUESTS"


def test_quests_message_wraps_payload_and_defaults_player_id() -> None:
    """QuestsMessage carries a QuestsPayload and a broadcast (empty) player_id,
    exactly like the RELATIONSHIPS sibling (global payload, all seated PCs)."""
    msg = QuestsMessage(payload=QuestsPayload())
    assert msg.type == MessageType.QUESTS
    assert msg.player_id == ""
    assert isinstance(msg.payload, QuestsPayload)


def test_payload_carries_all_three_spine_fields() -> None:
    """AC1: log + anchors + stakes travel together in one typed payload."""
    payload = QuestsPayload(
        quest_log=[
            QuestLogEntry(
                quest_id="q1",
                title="Go Home",
                objective="Return to Kansas",
                status="active",
                anchor_id="emerald_city",
            )
        ],
        quest_anchors=[QuestAnchorEntry(anchor_id="emerald_city", quest_id="q1")],
        active_stakes="The witch hunts you",
    )
    assert payload.quest_log[0].quest_id == "q1"
    assert payload.quest_log[0].title == "Go Home"
    assert payload.quest_log[0].objective == "Return to Kansas"
    assert payload.quest_log[0].status == "active"
    assert payload.quest_log[0].anchor_id == "emerald_city"
    assert payload.quest_anchors[0].anchor_id == "emerald_city"
    # The anchor knows which quest owns it (matched via QuestEntry.anchor_id).
    assert payload.quest_anchors[0].quest_id == "q1"
    assert payload.active_stakes == "The witch hunts you"


def test_empty_payload_is_valid_not_null_soup() -> None:
    """AC3: an unpopulated spine yields a clean, well-formed empty payload —
    empty lists and an empty string, never None and never a throw."""
    payload = QuestsPayload()
    assert payload.quest_log == []
    assert payload.quest_anchors == []
    assert payload.active_stakes == ""


def test_populated_payload_round_trips_on_the_wire() -> None:
    """AC1: a populated payload serializes with all three fields present."""
    payload = QuestsPayload(
        quest_log=[
            QuestLogEntry(
                quest_id="q1",
                title="Go Home",
                objective="Return to Kansas",
                status="active",
                anchor_id="emerald_city",
            )
        ],
        quest_anchors=[QuestAnchorEntry(anchor_id="emerald_city", quest_id="q1")],
        active_stakes="The witch hunts you",
    )
    data = json.loads(payload.model_dump_json())
    assert data["quest_log"][0]["title"] == "Go Home"
    assert data["quest_log"][0]["quest_id"] == "q1"
    assert data["quest_anchors"][0]["anchor_id"] == "emerald_city"
    assert data["active_stakes"] == "The witch hunts you"


def test_quests_message_nests_through_game_message() -> None:
    """The message serializes through the GameMessage envelope with type=QUESTS
    so the client dispatcher can route it (parity with RelationshipsMessage)."""
    msg = QuestsMessage(payload=QuestsPayload(active_stakes="The witch hunts you"))
    data = json.loads(msg.model_dump_json())
    assert data["type"] == "QUESTS"
    assert data["payload"]["active_stakes"] == "The witch hunts you"
