"""sidequest.protocol — WebSocket protocol types for the SideQuest game engine.

Re-exports all public symbols from the protocol sub-modules so callers can
import directly from sidequest.protocol without knowing the internal layout.

Port of sidequest-protocol (Rust crate) to Python via pydantic.
"""

from __future__ import annotations

# Dice payloads (story 34 — ported 2026-04-24)
from sidequest.protocol.dice import (
    DiceRequestPayload,
    DiceResultPayload,
    DiceThrowPayload,
    DieGroupResult,
    DieSides,
    DieSpec,
    OpposedRollResult,
    RollOutcome,
    ThrowParams,
)

# Local DM decomposer output contract (Group B)
from sidequest.protocol.dispatch import (
    CrossAction,
    DispatchPackage,
    LethalityVerdict,
    LethalityVerdictKind,
    NarratorDirective,
    NarratorDirectiveKind,
    PerceptionFidelity,
    PlayerDispatch,
    Referent,
    Reversibility,
    SubsystemDispatch,
    VisibilityTag,
)

# Foundation types
from sidequest.protocol.enums import MessageType, NarratorVerbosity, NarratorVocabulary

# Phase 1 payload classes
from sidequest.protocol.messages import (
    ActionQueueMessage,
    ActionQueuePayload,
    ChapterMarkerMessage,
    ChapterMarkerPayload,
    CharacterCreationMessage,
    CharacterCreationPayload,
    DiceRequestMessage,
    DiceResultMessage,
    DiceThrowMessage,
    ErrorMessage,
    ErrorPayload,
    GameMessage,
    LocationDescriptionMessage,
    LocationOverlayChangedMessage,
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
    TacticalGridMessage,
    ThinkingMessage,
    ThinkingPayload,
    TurnStatusMessage,
    TurnStatusPayload,
)

# Nested model types
from sidequest.protocol.models import (
    CellularParams,
    CharacterSheetDetails,
    CharacterState,
    CreationChoice,
    DerivedRoomData,
    EncounterLocationOverlay,
    FactCategory,
    Footnote,
    InitialState,
    InitiativeEntry,
    InventoryItem,
    InventoryPayload,
    ItemGained,
    LocationDescriptionOverlaySummary,
    LocationDescriptionPayload,
    LocationEntity,
    LocationEntityBinding,
    LocationOverlayChangedPayload,
    PartyMember,
    RolledStat,
    StateDelta,
    TacticalGridPayload,
    TokenPayload,
)
from sidequest.protocol.provenance import (
    ContributionKind,
    MergeStep,
    Provenance,
    Span,
    Tier,
)
from sidequest.protocol.sanitize import sanitize_player_text
from sidequest.protocol.types import NonBlankString, Stat

__all__ = [
    # Dice payloads (story 34)
    "DiceRequestPayload",
    "DiceResultPayload",
    "DiceThrowPayload",
    "DieGroupResult",
    "DieSides",
    "DieSpec",
    "OpposedRollResult",
    "RollOutcome",
    "ThrowParams",
    "DiceRequestMessage",
    "DiceResultMessage",
    "DiceThrowMessage",
    # Local DM decomposer (Group B)
    "CrossAction",
    "DispatchPackage",
    "LethalityVerdict",
    "LethalityVerdictKind",
    "NarratorDirective",
    "NarratorDirectiveKind",
    "PerceptionFidelity",
    "PlayerDispatch",
    "Referent",
    "Reversibility",
    "SubsystemDispatch",
    "VisibilityTag",
    # Foundation
    "MessageType",
    "NarratorVerbosity",
    "NarratorVocabulary",
    "ContributionKind",
    "MergeStep",
    "Provenance",
    "Span",
    "Tier",
    "sanitize_player_text",
    "NonBlankString",
    "Stat",
    # Nested models
    "CellularParams",
    "CharacterSheetDetails",
    "CharacterState",
    "CreationChoice",
    "DerivedRoomData",
    "EncounterLocationOverlay",
    "FactCategory",
    "Footnote",
    "InitialState",
    "InitiativeEntry",
    "InventoryItem",
    "InventoryPayload",
    "ItemGained",
    "LocationDescriptionOverlaySummary",
    "LocationDescriptionPayload",
    "LocationEntity",
    "LocationEntityBinding",
    "LocationOverlayChangedPayload",
    "PartyMember",
    "RolledStat",
    "StateDelta",
    "TacticalGridPayload",
    "TokenPayload",
    # Phase 1 payloads + messages
    "ActionQueueMessage",
    "ActionQueuePayload",
    "CharacterCreationMessage",
    "CharacterCreationPayload",
    "ChapterMarkerMessage",
    "ChapterMarkerPayload",
    "ErrorMessage",
    "ErrorPayload",
    "GameMessage",
    "LocationDescriptionMessage",
    "LocationOverlayChangedMessage",
    "NarrationEndMessage",
    "NarrationEndPayload",
    "NarrationMessage",
    "NarrationPayload",
    "PartyStatusMessage",
    "PartyStatusPayload",
    "PlayerActionMessage",
    "PlayerActionPayload",
    "SessionEventMessage",
    "SessionEventPayload",
    "TacticalGridMessage",
    "ThinkingMessage",
    "ThinkingPayload",
    "TurnStatusMessage",
    "TurnStatusPayload",
]
