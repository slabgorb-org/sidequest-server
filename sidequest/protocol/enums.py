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
    ACTION_QUEUE = "ACTION_QUEUE"
    CHAPTER_MARKER = "CHAPTER_MARKER"
    ERROR = "ERROR"
    # Story 67-1: inbound crash signal from a client whose render subtree
    # threw (e.g. a GameBoard ErrorBoundary catch). The socket stays open, so
    # the server would otherwise keep awaiting this player at the submit-and-
    # wait barrier forever. The CLIENT_ERROR handler drops the crashed player
    # from the current interaction's awaited count and re-evaluates the barrier
    # — releasing ONLY on an explicit crash signal, never on a slow typist's
    # silence (CLAUDE.md: never rush a slow typist).
    CLIENT_ERROR = "CLIENT_ERROR"
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
    # Non-beat SWN skill check or save (2d6 / d20). Client rolls in the 3D
    # overlay and submits settled faces; the server resolves via dispatch_check
    # and broadcasts DiceRequest + DiceResult to the room.
    CHECK_THROW = "CHECK_THROW"
    # ADR-144: a Fate-bound pack's player action (one of the three proactive Fate
    # actions or a concession). Routed to FateActionHandler → fate_conflict, gated
    # by isinstance(ruleset, FateRulesetModule). Distinct from DICE_THROW (beat+d20).
    FATE_ACTION = "FATE_ACTION"
    # ADR-148 (Story 126-7): a player's PROACTIVE Fate roll is physics-is-the-roll
    # — the four settled dF faces ARE the roll. FATE_THROW carries the action
    # intent + the authoritative face[4] + the throw_params gesture (the Fate analog
    # of DICE_THROW). Routed to FateThrowHandler → fate_conflict with thrown_faces.
    # Distinct from FATE_ACTION (the non-roll verbs: concede / compel_*).
    FATE_THROW = "FATE_THROW"
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
    # Payload schemas land with the emitter (per No-Stubbing — schemas are not
    # pre-reserved as empty shells). These are NOT yet filter-reachable (not in
    # _KIND_TO_MESSAGE_CLS) — emitters land with the group that owns each subsystem.
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
    # ADR-136: player-facing relationship surface (reactive, per-domain).
    RELATIONSHIPS = "RELATIONSHIPS"
    # Story 54-7 / ADR-109: delta channel for encounter location overlay
    # state changes. Fires when an encounter with a non-None
    # location_overlay activates or deactivates touching a bound_room_id.
    LOCATION_OVERLAY_CHANGED = "LOCATION_OVERLAY_CHANGED"
    # Track B site frame (story 164-4; renamed from DUNGEON_MAP, one
    # cutover, no alias). The live site-interior region graph (discovered
    # regions + current region + typed adjacencies + site identity)
    # projected to the UI Map tab whenever the connection's PC is inside a
    # site scene — the Sünden frontier megadungeon, a tavern, a vault.
    # ADR-019 MAP_UPDATE was deleted in the Rust→Python port; ADR-055
    # needs a NEW message — this is it (do NOT revive MAP_UPDATE). The UI
    # MapWidget routes this through its Automapper region-graph path.
    SITE_MAP = "SITE_MAP"
    # ADR-137 / Story 77-8: player-facing quest spine projection. The
    # RELATIONSHIPS-snapshot analog for quests — carries quest_log +
    # quest_anchors + active_stakes together, reactive on seed/record_quest/
    # set_stakes. Transient broadcast (never event-sourced), consumed by the
    # UI quest/objective panel (Story 77-5).
    QUESTS = "QUESTS"
    # ADR-144 F3a / Story 118-1: player-facing Fate Core spine projection. The
    # RELATIONSHIPS/QUESTS-snapshot analog for Fate — per-PC fate points/refresh,
    # skills->ladder, aspects, stress boxes, consequence slots, scene situation
    # aspects+boosts, and the active conflict's participants by side. Reactive
    # (on-change, NOT per-turn), ruleset=='fate'-gated, transient broadcast
    # (never event-sourced), consumed by the UI Fate panel (Story 118-2).
    FATE_STATE = "FATE_STATE"
    # The 4dF roll, surfaced to the player (ADR-144 F3c, Story 118-3). An EVENT
    # (like DICE_RESULT), not change-gated state: the four Fudge faces, the
    # ladder rating, the shift total, the outcome tier, and the succeed-with-
    # style flag. The dice tuple previously lived only on the OTEL span.
    FATE_ROLL = "FATE_ROLL"
    # Server -> client at the DEFEND barrier (ADR-148/151, Story 126-8): "you are
    # attacked by X — defend." One per incoming attack on a seated PC when the
    # round parks; carries the committed attack total so the defender is informed
    # before they throw their own 4dF (physics-is-the-roll for the defense too).
    FATE_DEFEND_REQUEST = "FATE_DEFEND_REQUEST"
    # sq-playtest 2026-06-07 (heavy_metal/barsoom-3, blocking): a PC the genre
    # lethality policy ruled dead kept full agency for four rounds with no
    # death surface. Emitted at the moment a PC is taken OUT of play (LETHAL
    # verdict) and again if a downed seat tries to act. PC-scoped
    # (payload.character_name); the UI locks that seat's input and surfaces a
    # death banner / re-roll CTA. The server-side turn-intake gate is the
    # authority — this message is the player-facing mirror.
    CHARACTER_INCAPACITATED = "CHARACTER_INCAPACITATED"


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

    @classmethod
    def default_for_player_count(cls, player_count: int) -> NarratorVocabulary:
        """Return the default vocabulary for a given player count.

        Per ADR-049 the vocabulary axis is player-count-invariant — both solo
        (n=1) and multiplayer (n>1) default to Literary. This method exists for
        *interface parity* with :meth:`NarratorVerbosity.default_for_player_count`
        so the TurnContext fallback can resolve both axes through the same call
        shape without special-casing one. ``player_count`` is accepted and
        ignored deliberately.
        """
        return cls.literary
