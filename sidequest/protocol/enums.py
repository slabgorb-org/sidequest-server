"""Protocol enums: MessageType, NarratorVerbosity, NarratorVocabulary.

Port of the enum definitions from sidequest-protocol/src/message.rs.

MessageType is a Python str enum of all wire-format type strings that
appear in the serde rename attributes on GameMessage variants. It does
not exist as a standalone Rust enum — the wire strings are encoded as
#[serde(rename = "...")] on each GameMessage variant. The Python
protocol layer makes them explicit here for use in dispatch/routing.

NarratorVerbosity and NarratorVocabulary are direct ports of the Rust
enums from message.rs.
"""

from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    """All WebSocket message type tags.

    Wire values match the serde rename strings on the Rust GameMessage enum.
    Use these constants when constructing or routing protocol messages.
    """

    PLAYER_ACTION = "PLAYER_ACTION"
    NARRATION = "NARRATION"
    # ADR-105 B3: per-PC private-prose channel. The shared NARRATION text
    # is public-safe by contract; PC-private perception travels as its
    # own NARRATION_SEGMENT, routed by _visibility.visible_to and
    # structurally firewalled by the CoreInvariant visibility gate (B1).
    NARRATION_SEGMENT = "NARRATION_SEGMENT"
    NARRATION_END = "NARRATION_END"
    THINKING = "THINKING"
    SESSION_EVENT = "SESSION_EVENT"
    CHARACTER_CREATION = "CHARACTER_CREATION"
    TURN_STATUS = "TURN_STATUS"
    PARTY_STATUS = "PARTY_STATUS"
    CONFRONTATION = "CONFRONTATION"
    # Phase 5 (Story 47-3): magic-confrontation outcome dispatch. Carries
    # the resolved branch + mandatory_outputs so the client overlay
    # surfaces the reveal panel and the LedgerPanel updates.
    CONFRONTATION_OUTCOME = "CONFRONTATION_OUTCOME"
    RENDER_QUEUED = "RENDER_QUEUED"
    IMAGE = "IMAGE"
    AUDIO_CUE = "AUDIO_CUE"
    VOICE_SIGNAL = "VOICE_SIGNAL"
    VOICE_TEXT = "VOICE_TEXT"
    ACTION_QUEUE = "ACTION_QUEUE"
    CHAPTER_MARKER = "CHAPTER_MARKER"
    ERROR = "ERROR"
    ACTION_REVEAL = "ACTION_REVEAL"
    # ADR-107 (story 50-25): out-of-band OOC GM reply to a player aside.
    # NEVER a turn record — does not advance the world, turn/round, the
    # narrative log, or the scrapbook; broadcast table-wide (spec §5).
    ASIDE_ANSWER = "ASIDE_ANSWER"
    # Playtest 2026-05-17: verbatim PC-spoken dialogue, attributed to the
    # speaking PC, surfaced into the shared MP transcript. The narrator
    # cannot echo it (SOUL.md Agency) and ACTION_REVEAL is wiped on
    # barrier-fire, so peers never saw what a PC said aloud. This is
    # public table speech — NOT routed through the perception firewall.
    PLAYER_SPEECH = "PLAYER_SPEECH"
    SCENARIO_EVENT = "SCENARIO_EVENT"
    ACHIEVEMENT_EARNED = "ACHIEVEMENT_EARNED"
    JOURNAL_REQUEST = "JOURNAL_REQUEST"
    JOURNAL_RESPONSE = "JOURNAL_RESPONSE"
    ITEM_DEPLETED = "ITEM_DEPLETED"
    RESOURCE_MIN_REACHED = "RESOURCE_MIN_REACHED"
    TACTICAL_STATE = "TACTICAL_STATE"
    TACTICAL_ACTION = "TACTICAL_ACTION"
    DICE_REQUEST = "DICE_REQUEST"
    DICE_THROW = "DICE_THROW"
    DICE_RESULT = "DICE_RESULT"
    BEAT_SELECTION = "BEAT_SELECTION"
    SCRAPBOOK_ENTRY = "SCRAPBOOK_ENTRY"
    YIELD = "YIELD"
    PLAYER_PRESENCE = "PLAYER_PRESENCE"
    PLAYER_SEAT = "PLAYER_SEAT"
    SEAT_CONFIRMED = "SEAT_CONFIRMED"
    GAME_PAUSED = "GAME_PAUSED"
    GAME_RESUMED = "GAME_RESUMED"
    SECRET_NOTE = "SECRET_NOTE"
    # Reserved event kinds for Group B/C going-forward corpus capture.
    # Payload schemas live in sidequest/corpus/going_forward.py. These are
    # NOT yet filter-reachable (not in _KIND_TO_MESSAGE_CLS) — emitters land
    # with the group that owns each subsystem.
    DISPATCH_PACKAGE = "DISPATCH_PACKAGE"
    NARRATOR_DIRECTIVE_USED = "NARRATOR_DIRECTIVE_USED"
    VERDICT_OVERRIDE = "VERDICT_OVERRIDE"
    # Orbital chart UI (orbital-map plan Task 15). Inbound intent
    # carries a discriminated OrbitalIntent payload; the server
    # responds with an ORBITAL_CHART message carrying a fresh SVG.
    ORBITAL_INTENT = "ORBITAL_INTENT"
    ORBITAL_CHART = "ORBITAL_CHART"
    # Cavern renderer revival (ADR-096 Task 20b). Emitted on room entry
    # when the world uses room_graph navigation and the room has a YAML
    # file in rooms/. Carries TacticalGridPayload; the UI Automapper
    # routes cavern rooms to TacticalGridRenderer and settlement rooms
    # to SettlementRoomView.
    TACTICAL_GRID = "TACTICAL_GRID"
    # Story 54-2 / ADR-109: persistent location description + manifest
    # snapshot. Emitted on current_room change and session resume. The
    # LOCATION_OVERLAY_CHANGED delta variant lands in Story 54-7.
    LOCATION_DESCRIPTION = "LOCATION_DESCRIPTION"
    # Story 54-7 / ADR-109: delta channel for encounter location overlay
    # state changes. Fires when an encounter with a non-None
    # location_overlay activates or deactivates touching a bound_room_id.
    LOCATION_OVERLAY_CHANGED = "LOCATION_OVERLAY_CHANGED"
    # Beneath Sünden BETTER fix (seam 3). Procedural megadungeon map
    # frame: the live region graph (discovered regions + current region +
    # typed adjacencies) projected to the UI Map tab. ADR-019 MAP_UPDATE
    # was deleted in the Rust→Python port; ADR-055 needs a NEW message —
    # this is it (do NOT revive MAP_UPDATE). The UI MapWidget routes this
    # through its Automapper region-graph path.
    DUNGEON_MAP = "DUNGEON_MAP"


