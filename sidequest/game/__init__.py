"""sidequest.game — Phase 1 minimal slice of the game engine.

Phase 1 exports:
- Character, CreatureCore, HpPool, Inventory
- GameSnapshot, WorldStatePatch, NpcPatch, NpcPoolMember, NarrativeEntry
- StateDelta (game-layer), StateSnapshot, snapshot, compute_delta
- TurnManager, TurnPhase
- CommandHandler, CommandResult, BUILTIN_COMMANDS
- SavedSession, SessionMeta, PersistError
- Resource pools (ADR-033): ResourcePool, ResourceThreshold,
  ResourcePatch, ResourcePatchOp, ResourcePatchResult, ResourcePatchError,
  UnknownResource, NotVoluntary, detect_crossings, mint_threshold_lore
- Encounter: StructuredEncounter,
  EncounterActor, EncounterMetric, EncounterPhase,
  RigType, SecondaryStats, StatValue

Phase 2+ (combat, dice, advancement) are deferred — not exported here.
"""

from sidequest.game.character import (
    AbilityDefinition,
    AffinityState,
    Character,
    KnownFact,
)
from sidequest.game.combatant import Combatant
from sidequest.game.commands import (
    BUILTIN_COMMANDS,
    CommandHandler,
    CommandResult,
    DisplayResult,
    ErrorResult,
    GmCommand,
    InventoryCommand,
    MapCommand,
    QuestsCommand,
    SaveCommand,
    StateMutationResult,
    StatusCommand,
)
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
    hp_pool_from_config,
    hp_pool_from_hp,
)
from sidequest.game.delta import (
    StateDelta,
    StateSnapshot,
    compute_delta,
    snapshot,
)
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    RigType,
    SecondaryStats,
    StatValue,
    StructuredEncounter,
)
from sidequest.game.monster_manual import (
    EntryState,
    ManualEncounter,
    ManualNpc,
    MonsterManual,
)
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.persistence import (
    DatabaseError,
    NotFoundError,
    PersistError,
    SavedSession,
    SerializationError,
    SessionMeta,
)
from sidequest.game.resource_pool import (
    NotVoluntary,
    ResourcePatch,
    ResourcePatchError,
    ResourcePatchOp,
    ResourcePatchResult,
    ResourcePool,
    ResourceThreshold,
    UnknownResource,
    mint_threshold_lore,
)
from sidequest.game.rig_composure_pool import (
    RigComposureDeltaResult,
    RigComposurePool,
)
from sidequest.game.rig_crash import (
    CrashSaveResult,
    RigCrashResult,
    RigDamageResult,
    apply_rig_damage,
    handle_rig_crash,
    resolve_crash_saves,
)
from sidequest.game.session import (
    AchievementTracker,
    AxisValue,
    DiscoveredFact,
    GameSnapshot,
    GenieWish,
    HistoryChapter,
    NarrativeEntry,
    Npc,
    NpcEncounterLogTag,
    NpcPatch,
    TropeState,
    WorldStatePatch,
)
from sidequest.game.thresholds import detect_crossings
from sidequest.game.turn import PreprocessedAction, TurnManager, TurnPhase
from sidequest.game.vehicle_combat import (
    RammingResult,
    resolve_ramming,
    vehicle_ac,
)
from sidequest.game.vessel_tags import (
    InvalidVesselTagsError,
    VesselTags,
    bind_rig_pool_from_inventory,
    bind_rig_pools,
    find_vessel_item,
    parse_vessel_tags,
)

__all__ = [
    # character
    "AbilityDefinition",
    "AffinityState",
    "Character",
    "KnownFact",
    # combatant
    "Combatant",
    # commands
    "BUILTIN_COMMANDS",
    "CommandHandler",
    "CommandResult",
    "DisplayResult",
    "ErrorResult",
    "GmCommand",
    "InventoryCommand",
    "MapCommand",
    "QuestsCommand",
    "SaveCommand",
    "StateMutationResult",
    "StatusCommand",
    # creature_core
    "CreatureCore",
    "HpPool",
    "Inventory",
    "hp_pool_from_hp",
    "hp_pool_from_config",
    # delta
    "StateDelta",
    "StateSnapshot",
    "compute_delta",
    "snapshot",
    # monster_manual (ADR-059)
    "EntryState",
    "ManualEncounter",
    "ManualNpc",
    "MonsterManual",
    # encounter
    "EncounterActor",
    "EncounterMetric",
    "EncounterPhase",
    "RigType",
    "SecondaryStats",
    "StatValue",
    "StructuredEncounter",
    # persistence
    "DatabaseError",
    "NotFoundError",
    "PersistError",
    "SavedSession",
    "SerializationError",
    "SessionMeta",
    # resource_pool (ADR-033)
    "NotVoluntary",
    "ResourcePatch",
    "ResourcePatchError",
    "ResourcePatchOp",
    "ResourcePatchResult",
    "ResourcePool",
    "ResourceThreshold",
    "UnknownResource",
    "detect_crossings",
    "mint_threshold_lore",
    # rig_composure_pool (ADR-078, Epic 53)
    "RigComposureDeltaResult",
    "RigComposurePool",
    # rig_crash (Epic 53, story 53-3 — Composure→0 consequences;
    # Epic 86, story 86-2 — CWN crash saves + two-pool transition)
    "CrashSaveResult",
    "RigCrashResult",
    "RigDamageResult",
    "apply_rig_damage",
    "handle_rig_crash",
    "resolve_crash_saves",
    # vehicle_combat (Epic 86, story 86-2 — CWN vehicle AC + ramming)
    "RammingResult",
    "resolve_ramming",
    "vehicle_ac",
    # session
    "AchievementTracker",
    "AxisValue",
    "DiscoveredFact",
    "GameSnapshot",
    "GenieWish",
    "HistoryChapter",
    "NarrativeEntry",
    "Npc",
    "NpcEncounterLogTag",
    "NpcPatch",
    "NpcPoolMember",
    "TropeState",
    "WorldStatePatch",
    # turn
    "PreprocessedAction",
    "TurnManager",
    "TurnPhase",
    # vessel_tags (Epic 53, story 53-2 — rig materializer binding)
    "InvalidVesselTagsError",
    "VesselTags",
    "bind_rig_pool_from_inventory",
    "bind_rig_pools",
    "find_vessel_item",
    "parse_vessel_tags",
]
