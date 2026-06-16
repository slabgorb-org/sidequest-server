"""Tests for MessageType, NarratorVerbosity, NarratorVocabulary.

Ported from:
- sidequest-protocol/src/tests.rs (message_type_tests wire string assertions)
- sidequest-protocol/src/narrator_verbosity_story_14_3_tests.rs (AC1, AC2, AC3, AC5, AC6)
- sidequest-protocol/src/narrator_vocabulary_story_14_4_tests.rs (AC1, AC2, AC3, AC5, AC6)

AC5 round-trips (SessionEvent with verbosity/vocabulary) are now included
since GameMessage/SessionEventPayload are ported in subagent 2.
"""

from __future__ import annotations

import pytest

from sidequest.protocol.enums import MessageType, NarratorVerbosity, NarratorVocabulary

# ===========================================================================
# MessageType wire strings — from tests.rs message_type_tests
# Wire values must match the serde rename on each GameMessage variant.
# ===========================================================================


def test_message_type_player_action_wire_string() -> None:
    assert MessageType.PLAYER_ACTION == "PLAYER_ACTION"
    assert MessageType.PLAYER_ACTION.value == "PLAYER_ACTION"


def test_message_type_narration_wire_string() -> None:
    assert MessageType.NARRATION == "NARRATION"


def test_message_type_narration_end_wire_string() -> None:
    assert MessageType.NARRATION_END == "NARRATION_END"


def test_message_type_thinking_wire_string() -> None:
    assert MessageType.THINKING == "THINKING"


def test_message_type_session_event_wire_string() -> None:
    assert MessageType.SESSION_EVENT == "SESSION_EVENT"


def test_message_type_character_creation_wire_string() -> None:
    assert MessageType.CHARACTER_CREATION == "CHARACTER_CREATION"


def test_message_type_turn_status_wire_string() -> None:
    assert MessageType.TURN_STATUS == "TURN_STATUS"


def test_message_type_party_status_wire_string() -> None:
    assert MessageType.PARTY_STATUS == "PARTY_STATUS"


def test_message_type_confrontation_wire_string() -> None:
    assert MessageType.CONFRONTATION == "CONFRONTATION"


def test_message_type_render_queued_wire_string() -> None:
    assert MessageType.RENDER_QUEUED == "RENDER_QUEUED"


def test_message_type_image_wire_string() -> None:
    assert MessageType.IMAGE == "IMAGE"


def test_message_type_audio_cue_wire_string() -> None:
    assert MessageType.AUDIO_CUE == "AUDIO_CUE"


def test_message_type_voice_signal_wire_string() -> None:
    assert MessageType.VOICE_SIGNAL == "VOICE_SIGNAL"


def test_message_type_voice_text_wire_string() -> None:
    assert MessageType.VOICE_TEXT == "VOICE_TEXT"


def test_message_type_action_queue_wire_string() -> None:
    assert MessageType.ACTION_QUEUE == "ACTION_QUEUE"


def test_message_type_chapter_marker_wire_string() -> None:
    assert MessageType.CHAPTER_MARKER == "CHAPTER_MARKER"


def test_message_type_error_wire_string() -> None:
    assert MessageType.ERROR == "ERROR"


def test_message_type_action_reveal_wire_string() -> None:
    assert MessageType.ACTION_REVEAL == "ACTION_REVEAL"


def test_message_type_player_speech_wire_string() -> None:
    assert MessageType.PLAYER_SPEECH == "PLAYER_SPEECH"


def test_message_type_scenario_event_wire_string() -> None:
    assert MessageType.SCENARIO_EVENT == "SCENARIO_EVENT"


def test_message_type_achievement_earned_wire_string() -> None:
    assert MessageType.ACHIEVEMENT_EARNED == "ACHIEVEMENT_EARNED"


def test_message_type_journal_request_wire_string() -> None:
    assert MessageType.JOURNAL_REQUEST == "JOURNAL_REQUEST"