class NarratorVerbosity(StrEnum):
    """Controls how verbose the narrator's prose output should be.

    Serializes as lowercase strings for wire compatibility with the React UI.
    Default is Standard. Solo sessions default to Verbose via
    default_for_player_count().
    """

    concise = "concise"
    """Keep descriptions to 1-2 sentences. Prioritize action over atmosphere."""
    standard = "standard"
    """Standard descriptive prose — balanced detail and pacing."""
    verbose = "verbose"
    """Elaborate with sensory details, world-building, and atmospheric prose."""

    @classmethod
    def default(cls) -> NarratorVerbosity:
        """Return the default verbosity (Standard)."""
        return cls.standard

    @classmethod
    def default_for_player_count(cls, player_count: int) -> NarratorVerbosity:
        """Return the default verbosity for a given player count.

        Solo sessions (1 player) default to Verbose for immersive storytelling.
        Multiplayer sessions (2+) default to Standard for pacing.
        """
        if player_count <= 1:
            return cls.verbose
        return cls.standard


class NarratorVocabulary(StrEnum):
    """Controls the prose complexity and diction of narrator output.

    Works alongside NarratorVerbosity (which controls length). Vocabulary
    controls word choice and sentence complexity. Serializes as lowercase
    strings for wire compatibility with the React UI. Default is Literary.
    """

    accessible = "accessible"
    """Simple, direct language. Approximately 8th-grade reading level."""
    literary = "literary"
    """Rich but clear prose. Varied vocabulary without being obscure."""
    epic = "epic"
    """Elevated, archaic, or mythic diction. Unrestricted complexity."""

    @classmethod
    def default(cls) -> NarratorVocabulary:
        """Return the default vocabulary (Literary)."""
        return cls.literary
