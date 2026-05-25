"""GenrePack aggregate root and PackMeta.

Port of sidequest-genre/src/models/pack.rs.

Note: In Rust, GenrePack is assembled by the loader from multiple YAML files.
In Python, we represent it as a structured aggregate that can be built
by the loader (Story 41-3). The individual components are validated models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sidequest.game.projection.rules import ProjectionRules
from sidequest.genre.models.archetype_axes import BaseArchetypes
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.archetype_funnels import ArchetypeFunnels
from sidequest.genre.models.audio import AudioConfig, VoicePresets
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.genre.models.axes import AxesConfig
from sidequest.genre.models.character import (
    BackstoryTables,
    CharCreationScene,
    ClassDef,
    EquipmentTables,
    NpcArchetype,
    VisualStyle,
)
from sidequest.genre.models.chassis import ChassisClassesConfig
from sidequest.genre.models.culture import Culture
from sidequest.genre.models.inventory import InventoryConfig
from sidequest.genre.models.items import WorldItemsCatalog
from sidequest.genre.models.legends import Legend
from sidequest.genre.models.lethality import LethalityPolicy
from sidequest.genre.models.lore import Lore, WorldLore
from sidequest.genre.models.narrative import (
    Achievement,
    BeatVocabulary,
    Opening,
    PowerTier,
    Prompts,
)
from sidequest.genre.models.npc_traits import NpcTraitsDatabase
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.genre.models.progression import ProgressionConfig
from sidequest.genre.models.rigs_world import ChassisInstanceConfig
from sidequest.genre.models.rules import RulesConfig, SavingThrowsTable
from sidequest.genre.models.scenario import ScenarioPack
from sidequest.genre.models.theme import GenreTheme
from sidequest.genre.models.tropes import SeedTrope, TropeDefinition
from sidequest.genre.models.visibility import VisibilityBaseline
from sidequest.genre.models.world import CartographyConfig, WorldConfig


class RecommendedPlayers(BaseModel):
    """Recommended player count for a genre pack."""

    model_config = {"extra": "forbid"}

    min: int
    max: int
    sweet_spot: int | None = None


class Inspiration(BaseModel):
    """A creative inspiration reference."""

    model_config = {"extra": "forbid"}

    name: str
    element: str


class PackMeta(BaseModel):
    """Genre pack metadata from pack.yaml."""

    model_config = {"extra": "forbid"}

    name: str
    version: str
    description: str
    min_sidequest_version: str
    refine_hooks: bool | None = None
    inspirations: list[Inspiration] = Field(default_factory=list)
    era_range: str | None = None
    core_vibe: str | None = None
    emotional_tone: list[str] = Field(default_factory=list)
    differentiation: str | None = None
    lobby_blurb: str | None = None
    recommended_players: RecommendedPlayers | None = None
    extensions: list[str] = Field(default_factory=list)


class PortraitManifestEntry(BaseModel):
    """A character entry in a portrait manifest.

    ``extra="ignore"`` matches Rust parity: the Rust struct doesn't use
    ``#[serde(deny_unknown_fields)]``, so packs can author flavor fields
    the engine doesn't consume (dress_1878, register, flux_prompt,
    negative_additions, references — all present on ``the_real_mccoy``).
    The Rust loader drops them silently; we match that rather than failing
    the whole pack load.
    """

    model_config = {"extra": "ignore", "populate_by_name": True}

    name: str
    role: str = ""
    character_type: str = Field(default="", alias="type", serialization_alias="type")
    appearance: str = ""
    culture_aesthetic: str = ""
    element_visual: str = ""


class World(BaseModel):
    """A world within a genre pack, assembled from worlds/{slug}/.

    Fields are populated by the loader (Story 41-3).
    """

    # No extra="forbid" at aggregate level — loader populates this
    model_config = {"extra": "allow"}

    config: WorldConfig
    lore: WorldLore
    legends: list[Legend] = Field(default_factory=list)
    cartography: CartographyConfig
    cultures: list[Culture] = Field(default_factory=list)
    tropes: list[TropeDefinition] = Field(default_factory=list)
    archetypes: list[NpcArchetype] = Field(default_factory=list)
    visual_style: Any = None  # can be VisualStyle or richer world-level JSON
    history: Any = None
    legends_raw: Any = None
    portrait_manifest: list[PortraitManifestEntry] = Field(default_factory=list)
    archetype_funnels: ArchetypeFunnels | None = None
    openings: list[Opening] = Field(default_factory=list)
    authored_npcs: list[AuthoredNpc] = Field(default_factory=list)
    char_creation: list[CharCreationScene] = Field(default_factory=list)
    chassis_instances: list[ChassisInstanceConfig] = Field(default_factory=list)
    magic_register: str = ""
    items: WorldItemsCatalog | None = None
    client_theme_css: str | None = None
    """Raw contents of ``worlds/<slug>/client_theme.css`` if present.

    World-level override of the genre's client theme. When set, the server
    sends this to the client on SESSION_EVENT{connect} via SESSION_EVENT
    {theme_css} (ADR-079). When absent, the genre-level
    ``GenrePack.client_theme_css`` is used. World wins on resolution.
    """


class GenrePack(BaseModel):
    """A fully-loaded genre pack with all YAML files assembled.

    This is an aggregate root built by the loader (Story 41-3).
    Each field corresponds to one (or more) YAML files in the pack directory.
    """

    model_config = {"extra": "allow"}

    meta: PackMeta
    rules: RulesConfig
    lore: Lore
    theme: GenreTheme
    archetypes: list[NpcArchetype] = Field(default_factory=list)
    char_creation: list[CharCreationScene] = Field(default_factory=list)
    visual_style: VisualStyle
    progression: ProgressionConfig
    axes: AxesConfig
    audio: AudioConfig
    cultures: list[Culture] = Field(default_factory=list)
    prompts: Prompts
    tropes: list[TropeDefinition] = Field(default_factory=list)
    # Epic 22 — seed trope deck. Sibling to ``tropes`` (macro arcs);
    # loaded from ``<pack>/seed_tropes.yaml`` by ``load_genre_pack``.
    # Empty when the file is absent — no silent fallback to a shared
    # default deck.
    seed_tropes: list[SeedTrope] = Field(default_factory=list)
    beat_vocabulary: BeatVocabulary | None = None
    chassis_classes: ChassisClassesConfig | None = None
    achievements: list[Achievement] = Field(default_factory=list)
    voice_presets: VoicePresets | None = None
    power_tiers: dict[str, list[PowerTier]] = Field(default_factory=dict)
    worlds: dict[str, World] = Field(default_factory=dict)
    scenarios: dict[str, ScenarioPack] = Field(default_factory=dict)
    drama_thresholds: DramaThresholds | None = None
    inventory: InventoryConfig | None = None
    openings: list[Opening] = Field(default_factory=list)
    backstory_tables: BackstoryTables | None = None
    equipment_tables: EquipmentTables | None = None
    classes: list[ClassDef] = Field(default_factory=list)
    base_archetypes: BaseArchetypes | None = None
    archetype_constraints: ArchetypeConstraints | None = None
    npc_traits: NpcTraitsDatabase | None = None
    projection_rules: ProjectionRules | None = None
    visibility_baseline: VisibilityBaseline | None = None
    lethality_policy: LethalityPolicy | None = None
    source_dir: Path | None = None
    client_theme_css: str | None = None
    """Raw contents of the genre's top-level ``client_theme.css`` if present.

    ADR-079 (Genre Theme System Unification): genre CSS is the single source
    of truth for all theme tokens (genre identity vars + Tailwind vars).
    The server emits this via SESSION_EVENT{theme_css} on connect, so the
    UI's ``useGenreTheme`` hook can inject it as a ``<style>`` tag and set
    ``data-genre`` on ``<html>``. If a world ships its own
    ``worlds/<slug>/client_theme.css``, the world value wins; otherwise this
    genre-level value is sent.
    """

    # Convenience accessor for pack name
    @property
    def name(self) -> str:
        return self.meta.name


# ClassDef.saving_throws uses a TYPE_CHECKING-only import of SavingThrowsTable to
# avoid a circular dependency (character → rules → game.beat_kinds → game → genre).
# Now that pack.py has resolved rules.py (SavingThrowsTable is in scope), we rebuild
# ClassDef so pydantic v2 can materialise the forward reference.
ClassDef.model_rebuild(_types_namespace={"SavingThrowsTable": SavingThrowsTable})