def test_message_type_journal_response_wire_string() -> None:
    assert MessageType.JOURNAL_RESPONSE == "JOURNAL_RESPONSE"


def test_message_type_item_depleted_wire_string() -> None:
    assert MessageType.ITEM_DEPLETED == "ITEM_DEPLETED"


def test_message_type_resource_min_reached_wire_string() -> None:
    assert MessageType.RESOURCE_MIN_REACHED == "RESOURCE_MIN_REACHED"


def test_message_type_tactical_state_wire_string() -> None:
    assert MessageType.TACTICAL_STATE == "TACTICAL_STATE"


def test_message_type_tactical_action_wire_string() -> None:
    assert MessageType.TACTICAL_ACTION == "TACTICAL_ACTION"


def test_message_type_dice_request_wire_string() -> None:
    assert MessageType.DICE_REQUEST == "DICE_REQUEST"


def test_message_type_dice_throw_wire_string() -> None:
    assert MessageType.DICE_THROW == "DICE_THROW"


def test_message_type_dice_result_wire_string() -> None:
    assert MessageType.DICE_RESULT == "DICE_RESULT"


def test_message_type_beat_selection_wire_string() -> None:
    assert MessageType.BEAT_SELECTION == "BEAT_SELECTION"


def test_message_type_scrapbook_entry_wire_string() -> None:
    assert MessageType.SCRAPBOOK_ENTRY == "SCRAPBOOK_ENTRY"


def test_message_type_player_seat_wire_string() -> None:
    assert MessageType.PLAYER_SEAT == "PLAYER_SEAT"


def test_message_type_seat_confirmed_wire_string() -> None:
    assert MessageType.SEAT_CONFIRMED == "SEAT_CONFIRMED"


def test_message_type_unknown_string_rejected() -> None:
    """Unknown type string must not be a valid MessageType."""
    with pytest.raises(ValueError):
        MessageType("BOGUS_TYPE")


def test_message_type_game_paused_wire_string() -> None:
    assert MessageType.GAME_PAUSED == "GAME_PAUSED"


def test_message_type_game_resumed_wire_string() -> None:
    assert MessageType.GAME_RESUMED == "GAME_RESUMED"


def test_message_type_dispatch_package_wire_string() -> None:
    assert MessageType.DISPATCH_PACKAGE == "DISPATCH_PACKAGE"


def test_message_type_narrator_directive_used_wire_string() -> None:
    assert MessageType.NARRATOR_DIRECTIVE_USED == "NARRATOR_DIRECTIVE_USED"


def test_message_type_verdict_override_wire_string() -> None:
    assert MessageType.VERDICT_OVERRIDE == "VERDICT_OVERRIDE"


def test_message_type_yield_wire_string() -> None:
    assert MessageType.YIELD == "YIELD"


def test_message_type_tactical_grid_wire_string() -> None:
    """ADR-096 Task 20b — cavern renderer revival."""
    assert MessageType.TACTICAL_GRID == "TACTICAL_GRID"


def test_message_type_narration_segment_wire_string() -> None:
    """ADR-105 B3 — per-PC private-prose channel."""
    assert MessageType.NARRATION_SEGMENT == "NARRATION_SEGMENT"


def test_message_type_dungeon_map_wire_string() -> None:
    """Beneath Sünden BETTER fix (seam 3) — the NEW ADR-055 procedural
    megadungeon map frame. Distinct from the port-deleted ADR-019
    MAP_UPDATE (which is NOT revived)."""
    assert MessageType.DUNGEON_MAP == "DUNGEON_MAP"


