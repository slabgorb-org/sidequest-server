"""sidequest.genre — genre pack loading, resolution, and models.

Public re-exports for the full genre layer (Story 41-2).
"""

from sidequest.genre.archetype.shim import (
    ArchetypeResolution,
    ResolutionSource,
    resolve_archetype,
)
from sidequest.genre.cache import GenreCache
from sidequest.genre.error import (
    GenreCycleError,
    GenreError,
    GenreIoError,
    GenreLoadError,
    GenreMissingParentError,
    GenreNotFoundError,
    GenreValidationError,
    PackError,
    SchemaValidationError,
    ValidationErrors,
)
from sidequest.genre.genre_code import GenreCode
from sidequest.genre.loader import (
    DEFAULT_GENRE_PACK_SEARCH_PATHS,
    GenreLoader,
    find_pack_dir,
    load_genre_pack,
    load_genre_pack_cached,
)
from sidequest.genre.magic_loader import LoaderError, load_world_magic
from sidequest.genre.models import (
    AdvancementEffect,
    AdvancementTier,
    AdvancementTree,
    ArchetypeConstraints,
    ArchetypeFunnels,
    ArchetypeResolved,
    AudioConfig,
    AxesConfig,
    BackstoryTables,
    BaseArchetypes,
    BeatDef,
    BeatVocabulary,
    CartographyConfig,
    CharCreationScene,
    ConfrontationDef,
    Culture,
    DramaThresholds,
    GenrePack,
    GenreTheme,
    InventoryConfig,
    Legend,
    Lore,
    MixerConfig,
    NpcArchetype,
    NpcTraitsDatabase,
    OceanProfile,
    Opening,
    PackMeta,
    ProgressionConfig,
    Prompts,
    RecoveryTrigger,
    RulesConfig,
    ScenarioPack,
    TropeDefinition,
    VisualStyle,
    World,
    WorldConfig,
    WorldLore,
)
from sidequest.genre.resolve import resolve_trope_inheritance
from sidequest.genre.resolver import (
    LayeredMerge,
    MergeStrategy,
    ResolutionContext,
    Resolved,
    Resolver,
)

__all__ = [
    # genre_code
    "GenreCode",
    # loader
    "DEFAULT_GENRE_PACK_SEARCH_PATHS",
    "GenreLoader",
    "find_pack_dir",
    "load_genre_pack",
    "load_genre_pack_cached",
    # magic_loader
    "LoaderError",
    "load_world_magic",
    # resolve (trope inheritance)
    "resolve_trope_inheritance",
    # archetype shim
    "ArchetypeResolution",
    "ResolutionSource",
    "resolve_archetype",
    # resolver
    "LayeredMerge",
    "MergeStrategy",
    "ResolutionContext",
    "Resolved",
    "Resolver",
    # errors
    "GenreError",
    "GenreLoadError",
    "GenreCycleError",
    "GenreMissingParentError",
    "GenreValidationError",
    "GenreIoError",
    "GenreNotFoundError",
    "PackError",
    "SchemaValidationError",
    "ValidationErrors",
    # cache
    "GenreCache",
    # models — key public types
    "AdvancementEffect",
    "AdvancementTier",
    "AdvancementTree",
    "ArchetypeConstraints",
    "ArchetypeFunnels",
    "ArchetypeResolved",
    "AudioConfig",
    "AxesConfig",
    "BackstoryTables",
    "BaseArchetypes",
    "BeatDef",
    "BeatVocabulary",
    "CartographyConfig",
    "CharCreationScene",
    "ConfrontationDef",
    "Culture",
    "DramaThresholds",
    "GenrePack",
    "GenreTheme",
    "InventoryConfig",
    "Legend",
    "Lore",
    "MixerConfig",
    "NpcArchetype",
    "NpcTraitsDatabase",
    "OceanProfile",
    "Opening",
    "PackMeta",
    "Prompts",
    "ProgressionConfig",
    "RecoveryTrigger",
    "RulesConfig",
    "ScenarioPack",
    "TropeDefinition",
    "VisualStyle",
    "World",
    "WorldConfig",
    "WorldLore",
]
