from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import RelationshipsMessage
from sidequest.protocol.models import (
    DispositionBeatPayload,
    RelationshipEntry,
    RelationshipsPayload,
)


def test_relationships_message_roundtrip():
    entry = RelationshipEntry(
        name="Tabitha",
        portrait_url=None,
        band="Warm",
        disposition=24,
        trend="up",
        last_seen_turn=6,
        last_seen_location="parlor",
        beats=[DispositionBeatPayload(turn=6, delta=3, reason="candor", location="parlor")],
        personality_read=None,
        ocean=None,
        claims=[],
    )
    msg = RelationshipsMessage(payload=RelationshipsPayload(entries=[entry]))
    assert msg.type == MessageType.RELATIONSHIPS
    dumped = msg.model_dump(mode="json")
    assert dumped["type"] == "RELATIONSHIPS"
    assert dumped["payload"]["entries"][0]["band"] == "Warm"
    again = RelationshipsMessage.model_validate(dumped)
    assert again.payload.entries[0].disposition == 24