def test_message_type_complete_count() -> None:
    """All 46 GameMessage variants must be represented.

    Group G Task 6 added SECRET_NOTE (structural hiding); bumped 37 → 38.
    Group D Task 7 reserved DISPATCH_PACKAGE, NARRATOR_DIRECTIVE_USED,
    VERDICT_OVERRIDE for corpus going-forward capture; bumped 38 → 41.
    Task 23 (dual-track momentum Phase 3) added YIELD; bumped 41 → 42.
    Cartography removal 2026-04-28 dropped MAP_UPDATE; back to 41.
    Voice protocol additions (VOICE_SIGNAL, VOICE_TEXT) bumped 41 → 43.
    Story 47-3 added CONFRONTATION_OUTCOME (Phase 5 reveal dispatch);
    bumped 43 → 44.
    ADR-096 Task 20b added TACTICAL_GRID (cavern renderer revival);
    bumped 44 → 45.
    ADR-105 B3 added NARRATION_SEGMENT (per-PC private-prose channel —
    the broadcast-layer perception firewall); bumped 45 → 46.
    Beneath Sünden BETTER fix (seam 3) added DUNGEON_MAP — the NEW
    ADR-055 procedural-megadungeon map frame (NOT a revival of the
    port-deleted ADR-019 MAP_UPDATE); bumped 46 → 47.
    Playtest 2026-05-17 added PLAYER_SPEECH — verbatim PC dialogue
    surfaced to the MP party (the narrator can't echo player speech per
    SOUL.md Agency, and ACTION_REVEAL is wiped on barrier-fire); bumped
    47 → 48.
    ADR-107 (story 50-25) added ASIDE_ANSWER — the out-of-band OOC GM
    reply to a player aside; never a turn record, broadcast table-wide.
    Intentional addition, NOT a regression; bumped 48 → 49.
    ADR-109 (story 54-2) added LOCATION_DESCRIPTION — the persistent
    location description + typed manifest snapshot. Emitted on
    current_room change and session resume. Intentional addition;
    bumped 49 → 50.
    ADR-109 (story 54-7) added LOCATION_OVERLAY_CHANGED — the delta
    channel for encounter location overlay state. Fires on encounter
    activate/deactivate touching a bound_room_id. Intentional addition;
    bumped 50 → 51.
    Story 67-1 added CLIENT_ERROR — the inbound render-crash signal so a
    GameBoard ErrorBoundary catch can release the crashed player from the MP
    turn barrier instead of orphaning the table's turn. Intentional addition;
    bumped 51 → 52.
    When new variants land, update this count and the individual wire-string
    test above so the contract test keeps catching silent drift.
    """
    assert len(MessageType) == 52


# ===========================================================================
# NarratorVerbosity — AC1, AC2, AC3, AC6 (enum-only subset)
# Ported from narrator_verbosity_story_14_3_tests.rs
# Tests requiring GameMessage/SessionEventPayload are omitted (subagent 2).
# ===========================================================================


# AC1: enum exists with three variants


def test_narrator_verbosity_has_concise_variant() -> None:
    v = NarratorVerbosity.concise
    assert v == NarratorVerbosity.concise


def test_narrator_verbosity_has_standard_variant() -> None:
    v = NarratorVerbosity.standard
    assert v == NarratorVerbosity.standard


def test_narrator_verbosity_has_verbose_variant() -> None:
    v = NarratorVerbosity.verbose
    assert v == NarratorVerbosity.verbose


# AC2: round-trips (str enum — value IS the wire string)


def test_narrator_verbosity_concise_round_trip() -> None:
    v = NarratorVerbosity.concise
    assert NarratorVerbosity(v.value) == v


def test_narrator_verbosity_standard_round_trip() -> None:
    v = NarratorVerbosity.standard
    assert NarratorVerbosity(v.value) == v


def test_narrator_verbosity_verbose_round_trip() -> None:
    v = NarratorVerbosity.verbose
    assert NarratorVerbosity(v.value) == v


def test_narrator_verbosity_serializes_as_lowercase() -> None:
    assert NarratorVerbosity.concise.value == "concise"
    assert NarratorVerbosity.standard.value == "standard"
    assert NarratorVerbosity.verbose.value == "verbose"


# AC3: default is Standard


