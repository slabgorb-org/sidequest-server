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


# VOICE_SIGNAL / VOICE_TEXT wire-string tests removed by Story 101-2 — the
# voice-generation protocol surface is dead and the members are being deleted.
# Absence is now asserted in tests/game/test_101_2_voice_removal.py.


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


def test_message_type_fate_action_wire_string() -> None:
    assert MessageType.FATE_ACTION == "FATE_ACTION"


def test_message_type_fate_throw_wire_string() -> None:
    assert MessageType.FATE_THROW == "FATE_THROW"


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


def test_message_type_site_map_wire_string() -> None:
    """Track B Task 8 (story 164-4) renamed DUNGEON_MAP -> SITE_MAP: the map
    frame is now the generalized per-site projection (the ADR-055 shape,
    kept). One cutover, no alias — the old wire string must be GONE.
    Distinct from the port-deleted ADR-019 MAP_UPDATE (which is NOT
    revived)."""
    assert MessageType.SITE_MAP == "SITE_MAP"
    assert not hasattr(MessageType, "DUNGEON_MAP")


def test_message_type_character_incapacitated_wire_string() -> None:
    """sq-playtest 2026-06-07 (barsoom-3) — the player-facing death surface.
    Emitted when a PC is taken out of play; the UI locks that seat's input."""
    assert MessageType.CHARACTER_INCAPACITATED == "CHARACTER_INCAPACITATED"


def test_message_type_complete_count() -> None:
    """All 56 GameMessage variants must be represented.

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
    port-deleted ADR-019 MAP_UPDATE); bumped 46 → 47. Track B Task 8
    (story 164-4) renamed it DUNGEON_MAP → SITE_MAP — a RENAME, not an
    addition; the count is unchanged.
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
    CHECK_THROW added for non-beat SWN skill checks and saves (2d6 / d20).
    Client rolls in the 3D overlay and submits settled faces; server resolves
    via dispatch_check and broadcasts DiceRequest + DiceResult. Intentional
    addition; bumped 52 → 53.
    ADR-136 added RELATIONSHIPS — the player-facing relationship roster snapshot.
    Emitted reactively when the relationship set changes (disposition shift, NPC
    promoted, claim recorded), not every turn. Intentional addition; bumped
    53 → 54.
    ADR-137 (story 77-8) added QUESTS — the player-facing quest-spine snapshot
    (quest_log + quest_anchors + active_stakes). Emitted reactively on
    seed/record_quest/set_stakes; transient broadcast, never event-sourced.
    Intentional addition; bumped 54 → 55.
    sq-playtest 2026-06-07 (barsoom-3) added CHARACTER_INCAPACITATED — the
    player-facing death surface. Emitted when a PC is taken out of play (LETHAL
    lethality verdict) so the UI locks that seat's input and shows a death
    banner / re-roll CTA; the server-side turn-intake gate is the authority.
    Intentional addition; bumped 55 → 56.
    Story 101-2 removed VOICE_SIGNAL + VOICE_TEXT — the voice-generation
    protocol surface is fully dead (zero emitters/handlers, no UI readers).
    Dropped 56 → 54.
    ADR-144 F1d added FATE_ACTION — the Fate-bound pack's player action (one of
    the three proactive Fate actions or a concession). Routed to
    FateActionHandler → fate_conflict, gated by isinstance(ruleset,
    FateRulesetModule). Intentional addition; bumped 54 → 55.
    ADR-144 F3a (story 118-1) added FATE_STATE — the player-facing Fate-spine
    snapshot (per-PC sheets + scene situation aspects + active conflict). Emitted
    reactively when the Fate state changes, ruleset=='fate'-gated; transient
    broadcast, never event-sourced. Intentional addition; bumped 55 → 56.
    ADR-144 F3c (story 118-7) added FATE_ROLL — the broadcast of a resolved 4dF
    roll (dice + ladder + tier + replay throw_params/seed) so every seat sees the
    soloist's roll. This count test was not updated at the time (pre-existing
    drift, caught here 2026-06-17); bumped 56 → 57.
    ADR-148 (story 126-7) added FATE_THROW — the player's PROACTIVE Fate roll with
    authoritative dF faces (physics-is-the-roll, the Fate analog of DICE_THROW).
    Routed to FateThrowHandler → fate_conflict with thrown_faces; distinct from the
    non-roll FATE_ACTION verbs. Intentional addition; bumped 57 → 58.
    ADR-148/149 (story 126-8) added FATE_DEFEND_REQUEST — the server→client prompt
    at the DEFEND barrier; intentional addition, bumped 58 → 59.
    When new variants land, update this count and the individual wire-string
    test above so the contract test keeps catching silent drift.
    """
    assert len(MessageType) == 59


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


# ===========================================================================
# Story 82-2 / AC3 — NarratorVocabulary.default_for_player_count parity
#
# ADR-049 specifies BOTH enums implement default_for_player_count(n):
#   - Solo (n=1):       Verbose / Literary
#   - Multiplayer (n>1): Standard / Literary
# NarratorVerbosity already has it (covered above). NarratorVocabulary does
# NOT on develop — these tests fail with AttributeError until 82-2 adds it.
# The vocabulary axis is player-count-invariant per the ADR (always Literary);
# the method exists for *interface parity* so the TurnContext fallback can call
# Verbosity.default_for_player_count(n) and Vocabulary.default_for_player_count(n)
# uniformly without special-casing one axis.
# ===========================================================================


def test_narrator_vocabulary_has_default_for_player_count() -> None:
    """AC3: the method exists (parity with NarratorVerbosity). Fails with
    AttributeError on develop — NarratorVocabulary lacks it entirely."""
    assert hasattr(NarratorVocabulary, "default_for_player_count"), (
        "NarratorVocabulary must implement default_for_player_count for "
        "interface parity with NarratorVerbosity (ADR-049, story 82-2)"
    )


def test_narrator_vocabulary_solo_defaults_to_literary() -> None:
    """ADR-049: Solo (n=1) -> Literary."""
    result = NarratorVocabulary.default_for_player_count(1)
    assert result == NarratorVocabulary.literary
    assert isinstance(result, NarratorVocabulary)


def test_narrator_vocabulary_multiplayer_defaults_to_literary() -> None:
    """ADR-049: Multiplayer (n>1) -> Literary. Vocabulary is count-invariant
    (unlike verbosity, which steps solo->verbose); both branches return
    Literary, but the method must still exist and accept the count."""
    assert NarratorVocabulary.default_for_player_count(2) == NarratorVocabulary.literary
    assert NarratorVocabulary.default_for_player_count(4) == NarratorVocabulary.literary


def test_narrator_vocabulary_zero_players_defaults_to_literary() -> None:
    """Edge: the n<=0 'unknown count' path (room=None safe-empty default in
    _build_turn_context) must still resolve to a real member, not raise."""
    assert NarratorVocabulary.default_for_player_count(0) == NarratorVocabulary.literary


def test_narrator_vocabulary_default_for_player_count_matches_plain_default() -> None:
    """Since vocabulary is count-invariant, default_for_player_count(n) must
    agree with default() for every n — guards against a future change that
    silently diverges the two defaulting paths."""
    for n in (0, 1, 2, 5):
        assert NarratorVocabulary.default_for_player_count(n) == NarratorVocabulary.default()
