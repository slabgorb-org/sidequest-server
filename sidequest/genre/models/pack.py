"""GenrePack aggregate root and PackMeta.

Port of sidequest-genre/src/models/pack.rs.

Note: In Rust, GenrePack is assembled by the loader from multiple YAML files.
In Python, we represent it as a structured aggregate that can be built
by the loader (Story 41-3). The individual components are validated models.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sidequest.game.projection.rules import ProjectionRules
from sidequest.genre.models.archetype_axes import BaseArchetypes
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.archetype_funnels import ArchetypeFunnels
from sidequest.genre.models.audio import AudioConfig
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.genre.models.axes import AxesConfig
from sidequest.genre.models.bestiary import Bestiary
from sidequest.genre.models.character import (
    Background,
    BackstoryTables,
    CharCreationScene,
    ClassDef,
    EquipmentTables,
    Focus,
    NpcArchetype,
    VisualStyle,
)
from sidequest.genre.models.chassis import ChassisClassesConfig
from sidequest.genre.models.culture import Culture
from sidequest.genre.models.inventory import GearDef, InventoryConfig
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
from sidequest.genre.models.premises import BlocDef, PremiseDef, WitnessedActArchetype
from sidequest.genre.models.progression import ProgressionConfig
from sidequest.genre.models.psionics import PsionicDisciplineCatalog
from sidequest.genre.models.rigs_world import ChassisInstanceConfig
from sidequest.genre.models.rules import FateHintSeed, RulesConfig, SavingThrowsTable
from sidequest.genre.models.scenario import ScenarioPack
from sidequest.genre.models.theme import GenreTheme
from sidequest.genre.models.tropes import SeedTrope, TropeDefinition
from sidequest.genre.models.visibility import VisibilityBaseline
from sidequest.genre.models.world import CartographyConfig, WorldConfig
from sidequest.genre.models.wwn_spell import WwnSpellCatalog
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.saints import SaintRegistry
from sidequest.mutation.stocks import StockRegistry


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
    # Picker metadata (Epic 66). Populated only on type=player_picker entries;
    # blank on canon NPC entries. `id` is the explicit slug (also the rendered
    # PNG filename and the catalog ref suffix); culture/archetype/sex drive the
    # UI soft-suggest; backdrop_poi names the history.yaml POI used as the
    # render backdrop (empty => plain portrait).
    id: str = ""
    culture: str = ""
    archetype: str = ""
    sex: str = ""
    backdrop_poi: str = ""


def _slugify_picker_name(name: str) -> str:
    """EXACT mirror of ``scripts/generate_portrait_images._slugify_name``
    (orchestrator repo), which itself mirrors the daemon's
    ``sidequest_daemon.media.catalogs._slugify_name``: lowercase, collapse
    whitespace runs to ``_``, drop everything but ``[a-z0-9_-]``.

    THREE divergent slug rules already exist in this codebase (render_common
    keeps non-ASCII, slugify_player_name NFKD-folds, reference_slug
    hyphenates) — do NOT "improve" this transform independently. The
    generator names the on-disk ``<slug>.png`` with its ``_slugify_name``;
    any drift here means the REST roster serves URLs that 404. The two
    functions must stay in lockstep. (No NFKD fold here: the generator
    doesn't fold, and matching its output is the load-bearing contract.)
    """
    lowered = name.strip().lower()
    collapsed = re.sub(r"\s+", "_", lowered)
    return re.sub(r"[^a-z0-9_-]", "", collapsed)


def picker_portrait_slug(entry: PortraitManifestEntry) -> str:
    """Catalog slug for a picker portrait entry (Epic 66).

    Explicit ``id`` wins (used verbatim, as the generator does); entries
    without an ``id`` fall back to the slugified ``name`` via
    :func:`_slugify_picker_name` — the exact transform the render script
    (``scripts/generate_portrait_images._slugify_name``) applies when naming
    the rendered PNG. The slug doubles as the rendered PNG filename and the
    ``Character.portrait_ref`` value — the single derivation shared by the
    REST roster endpoint (``GET /api/chargen/portraits``) and the chargen
    ``portrait_confirm`` validation, so the two surfaces can never disagree
    on a slug.
    """
    return entry.id or _slugify_picker_name(entry.name)


def picker_portrait_slugs(world: World) -> set[str]:
    """All ``type=player_picker`` portrait slugs a world ships (Epic 66).

    Empty set for worlds whose manifest holds only canon NPC entries (or no
    manifest at all).
    """
    return {
        picker_portrait_slug(entry)
        for entry in world.portrait_manifest
        if entry.character_type == "player_picker"
    }


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
    is_cluster: bool = False
    """Multi-system cluster flag (Story 104-1 / M-A). Loader-computed at load
    time via ``detect_is_cluster(world_path)`` — True iff the world declares more
    than one system (sector-graph ``system`` nodes, else ``systems/`` entries,
    else a definite single). The in-game MAP_UPDATE payload reads this so the UI
    (M-B) can retire its ``regionCount > 1`` heuristic. Defaults False: a World
    is its own single system until detection proves otherwise (never None /
    "unknown" — spec AC4)."""
    cultures: list[Culture] = Field(default_factory=list)
    tropes: list[TropeDefinition] = Field(default_factory=list)
    archetypes: list[NpcArchetype] = Field(default_factory=list)
    visual_style: Any = None  # can be VisualStyle or richer world-level JSON
    theme: GenreTheme | dict[str, Any] | None = None
    """World-tier theme, epic 74. **Two runtime shapes** — the union is load-
    bearing, not cosmetic: a ``dict`` when the world authors its own
    ``worlds/<slug>/theme.yaml`` (loaded raw, free-form override), or a
    ``GenreTheme`` when the loader falls back to the genre theme during the
    transitional refactor (story 74-1). Consumers MUST branch on the type
    (``isinstance(world.theme, GenreTheme)``) — do not assume attribute access.
    Never ``None`` on a loaded World: the loader rejects a world that resolves
    no theme from either tier (No Silent Fallbacks)."""
    audio: dict[str, Any] | None = None
    """World-tier audio (``worlds/<slug>/audio.yaml``), epic 74. A raw ``dict``
    when the world authors one; ``None`` when it authors none (genre audio still
    serves). World audio is a free-form hard-override (e.g.
    ``spaghetti_western/five_points``), not the strict genre ``AudioConfig`` —
    hence ``dict``, never the typed model."""
    history: Any = None
    legends_raw: Any = None
    portrait_manifest: list[PortraitManifestEntry] = Field(default_factory=list)
    archetype_funnels: ArchetypeFunnels | None = None
    openings: list[Opening] = Field(default_factory=list)
    authored_npcs: list[AuthoredNpc] = Field(default_factory=list)
    char_creation: list[CharCreationScene] = Field(default_factory=list)
    classes: list[ClassDef] = Field(default_factory=list)
    """World-tier class/calling CAST/CATALOG (``worlds/<slug>/classes.yaml``),
    epic 94. Genre/world boundary correction (supersedes ADR-120
    "mechanics-in-genre"): a world's classes (C&C kits, Victoria callings) are
    the cast a world ships, NOT a genre mechanic — the genre tier is the rulebook
    only. Empty list when the world authors no classes (e.g. an axis-archetype
    world). Consumers read this world-first; the genre-tier
    ``GenrePack.classes`` is the shared default for packs that have not migrated
    classes down to the world tier (no silent fallback to a fabricated roster)."""
    chassis_instances: list[ChassisInstanceConfig] = Field(default_factory=list)
    chassis_classes: ChassisClassesConfig | None = None
    """World-tier rig CAST/CATALOG (``worlds/<slug>/chassis_classes.yaml``), epic
    94. Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    chassis classes are the cast of rigs a world ships, NOT a genre mechanic — the
    genre tier is the rulebook only. ``None`` when the world authors no rigs (a
    valid choice, not a fallback). Consumers read this world-first; the old
    genre-tier ``GenrePack.chassis_classes`` is now ``None`` for migrated packs."""
    seed_tropes: list[SeedTrope] = Field(default_factory=list)
    """World-tier seed-trope deck (``worlds/<slug>/seed_tropes.yaml``), epic 94.
    Genre/world boundary correction: the seeds a world plants are CAST/CATALOG,
    not a genre mechanic. Empty list when the world authors no deck — no silent
    fallback to a shared default. Consumers read this world-first."""
    wwn_spell_catalog: WwnSpellCatalog | None = None
    """World-tier WWN spell CATALOG (``worlds/<slug>/spells_wwn.yaml``), epic 94.
    Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"): a
    world's spell catalog is the CAST/CATALOG of magic a world ships, NOT a genre
    mechanic — the genre tier is the rulebook only (resolution rules, the WWN
    magic block on ``rules.wwn``). ``None`` when the world ships no catalog (a
    valid choice for a pack that keeps a shared catalog at the genre tier — both
    elemental_harmony worlds share one). Consumers read this world-first via
    ``resolve_wwn_spell_catalog``; the genre-tier ``GenrePack.wwn_spell_catalog``
    is the shared default for packs that have not migrated the catalog down (no
    silent fallback to a fabricated catalog)."""
    psionic_discipline_catalog: PsionicDisciplineCatalog | None = None
    """World-tier psionic discipline CATALOG (``worlds/<slug>/disciplines_psionic.yaml``),
    Story 102-6. Same world-over-genre rule as ``wwn_spell_catalog``: the
    disciplines a world ships are CAST/CATALOG, not a genre mechanic (ADR-140).
    ``None`` when the world ships none; consumers read this world-first via
    ``resolve_psionic_discipline_catalog`` with genre-tier fallback."""
    magic_register: str = ""
    items: WorldItemsCatalog | None = None
    inventory: InventoryConfig | None = None
    """World-tier inventory CATALOG (``worlds/<slug>/inventory.yaml``), epic 94.
    Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"): a
    world's item catalog, class starting-kits, starting gold, and currency are
    CAST/CATALOG — the loot a world ships — NOT a genre mechanic. The genre tier
    is the rulebook only. ``None`` when the world authors no inventory (a valid
    choice for a pack that keeps a shared catalog at the genre tier — e.g.
    caverns_and_claudes). Consumers read this world-first via
    ``resolve_inventory``; the genre-tier ``GenrePack.inventory`` is the shared
    default for packs that have not migrated the catalog down to the world tier.
    A world's inventory merges over the genre's per the ADR-145 D3 split (see
    ``resolve_inventory``): the ``item_catalog`` is a **union by item ``id``** —
    the genre SRD baseline is non-droppable, a world entry sharing a baseline
    ``id`` overrides presentation (name/description/lore/narrative_weight) only,
    and the mechanical envelope of a ``mode=verbatim`` baseline item is locked
    (a world re-stat raises ``VerbatimFieldLockError``). ``starting_equipment``,
    ``starting_gold``, and ``currency`` still REPLACE wholesale per world (those
    are world-owned kit choices). Distinct from ``items``
    (``WorldItemsCatalog``), which is a separate named-artifact flavor list, not
    the chargen loadout/catalog surface."""
    gear: list[GearDef] = Field(default_factory=list)
    """World-tier Fate gear CATALOG (``worlds/<slug>/gear.yaml``), story 126-25.
    The Fate analogue of ``inventory`` at the world tier: a world's signature
    found-items (the Oz silver shoes) are world-owned CAST/CATALOG, NOT a genre
    mechanic (ADR-140). Empty list when the world authors none — the common case
    (most worlds ship no world-distinct gear). Loaded UNCONDITIONALLY of ruleset,
    mirroring the genre-tier ``GenrePack.gear``; only the merge INTO the effective
    Fate catalog is fate-gated, at resolution time. Consumers read world-first via
    ``game.ruleset.fate_gear.resolve_fate_gear_catalog`` (world wins per id over the
    genre catalog — the ADR-145 §D3 by-id merge, same rule as ``resolve_inventory``)."""
    chargen_seed_table: dict[str, FateHintSeed] = Field(default_factory=dict)
    """World-tier narrative-chargen seed override (``worlds/<slug>/chargen_seed_table.yaml``),
    story 126-24. A ``narrative-hint -> FateHintSeed`` map that overrides/extends the
    genre-tier ``rules.fate.chargen_seed_table`` per hint key (world wins). Empty when the
    world authors none — the genre table then serves unchanged (the common case). Loaded
    UNCONDITIONALLY of ruleset, mirroring ``World.gear``; consumers resolve world-first via
    ``game.ruleset.fate_chargen.resolve_fate_chargen_seed_table``. TYPED (not the ``extra``
    bag) so pydantic coerces the authored dict to ``FateHintSeed`` — ``.pyramid`` / ``.aspects``
    are real attributes, never raw dicts (the crash the Reviewer flagged in rework round 1)."""
    equipment_tables: EquipmentTables | None = None
    """World-tier chargen kit override (``worlds/<slug>/equipment_tables.yaml``),
    story 120-4. The genre tier is the SRD rulebook; a world's dungeon/flavor kit
    additions are CAST/CATALOG (ADR-140). ``None`` when the world authors none —
    the genre-tier ``GenrePack.equipment_tables`` then serves unchanged (additive:
    no behavior change for unmigrated worlds). When present, consumers resolve
    world-first via ``server.dispatch.equipment_tables_resolve.resolve_equipment_tables``,
    which MERGES the world over the genre: ``class_tables`` append per-slot within
    each kit (genre items first), ``guaranteed_grants`` append by kit id, and
    ``rolls_per_slot`` overrides per key. This re-adds the no-SRD-analog gear that a
    verbatim genre baseline can't carry (the caverns_and_claudes case, story 120-1)."""
    bestiary: Bestiary | None = None
    """World-tier ``worlds/<slug>/bestiary.yaml`` (genre/world repoint): SRD-
    aligned combat-layer stat blocks specific to this world. When present it
    REPLACES the genre-tier pool for this world (same world-over-genre rule as
    cultures/archetypes — see :meth:`GenrePack.effective_bestiary`), so a world
    with a canonical roster (e.g. barsoom's Martian fauna) is never polluted by
    the genre's generic creatures. ``None`` when the world authors none, in
    which case the genre-tier ``GenrePack.bestiary`` serves."""
    saints: SaintRegistry | None = None
    """World-tier Saint canon (``worlds/<slug>/saints.yaml``), story 103-1.
    Curated chargen presets over the genre-tier AWN mutation catalog (ADR-140:
    the world owns the cast and catalog; the genre owns the mutation rulebook).
    ``None`` when the world authors no Saints — a valid authored choice
    (flickering_reach stays Saint-less per the AWN rebase addendum), never a
    fallback. A present saints.yaml is cross-validated against
    ``GenrePack.mutations`` at load time and fails loud on any unresolvable id;
    saints.yaml in a pack with no mutation catalog is a configuration error."""
    stocks: StockRegistry | None = None
    """World-tier stock roster (``worlds/<slug>/stocks.yaml``), story 103-2.
    Chargen entry-path trait sets built from AWN primitives (build plan §D-B);
    one generic application path, zero per-stock engine cases. ``None`` when
    the world authors no stocks — a valid authored choice (single-path chargen,
    the flickering_reach shape), never a fallback. A present stocks.yaml is
    cross-validated against ``GenrePack.mutations`` at load time and fails loud
    on any unresolvable granted id; stocks.yaml in a pack with no mutation
    catalog is a configuration error."""
    scenarios: dict[str, ScenarioPack] = Field(default_factory=dict)
    """ADR-053 scenarios authored at world tier (``worlds/<slug>/scenarios/``).

    Populated by the loader (Story 71-32). A world either declares its own
    scenarios here or declares none (``{}``) — there is NO silent fallback to
    pack-level ``GenrePack.scenarios``. ``bind_scenario`` binds only the active
    world's scenarios; an empty dict means "this world has no mystery," a valid
    authored choice, not a misconfiguration.
    """
    premises: list[PremiseDef] = Field(default_factory=list)
    """World-tier political illusions (``worlds/<slug>/premises.yaml``), spec
    2026-06-02 wry_whimsy substrate. Empty list when the world authors none — a
    valid authoring choice (a world with no humbug to topple), NOT a fallback to
    any genre default. Content-tier only in Plan 1; Plan 2 hydrates the live
    ``PremiseState`` dials from these."""
    blocs: list[BlocDef] = Field(default_factory=list)
    """World-tier populations the outsider can move (``worlds/<slug>/premises.yaml``).
    Empty list when the world authors none."""
    backgrounds: dict[str, Background] = Field(default_factory=dict)
    """World-tier background CATALOG (``worlds/<slug>/backgrounds.yaml``), ADR-143.
    Genre/world boundary: a world's backgrounds are CAST/CATALOG, not a genre
    mechanic. Empty dict when the world authors none (a valid choice — world may
    share the genre-tier roster). Consumers resolve world-first via
    ``resolve_backgrounds``; the genre-tier ``GenrePack.backgrounds`` is the
    shared default."""
    foci: dict[str, Focus] = Field(default_factory=dict)
    """World-tier focus CATALOG (``worlds/<slug>/foci.yaml``), ADR-143.
    Same world-over-genre rule as ``backgrounds``. Empty dict when the world
    authors none. Consumers resolve world-first via ``resolve_foci``."""
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
    # Epic 74 — genre tier is mechanics-only. Flavor (lore/theme/audio) becomes
    # optional at the genre tier; the world tier is authoritative. None when the
    # pack ships no genre-tier file (live packs still ship them until the
    # per-world migration, so these stay non-None at runtime for now).
    lore: Lore | None = None
    theme: GenreTheme | None = None
    archetypes: list[NpcArchetype] = Field(default_factory=list)
    witnessed_acts: list[WitnessedActArchetype] = Field(default_factory=list)
    """Genre-tier witnessed-act vocabulary (``witnessed_acts.yaml``), spec
    2026-06-02. The act archetypes a world's premises/blocs bind by id. Empty
    list when the pack ships no political layer (mechanics-in-genre per ADR-120)."""
    char_creation: list[CharCreationScene] = Field(default_factory=list)
    # Optional per the 2026-05-29 directive: all visual prompts live at world
    # level (each world ships its own visual_style.yaml). A pack-level
    # visual_style.yaml is no longer required; when absent this is None and
    # the daemon resolves style from the world scope. No silent fallback —
    # if a render needs style and neither world nor genre supplies it, the
    # daemon's StyleCatalog fails loud.
    visual_style: VisualStyle | None = None
    progression: ProgressionConfig
    axes: AxesConfig
    audio: AudioConfig | None = None  # epic 74: genre-optional, world-authoritative
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
    power_tiers: dict[str, list[PowerTier]] = Field(default_factory=dict)
    worlds: dict[str, World] = Field(default_factory=dict)
    scenarios: dict[str, ScenarioPack] = Field(default_factory=dict)
    drama_thresholds: DramaThresholds | None = None
    inventory: InventoryConfig | None = None
    # Fate gear catalog (genre-tier ``gear.yaml``). The lightweight Fate analogue
    # of ``inventory`` — Fate has no equipment economy, so gear compiles into the
    # FateSheet at chargen rather than living as carried CatalogItems (ADR-144 gear
    # model, 114-10). World-tier gear merge is a future story (no pack authors
    # world-distinct gear yet).
    gear: list[GearDef] = Field(default_factory=list)
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
    wwn_spell_catalog: WwnSpellCatalog | None = None
    psionic_discipline_catalog: PsionicDisciplineCatalog | None = None
    """Genre-tier psionic discipline catalog (the shared default for packs that
    keep one catalog at the genre tier). Story 102-6 — discipline catalogs are
    content (ADR-140 "Crunch in the Genre"). None = the pack ships no psionics
    (a deliberate authoring choice). Consumers read world-first via
    ``resolve_psionic_discipline_catalog``."""
    bestiary: Bestiary | None = None
    """Pack-root ``bestiary.yaml`` (story 90-1): SRD-aligned combat-layer stat
    blocks for ruleset-module packs. None when the file is absent — encountergen
    fails loud when the bound ruleset is non-native and this is None (the
    bestiary is REQUIRED for ruleset-module packs; native packs ignore it)."""
    mutations: MutationCatalog | None = None
    """Genre-tier ``mutations.yaml`` (AWN Plan 2): the mutation catalog the
    awn ruleset's mutation subsystem resolves against. None = the pack has
    no mutation system (a deliberate authoring choice, like magic.yaml)."""
    backgrounds: dict[str, Background] = Field(default_factory=dict)
    """Genre-tier background CATALOG (``backgrounds.yaml``), ADR-143.
    Background definitions keyed by id. Empty dict when the pack ships no
    backgrounds.yaml — a legitimate "pack authors none" state (absent ≠
    error). Consumers resolve world-first via ``resolve_backgrounds``;
    world-tier ``World.backgrounds`` replaces this dict wholesale when
    non-empty (same world-over-genre rule as classes/foci)."""
    foci: dict[str, Focus] = Field(default_factory=dict)
    """Genre-tier focus CATALOG (``foci.yaml``), ADR-143.
    Focus definitions keyed by id. Empty dict when absent (same
    absent-OK rule as backgrounds). Consumers resolve world-first via
    ``resolve_foci``."""
    skills: list[str] = Field(default_factory=list)
    """Genre-tier skill name catalog (``skills.yaml``), ADR-143.
    Ordered list of skill-id strings the pack recognises. Empty list
    when absent — the loader does not require a skills catalog. A present-
    but-malformed file fails loud via pydantic validation. Genre-tier only:
    unlike ``backgrounds``/``foci`` there is no world-tier skills override
    (the skill vocabulary is a pack-wide constant; worlds extend backgrounds/
    foci, not the skill list)."""
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

    # ── World-over-genre resolution (SOUL "Crunch in the Genre, Flavor in
    # the World") ─────────────────────────────────────────────────────────
    # A world that declares its own cultures/archetypes REPLACES the genre
    # set; a world that declares none inherits the genre's. This is the SINGLE
    # resolution every consumer must use — namegen (name generation) and
    # ``pregen.seed_manual`` (Monster-Manual seeding) both call these so a
    # seeded NPC's culture tag resolves against the SAME set the name generator
    # validates against. Reading ``pack.cultures`` raw in one and resolving via
    # the world in the other is the divergence that made perseus_cloud seeding
    # fail (genre name "Hegemonic" handed to a world that only knows
    # spacer/thari/yulan → 0 NPCs seeded, 2026-05-29 session 894).

    def effective_cultures(self, world: str | None) -> tuple[list[Culture], str]:
        """Resolve the active culture list for ``world``.

        Returns ``(cultures, source)`` where ``source`` is ``"world"`` when the
        named world supplies its own non-empty culture list, else ``"genre"``
        (including when ``world`` is ``None`` or unknown).
        """
        world_opt = self.worlds.get(world) if world else None
        if world_opt is not None and world_opt.cultures:
            return list(world_opt.cultures), "world"
        return list(self.cultures), "genre"

    def effective_archetypes(self, world: str | None) -> tuple[list[NpcArchetype], str]:
        """Resolve the active archetype list for ``world`` (same rule as
        :meth:`effective_cultures`)."""
        world_opt = self.worlds.get(world) if world else None
        if world_opt is not None and world_opt.archetypes:
            return list(world_opt.archetypes), "world"
        return list(self.archetypes), "genre"

    def effective_bestiary(self, world: str | None) -> tuple[Bestiary | None, str]:
        """Resolve the active bestiary for ``world`` (same world-over-genre rule
        as :meth:`effective_cultures`).

        Returns ``(bestiary, source)`` where ``source`` is ``"world"`` when the
        named world ships its own ``bestiary.yaml``, else ``"genre"`` (including
        when ``world`` is ``None`` or unknown). The genre/world repoint moved
        creature rosters to the world tier ("genre is rulebook only, world owns
        cast/catalog"); ruleset-module packs now author their hostiles in
        ``worlds/<slug>/bestiary.yaml``. The world set REPLACES the genre pool so
        a world with a canonical roster is never polluted by generic genre
        creatures. ``bestiary`` is ``None`` only when NEITHER tier supplies one —
        encountergen fails loud on that for ruleset-module packs (No Silent
        Fallbacks: never a silently-empty encounter pool)."""
        world_opt = self.worlds.get(world) if world else None
        if world_opt is not None and world_opt.bestiary is not None:
            return world_opt.bestiary, "world"
        return self.bestiary, "genre"


# ClassDef.saving_throws uses a TYPE_CHECKING-only import of SavingThrowsTable to
# avoid a circular dependency (character → rules → game.beat_kinds → game → genre).
# Now that pack.py has resolved rules.py (SavingThrowsTable is in scope), we rebuild
# ClassDef so pydantic v2 can materialise the forward reference.
ClassDef.model_rebuild(_types_namespace={"SavingThrowsTable": SavingThrowsTable})