def test_narrator_verbosity_defaults_to_standard() -> None:
    assert NarratorVerbosity.default() == NarratorVerbosity.standard


# AC6 partial: invalid value rejected


def test_narrator_verbosity_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        NarratorVerbosity("extra_verbose")


# default_for_player_count helper


def test_narrator_verbosity_solo_defaults_to_verbose() -> None:
    assert NarratorVerbosity.default_for_player_count(1) == NarratorVerbosity.verbose


def test_narrator_verbosity_zero_players_defaults_to_verbose() -> None:
    assert NarratorVerbosity.default_for_player_count(0) == NarratorVerbosity.verbose


def test_narrator_verbosity_multiplayer_defaults_to_standard() -> None:
    assert NarratorVerbosity.default_for_player_count(2) == NarratorVerbosity.standard
    assert NarratorVerbosity.default_for_player_count(4) == NarratorVerbosity.standard


# ===========================================================================
# NarratorVocabulary — AC1, AC2, AC3, AC6 (enum-only subset)
# Ported from narrator_vocabulary_story_14_4_tests.rs
# Tests requiring GameMessage/SessionEventPayload are omitted (subagent 2).
# ===========================================================================


# AC1: enum exists with three variants


def test_narrator_vocabulary_has_accessible_variant() -> None:
    v = NarratorVocabulary.accessible
    assert v == NarratorVocabulary.accessible


def test_narrator_vocabulary_has_literary_variant() -> None:
    v = NarratorVocabulary.literary
    assert v == NarratorVocabulary.literary


def test_narrator_vocabulary_has_epic_variant() -> None:
    v = NarratorVocabulary.epic
    assert v == NarratorVocabulary.epic


# AC2: round-trips


def test_narrator_vocabulary_accessible_round_trip() -> None:
    v = NarratorVocabulary.accessible
    assert NarratorVocabulary(v.value) == v


def test_narrator_vocabulary_literary_round_trip() -> None:
    v = NarratorVocabulary.literary
    assert NarratorVocabulary(v.value) == v


def test_narrator_vocabulary_epic_round_trip() -> None:
    v = NarratorVocabulary.epic
    assert NarratorVocabulary(v.value) == v


def test_narrator_vocabulary_serializes_as_lowercase() -> None:
    assert NarratorVocabulary.accessible.value == "accessible"
    assert NarratorVocabulary.literary.value == "literary"
    assert NarratorVocabulary.epic.value == "epic"


# AC3: default is Literary


def test_narrator_vocabulary_defaults_to_literary() -> None:
    assert NarratorVocabulary.default() == NarratorVocabulary.literary


# AC6 partial: invalid value rejected


def test_narrator_vocabulary_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        NarratorVocabulary("flowery")


# ===========================================================================
# AC5: SessionEvent verbosity/vocabulary round-trips (story 14-3 / 14-4)
# Deferred from subagent 1; ported by subagent 2 alongside payload structs.
# Ported from narrator_verbosity_story_14_3_tests.rs and
# narrator_vocabulary_story_14_4_tests.rs (AC5 sections).
# ===========================================================================


def _import_session_types() -> tuple[type, type, type]:
    """Late import to keep test_enums.py decoupled from messages during SA1."""
    from sidequest.protocol.messages import (  # noqa: PLC0415
        GameMessage,
        SessionEventMessage,
        SessionEventPayload,
    )

    return GameMessage, SessionEventMessage, SessionEventPayload


# -- Story 14-3: verbosity on SessionEvent --


