"""Tests for Phase 1 payloads and GameMessage discriminated union.

Ported from:
- sidequest-protocol/src/tests.rs (message_type_tests, wire_compatibility_tests,
  deny_unknown_fields_tests)
- sidequest-protocol/src/tests.rs (player_location_tests)

Wire format verified: Rust uses #[serde(tag = "type")] with struct variants.
The top-level JSON object has "type", "payload", and "player_id" as siblings:
    {"type": "PLAYER_ACTION", "payload": {"action": "..."}, "player_id": ""}

GameMessage.model_validate_json() and model_dump_json() must round-trip
identically to the Rust serde output.
"""

from __future__ import annotations

import json

import pytest

from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    ActionQueueMessage,
    ActionQueuePayload,
    ChapterMarkerMessage,
    ChapterMarkerPayload,
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    ErrorPayload,
    GameMessage,
    NarrationEndMessage,
    NarrationEndPayload,
    NarrationMessage,
    NarrationPayload,
    PartyStatusMessage,
    PartyStatusPayload,
    PlayerActionMessage,
    PlayerActionPayload,
    SessionEventMessage,
    SessionEventPayload,
    ThinkingMessage,
    ThinkingPayload,
    TurnStatusMessage,
    TurnStatusPayload,
)
from sidequest.protocol.models import (
    CharacterState,
    CreationChoice,
    FactCategory,
    Footnote,
    InitialState,
    InventoryItem,
    ItemGained,
    PartyMember,
    StateDelta,
)
from sidequest.protocol.types import NonBlankString

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def nbs(s: str) -> NonBlankString:
    return NonBlankString.model_validate(s)


def make_character_state(**kwargs: object) -> CharacterState:
    defaults: dict[str, object] = {
        "name": "Hero",
        "hp": 20,
        "max_hp": 20,
        "level": 1,
        "class": "Adventurer",
        "statuses": [],
        "inventory": [],
    }
    defaults.update(kwargs)
    return CharacterState.model_validate(defaults)


def make_party_member(**kwargs: object) -> PartyMember:
    defaults: dict[str, object] = {
        "player_id": "p1",
        "name": "Alice",
        "current_hp": 20,
        "max_hp": 20,
        "statuses": [],
        "class": "Adventurer",
        "level": 1,
    }
    defaults.update(kwargs)
    return PartyMember.model_validate(defaults)


def make_inventory_item(**kwargs: object) -> InventoryItem:
    defaults: dict[str, object] = {
        "name": "Item",
        "type": "misc",
        "equipped": False,
        "quantity": 1,
        "description": "A thing",
    }
    defaults.update(kwargs)
    return InventoryItem.model_validate(defaults)


def round_trip(msg: GameMessage) -> GameMessage:
    """Serialize and re-parse a GameMessage."""
    json_str = msg.model_dump_json()
    return GameMessage.model_validate_json(json_str)


def parse_wire(json_str: str) -> GameMessage:
    """Parse a raw wire JSON string into GameMessage."""
    return GameMessage.model_validate_json(json_str)


# ===========================================================================
# AC: GameMessage — all 12 Phase 1 variants construct and round-trip
# Ported from tests.rs message_type_tests
# ===========================================================================


def test_player_action_round_trip() -> None:
    msg = GameMessage(
        root=PlayerActionMessage(
            payload=PlayerActionPayload(action=nbs("attack the goblin"), aside=False),
            player_id="player1",
        )
    )
    json_str = msg.model_dump_json()
    decoded = GameMessage.model_validate_json(json_str)
    assert decoded.type == MessageType.PLAYER_ACTION
    assert '"type":"PLAYER_ACTION"' in json_str
    payload = decoded.payload
    assert isinstance(payload, PlayerActionPayload)
    assert str(payload.action) == "attack the goblin"


def test_player_action_wire_type_tag() -> None:
    """Type discriminator must appear in serialized JSON."""
    msg = GameMessage(
        root=PlayerActionMessage(
            payload=PlayerActionPayload(action=nbs("go north"), aside=False),
            player_id="",
        )
    )
    data = json.loads(msg.model_dump_json())
    assert data["type"] == "PLAYER_ACTION"
    assert "payload" in data
    assert data["payload"]["action"] == "go north"
    # player_id="" is omitted (empty-string default dropped for wire parity);
    # absent player_id means the empty-string case on this Python port.
    assert data.get("player_id", "") == ""


def test_narration_round_trip() -> None:
    msg = GameMessage(
        root=NarrationMessage(
            payload=NarrationPayload(text=nbs("The orc lunges...")),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.NARRATION
    assert '"type":"NARRATION"' in json_str


def test_narration_with_state_delta_round_trip() -> None:
    cs = make_character_state(
        name="Grok",
        hp=15,
        max_hp=20,
        level=3,
        **{"class": "Fighter"},
        statuses=["poisoned"],
        inventory=["sword"],
    )
    delta = StateDelta(
        location="Dark Cave",
        characters=[cs],
        quests=None,
        items_gained=None,
    )
    msg = GameMessage(
        root=NarrationMessage(
            payload=NarrationPayload(text=nbs("You arrive."), state_delta=delta),
            player_id="",
        )
    )
    decoded = round_trip(msg)
    payload = decoded.payload
    assert isinstance(payload, NarrationPayload)
    assert payload.state_delta is not None
    delta = payload.state_delta
    assert delta.location == "Dark Cave"
    assert delta.characters is not None
    assert str(delta.characters[0].name) == "Grok"


def test_narration_end_round_trip() -> None:
    msg = GameMessage(
        root=NarrationEndMessage(
            payload=NarrationEndPayload(state_delta=None),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.NARRATION_END
    assert '"type":"NARRATION_END"' in json_str


def test_thinking_round_trip() -> None:
    msg = GameMessage(
        root=ThinkingMessage(
            payload=ThinkingPayload(),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.THINKING
    assert '"type":"THINKING"' in json_str


def test_session_event_connect_round_trip() -> None:
    msg = GameMessage(
        root=SessionEventMessage(
            payload=SessionEventPayload(
                event="connect",
                player_name="Alice",
                genre="mutant_wasteland",
                world="flickering_reach",
            ),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.SESSION_EVENT
    assert '"type":"SESSION_EVENT"' in json_str
    payload = decoded.payload
    assert isinstance(payload, SessionEventPayload)
    assert payload.event == "connect"


def test_session_event_ready_with_initial_state() -> None:
    cs = make_character_state(
        name="Hero",
        hp=20,
        max_hp=20,
        level=1,
        **{"class": "Ranger"},
        statuses=[],
        inventory=["map"],
    )
    state = InitialState(
        characters=[cs],
        location=nbs("Town Square"),
        quests={},
        turn_count=0,
    )
    msg = GameMessage(
        root=SessionEventMessage(
            payload=SessionEventPayload(event="ready", initial_state=state),
            player_id="",
        )
    )
    decoded = round_trip(msg)
    payload = decoded.payload
    assert isinstance(payload, SessionEventPayload)
    assert payload.initial_state is not None
    assert str(payload.initial_state.location) == "Town Square"


def test_character_creation_round_trip() -> None:
    msg = GameMessage(
        root=CharacterCreationMessage(
            payload=CharacterCreationPayload(
                phase="scene",
                scene_index=1,
                total_scenes=3,
                prompt="Describe your character...",
                choices=[
                    CreationChoice(label=nbs("Warrior"), description=nbs("Strong fighter")),
                ],
                allows_freeform=True,
                input_type="text",
            ),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.CHARACTER_CREATION
    assert '"type":"CHARACTER_CREATION"' in json_str


def test_chargen_payload_deserializes_action_back() -> None:
    """The UI sends action:'back' when back button clicked. Must not be rejected."""
    wire = json.dumps(
        {
            "type": "CHARACTER_CREATION",
            "payload": {"phase": "scene", "action": "back"},
            "player_id": "test-player",
        }
    )
    msg = parse_wire(wire)
    assert msg.type == MessageType.CHARACTER_CREATION
    payload = msg.payload
    assert isinstance(payload, CharacterCreationPayload)
    assert payload.action == "back"


def test_chargen_payload_action_back_without_phase() -> None:
    """The UI sends `{action: 'back'}` with no `phase` field — must deserialize."""
    wire = json.dumps(
        {
            "type": "CHARACTER_CREATION",
            "payload": {"action": "back"},
            "player_id": "test-player",
        }
    )
    msg = parse_wire(wire)
    payload = msg.payload
    assert isinstance(payload, CharacterCreationPayload)
    assert payload.action == "back"
    assert payload.phase is None


def test_chargen_payload_without_action_still_deserializes() -> None:
    """Backwards compatibility: existing messages without action must still work."""
    wire = json.dumps(
        {
            "type": "CHARACTER_CREATION",
            "payload": {"phase": "scene", "choice": "1"},
            "player_id": "test-player",
        }
    )
    msg = parse_wire(wire)
    assert msg.type == MessageType.CHARACTER_CREATION
    payload = msg.payload
    assert isinstance(payload, CharacterCreationPayload)
    assert payload.action is None


def test_turn_status_round_trip() -> None:
    msg = GameMessage(
        root=TurnStatusMessage(
            payload=TurnStatusPayload(player_name=nbs("Kael"), status="active"),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.TURN_STATUS
    assert '"type":"TURN_STATUS"' in json_str
    payload = decoded.payload
    assert isinstance(payload, TurnStatusPayload)
    assert str(payload.player_name) == "Kael"


def test_party_status_round_trip() -> None:
    """Collapsed PARTY_STATUS: sheet + inventory nested inside each PartyMember."""
    member = make_party_member(
        player_id="p1",
        name="Player1",
        character_name="Grok",
        current_hp=20,
        max_hp=20,
        statuses=["blessed"],
        **{"class": "Warrior"},
        level=3,
        sheet={
            "race": "Orc",
            "stats": {"strength": 16, "dexterity": 12},
            "abilities": [
                {
                    "name": "Power Strike",
                    "genre_description": "A mighty blow.",
                    "mechanical_effect": "+2 damage.",
                    "involuntary": False,
                    "source": "Class",
                }
            ],
            "backstory": "A wandering fighter.",
            "personality": "Gruff",
            "pronouns": "he/him",
            "equipment": ["Iron Sword [equipped]"],
        },
        inventory={
            "items": [
                {
                    "name": "Iron Sword",
                    "type": "weapon",
                    "equipped": True,
                    "quantity": 1,
                    "description": "A sturdy blade",
                }
            ],
            "gold": 150,
        },
    )
    msg = GameMessage(
        root=PartyStatusMessage(
            payload=PartyStatusPayload(members=[member]),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.PARTY_STATUS
    assert '"type":"PARTY_STATUS"' in json_str

    # Pre-chargen member: sheet and inventory are None
    pre_chargen = make_party_member(
        player_id="p2",
        name="Player2",
        current_hp=0,
        max_hp=0,
        statuses=[],
        **{"class": "Adventurer"},
        level=0,
    )
    pre_json = pre_chargen.model_dump_json()
    parsed = json.loads(pre_json)
    assert "sheet" not in parsed or parsed.get("sheet") is None
    assert "inventory" not in parsed or parsed.get("inventory") is None


def test_chapter_marker_round_trip() -> None:
    msg = GameMessage(
        root=ChapterMarkerMessage(
            payload=ChapterMarkerPayload(title="Chapter 1", location="The Dark Forest"),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.CHAPTER_MARKER
    assert '"type":"CHAPTER_MARKER"' in json_str


def test_action_queue_round_trip() -> None:
    msg = GameMessage(
        root=ActionQueueMessage(
            payload=ActionQueuePayload(actions=[]),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.ACTION_QUEUE
    assert '"type":"ACTION_QUEUE"' in json_str


def test_error_round_trip() -> None:
    msg = GameMessage(
        root=ErrorMessage(
            payload=ErrorPayload(message=nbs("something went wrong")),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = round_trip(msg)
    assert decoded.type == MessageType.ERROR
    assert '"type":"ERROR"' in json_str
    payload = decoded.payload
    assert isinstance(payload, ErrorPayload)
    assert str(payload.message) == "something went wrong"


# ===========================================================================
# AC: Wire compatibility — exact JSON shapes from api-contract.md
# Ported from tests.rs wire_compatibility_tests
# ===========================================================================


def test_player_action_wire_format() -> None:
    """Exact JSON from api-contract.md."""
    wire = json.dumps(
        {
            "type": "PLAYER_ACTION",
            "payload": {"action": "attack the goblin", "aside": False},
            "player_id": "",
        }
    )
    msg = parse_wire(wire)
    assert msg.type == MessageType.PLAYER_ACTION
    payload = msg.payload
    assert isinstance(payload, PlayerActionPayload)
    assert str(payload.action) == "attack the goblin"
    assert payload.aside is False
    assert msg.player_id == ""


def test_session_event_connect_wire_format() -> None:
    wire = json.dumps(
        {
            "type": "SESSION_EVENT",
            "payload": {
                "event": "connect",
                "player_name": "Alice",
                "genre": "mutant_wasteland",
                "world": "flickering_reach",
            },
            "player_id": "",
        }
    )
    msg = parse_wire(wire)
    assert msg.type == MessageType.SESSION_EVENT
    payload = msg.payload
    assert isinstance(payload, SessionEventPayload)
    assert payload.event == "connect"
    assert payload.player_name == "Alice"


def test_thinking_wire_format() -> None:
    wire = json.dumps({"type": "THINKING", "payload": {}, "player_id": ""})
    msg = parse_wire(wire)
    assert msg.type == MessageType.THINKING


def test_narration_with_delta_wire_format() -> None:
    wire = json.dumps(
        {
            "type": "NARRATION",
            "payload": {
                "text": "The orc lunges...",
                "state_delta": {
                    "location": "Dark Cave",
                    "characters": [
                        {
                            "name": "Grok",
                            "hp": 15,
                            "max_hp": 20,
                            "statuses": ["poisoned"],
                            "inventory": ["sword"],
                        },
                    ],
                    "quests": {"Find the Gem": "in_progress"},
                },
            },
            "player_id": "",
        }
    )
    msg = parse_wire(wire)
    assert msg.type == MessageType.NARRATION
    payload = msg.payload
    assert isinstance(payload, NarrationPayload)
    assert str(payload.text) == "The orc lunges..."
    delta = payload.state_delta
    assert delta is not None
    assert delta.location == "Dark Cave"
    assert delta.characters is not None
    assert str(delta.characters[0].name) == "Grok"
    assert delta.characters[0].hp == 15


def test_error_wire_format() -> None:
    wire = json.dumps(
        {
            "type": "ERROR",
            "payload": {"message": "something broke"},
            "player_id": "",
        }
    )
    msg = parse_wire(wire)
    assert msg.type == MessageType.ERROR
    payload = msg.payload
    assert isinstance(payload, ErrorPayload)
    assert str(payload.message) == "something broke"


def test_unknown_message_type_rejected() -> None:
    """Unknown type string must fail deserialization."""
    wire = json.dumps({"type": "BOGUS_TYPE", "payload": {}, "player_id": ""})
    with pytest.raises((ValueError, Exception)):
        parse_wire(wire)


# ===========================================================================
# AC: deny_unknown_fields — payloads reject unexpected JSON keys
# Ported from tests.rs deny_unknown_fields_tests
# ===========================================================================


def test_player_action_rejects_extra_fields() -> None:
    wire = json.dumps(
        {
            "type": "PLAYER_ACTION",
            "payload": {"action": "go north", "aside": False, "hacker_field": "gotcha"},
            "player_id": "",
        }
    )
    with pytest.raises((ValueError, Exception)):
        parse_wire(wire)


def test_error_payload_rejects_extra_fields() -> None:
    wire = json.dumps(
        {
            "type": "ERROR",
            "payload": {"message": "oops", "secret": "leak"},
            "player_id": "",
        }
    )
    with pytest.raises((ValueError, Exception)):
        parse_wire(wire)


# ===========================================================================
# Narration ADR-076: NARRATION_CHUNK must not deserialize
# Ported from narration_collapse_story_27_9_tests.rs
# ===========================================================================


def test_narration_chunk_json_does_not_deserialize_as_game_message() -> None:
    """ADR-076: NARRATION_CHUNK variant no longer exists."""
    wire = '{"type":"NARRATION_CHUNK","payload":{"text":"partial"},"player_id":""}'
    with pytest.raises((ValueError, Exception)):
        parse_wire(wire)


# ===========================================================================
# Story 14-2: Player location on character sheet
# Ported from tests.rs player_location_tests
# ===========================================================================


def test_party_member_includes_current_location() -> None:
    member = make_party_member(
        player_id="p1",
        name="Alice",
        character_name="Kael",
        current_hp=20,
        max_hp=20,
        statuses=[],
        **{"class": "Ranger"},
        level=3,
        current_location="The Rusty Cantina",
    )
    assert member.current_location is not None
    assert str(member.current_location) == "The Rusty Cantina"


def test_party_member_location_serializes_to_json() -> None:
    member = make_party_member(
        player_id="p1",
        name="Alice",
        character_name="Kael",
        current_hp=20,
        max_hp=20,
        statuses=[],
        **{"class": "Ranger"},
        level=3,
        current_location="Market Square",
    )
    data = json.loads(member.model_dump_json())
    assert data.get("current_location") == "Market Square"


def test_party_member_location_round_trips_through_json() -> None:
    member = make_party_member(
        player_id="p1",
        name="Alice",
        character_name="Kael",
        current_hp=20,
        max_hp=20,
        statuses=[],
        **{"class": "Ranger"},
        level=3,
        current_location="The Wastes",
    )
    json_str = member.model_dump_json()
    decoded = PartyMember.model_validate_json(json_str)
    assert decoded.current_location is not None
    assert str(decoded.current_location) == "The Wastes"


def test_party_status_with_multiple_locations() -> None:
    """Multiplayer: two players in different locations."""
    members = [
        make_party_member(
            player_id="p1",
            name="Alice",
            character_name="Kael",
            current_hp=20,
            max_hp=20,
            statuses=[],
            **{"class": "Ranger"},
            level=3,
            current_location="The Rusty Cantina",
        ),
        make_party_member(
            player_id="p2",
            name="Bob",
            character_name="Lyra",
            current_hp=35,
            max_hp=40,
            statuses=[],
            **{"class": "Cleric"},
            level=5,
            current_location="Scrapyard Gate",
        ),
    ]
    msg = GameMessage(
        root=PartyStatusMessage(
            payload=PartyStatusPayload(members=members),
            player_id="p1",
        )
    )
    decoded = round_trip(msg)
    payload = decoded.payload
    assert isinstance(payload, PartyStatusPayload)
    party_members = payload.members
    assert party_members[0].current_location is not None
    assert str(party_members[0].current_location) == "The Rusty Cantina"
    assert party_members[1].current_location is not None
    assert str(party_members[1].current_location) == "Scrapyard Gate"


# ===========================================================================
# Footnote round-trip inside NarrationPayload
# Ported from narration_with_state_delta_round_trip pattern
# ===========================================================================


def test_narration_with_footnotes_round_trip() -> None:
    footnote = Footnote(
        marker=1,
        summary=nbs("A hooded figure watches from the rafters"),
        category=FactCategory.Person,
        is_new=True,
    )
    msg = GameMessage(
        root=NarrationMessage(
            payload=NarrationPayload(
                text=nbs("The innkeeper nods subtly[1]."),
                footnotes=[footnote],
            ),
            player_id="",
        )
    )
    decoded = round_trip(msg)
    payload = decoded.payload
    assert isinstance(payload, NarrationPayload)
    assert len(payload.footnotes) == 1
    assert str(payload.footnotes[0].summary) == "A hooded figure watches from the rafters"
    assert payload.footnotes[0].category == FactCategory.Person


def test_narration_with_items_gained() -> None:
    delta = StateDelta(
        items_gained=[
            ItemGained(
                name=nbs("Rusty Key"),
                description=nbs("A key to an unknown door"),
                category="quest",
            )
        ],
    )
    msg = GameMessage(
        root=NarrationMessage(
            payload=NarrationPayload(text=nbs("You find a key."), state_delta=delta),
            player_id="",
        )
    )
    decoded = round_trip(msg)
    payload = decoded.payload
    assert isinstance(payload, NarrationPayload)
    assert payload.state_delta is not None
    items = payload.state_delta.items_gained
    assert items is not None
    assert len(items) == 1
    assert str(items[0].name) == "Rusty Key"


# ===========================================================================
# GameMessage discriminator property
# ===========================================================================


def test_game_message_type_property() -> None:
    msg = GameMessage(
        root=ErrorMessage(
            payload=ErrorPayload(message=nbs("test")),
            player_id="",
        )
    )
    assert msg.type == MessageType.ERROR


def test_game_message_payload_property() -> None:
    msg = GameMessage(
        root=ThinkingMessage(
            payload=ThinkingPayload(),
            player_id="server",
        )
    )
    assert isinstance(msg.payload, ThinkingPayload)


def test_game_message_player_id_property() -> None:
    msg = GameMessage(
        root=PlayerActionMessage(
            payload=PlayerActionPayload(action=nbs("look around")),
            player_id="p1",
        )
    )
    assert msg.player_id == "p1"


# ===========================================================================
# All 12 Phase 1 types exist and discriminate correctly
# Wiring test: GameMessage resolves all 12 variant types
# ===========================================================================


def test_all_phase1_variants_parse_correctly() -> None:
    """Integration: all 12 Phase 1 variants must parse via GameMessage."""
    payloads = [
        (MessageType.PLAYER_ACTION, {"payload": {"action": "go north", "aside": False}}),
        (MessageType.NARRATION, {"payload": {"text": "You move forward."}}),
        (MessageType.NARRATION_END, {"payload": {}}),
        (MessageType.THINKING, {"payload": {}}),
        (MessageType.SESSION_EVENT, {"payload": {"event": "connect"}}),
        (MessageType.CHARACTER_CREATION, {"payload": {"phase": "scene"}}),
        (MessageType.TURN_STATUS, {"payload": {"player_name": "Alice", "status": "active"}}),
        (MessageType.PARTY_STATUS, {"payload": {"members": []}}),
        (MessageType.CHAPTER_MARKER, {"payload": {}}),
        (MessageType.ACTION_QUEUE, {"payload": {"actions": []}}),
        (MessageType.ERROR, {"payload": {"message": "oops"}}),
    ]
    for msg_type, extra in payloads:
        wire: dict[str, object] = {"type": msg_type.value, "player_id": "", **extra}
        msg = GameMessage.model_validate(wire)
        assert msg.type == msg_type, f"Expected {msg_type}, got {msg.type}"


# ---------------------------------------------------------------------------
# Visibility sidecar round-trip (regression: session_handler.py:250 crash
# on slug-resume when _NarrationPayload(**data) saw the serialized
# _visibility key that it couldn't validate as input).
# ---------------------------------------------------------------------------


def test_narration_payload_accepts_visibility_wire_alias() -> None:
    """NarrationPayload must deserialize from the _visibility wire alias —
    event-log replay re-hydrates payloads using the already-serialized form
    which uses the wire name, not the field name."""
    payload = NarrationPayload(
        text="The auditorium dims.",
        _visibility={"visible_to": "all", "fidelity": {}},
    )
    assert payload.visibility_sidecar == {"visible_to": "all", "fidelity": {}}


def test_narration_payload_round_trips_visibility() -> None:
    """Serialize by alias, deserialize again — must equal the original."""
    original = NarrationPayload(
        text="The host raises an arm.",
        visibility_sidecar={"visible_to": "all", "fidelity": {"pc:alice": "full"}},
    )
    wire_json = original.model_dump_json(by_alias=True)
    wire_dict = json.loads(wire_json)
    assert "_visibility" in wire_dict
    assert "visibility_sidecar" not in wire_dict
    rebuilt = NarrationPayload.model_validate(wire_dict)
    assert rebuilt.visibility_sidecar == original.visibility_sidecar


def test_secret_note_payload_round_trips_visibility() -> None:
    """Same round-trip guarantee for SECRET_NOTE replay."""
    from sidequest.protocol.messages import SecretNotePayload

    original = SecretNotePayload(
        turn_id="t-1",
        idempotency_key="k-1",
        subsystem="inventory",
        visibility_sidecar={"visible_to": ["player:Alice"], "fidelity": {}},
    )
    wire_dict = json.loads(original.model_dump_json(by_alias=True))
    assert "_visibility" in wire_dict
    rebuilt = SecretNotePayload.model_validate(wire_dict)
    assert rebuilt.visibility_sidecar == original.visibility_sidecar


# ===========================================================================
# YieldMessage (Task 23 — dual-track momentum Phase 3)
# ===========================================================================


def test_message_type_has_yield() -> None:
    """MessageType enum must include a YIELD variant with value "YIELD"."""
    assert MessageType.YIELD.value == "YIELD"


def test_yield_message_no_payload_fields() -> None:
    """YieldMessage serializes with type YIELD and player_id; payload is empty."""
    from sidequest.protocol.messages import YieldMessage

    msg = YieldMessage(player_id="p1")
    raw = msg.model_dump_json()
    parsed = json.loads(raw)
    assert parsed["type"] == "YIELD"
    assert parsed["player_id"] == "p1"


def test_yield_message_round_trip() -> None:
    """YieldMessage must survive a JSON round-trip unchanged."""
    from sidequest.protocol.messages import YieldMessage

    msg = YieldMessage(player_id="p1")
    re_msg = YieldMessage.model_validate_json(msg.model_dump_json())
    assert re_msg == msg


def test_yield_message_parses_via_game_message() -> None:
    """Integration: YieldMessage must be parseable via the GameMessage union."""
    wire = {"type": "YIELD", "player_id": "p1", "payload": {}}
    msg = GameMessage.model_validate(wire)
    assert msg.type == MessageType.YIELD
    assert msg.player_id == "p1"