def test_session_event_connect_with_verbosity_round_trip() -> None:
    """AC5: SessionEvent connect payload carries narrator_verbosity."""
    GameMessage, SessionEventMessage, SessionEventPayload = _import_session_types()
    msg = GameMessage(
        root=SessionEventMessage(
            payload=SessionEventPayload(
                event="connect",
                player_name="Alice",
                genre="mutant_wasteland",
                world="flickering_reach",
                narrator_verbosity=NarratorVerbosity.verbose,
            ),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = GameMessage.model_validate_json(json_str)
    assert decoded.payload.narrator_verbosity == NarratorVerbosity.verbose  # type: ignore[union-attr]


def test_session_event_without_verbosity_defaults_to_none() -> None:
    """Backward compat: old clients without narrator_verbosity → None."""
    GameMessage, _, _ = _import_session_types()
    import json as _json

    wire = _json.dumps(
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
    msg = GameMessage.model_validate_json(wire)
    assert msg.payload.narrator_verbosity is None  # type: ignore[union-attr]


def test_session_event_verbosity_wire_format() -> None:
    """AC6: wire key 'narrator_verbosity' with lowercase value 'concise'."""
    GameMessage, _, _ = _import_session_types()
    import json as _json

    wire = _json.dumps(
        {
            "type": "SESSION_EVENT",
            "payload": {
                "event": "connect",
                "player_name": "Alice",
                "genre": "mutant_wasteland",
                "world": "flickering_reach",
                "narrator_verbosity": "concise",
            },
            "player_id": "",
        }
    )
    msg = GameMessage.model_validate_json(wire)
    assert msg.payload.narrator_verbosity == NarratorVerbosity.concise  # type: ignore[union-attr]


# -- Story 14-4: vocabulary on SessionEvent --


def test_session_event_connect_with_vocabulary_round_trip() -> None:
    """AC5: SessionEvent connect payload carries narrator_vocabulary."""
    GameMessage, SessionEventMessage, SessionEventPayload = _import_session_types()
    msg = GameMessage(
        root=SessionEventMessage(
            payload=SessionEventPayload(
                event="connect",
                player_name="Alice",
                genre="mutant_wasteland",
                world="flickering_reach",
                narrator_vocabulary=NarratorVocabulary.epic,
            ),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = GameMessage.model_validate_json(json_str)
    assert decoded.payload.narrator_vocabulary == NarratorVocabulary.epic  # type: ignore[union-attr]


def test_session_event_without_vocabulary_defaults_to_none() -> None:
    """Backward compat: old clients without narrator_vocabulary → None."""
    GameMessage, _, _ = _import_session_types()
    import json as _json

    wire = _json.dumps(
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
    msg = GameMessage.model_validate_json(wire)
    assert msg.payload.narrator_vocabulary is None  # type: ignore[union-attr]


def test_session_event_vocabulary_wire_format() -> None:
    """AC6: wire key 'narrator_vocabulary' with lowercase value 'accessible'."""
    GameMessage, _, _ = _import_session_types()
    import json as _json

    wire = _json.dumps(
        {
            "type": "SESSION_EVENT",
            "payload": {
                "event": "connect",
                "player_name": "Alice",
                "genre": "mutant_wasteland",
                "world": "flickering_reach",
                "narrator_vocabulary": "accessible",
            },
            "player_id": "",
        }
    )
    msg = GameMessage.model_validate_json(wire)
    assert msg.payload.narrator_vocabulary == NarratorVocabulary.accessible  # type: ignore[union-attr]


def test_session_event_with_both_verbosity_and_vocabulary() -> None:
    """Both vocabulary and verbosity can coexist on the same payload."""
    GameMessage, SessionEventMessage, SessionEventPayload = _import_session_types()
    msg = GameMessage(
        root=SessionEventMessage(
            payload=SessionEventPayload(
                event="connect",
                player_name="Alice",
                genre="mutant_wasteland",
                world="flickering_reach",
                narrator_verbosity=NarratorVerbosity.concise,
                narrator_vocabulary=NarratorVocabulary.epic,
            ),
            player_id="",
        )
    )
    json_str = msg.model_dump_json()
    decoded = GameMessage.model_validate_json(json_str)
    assert decoded.payload.narrator_verbosity == NarratorVerbosity.concise  # type: ignore[union-attr]
    assert decoded.payload.narrator_vocabulary == NarratorVocabulary.epic  # type: ignore[union-attr]
