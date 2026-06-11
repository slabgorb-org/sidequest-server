"""Unified genre pack loader.

Port of sidequest-genre/src/loader.rs (638 LOC).

A single function loads an entire genre pack from a directory, reading all
YAML files and assembling them into a typed GenrePack. A GenreLoader class
supports multi-path search (local → home → install).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sidequest.game.disposition import (
    DEFAULT_ATTITUDE_THRESHOLDS,
    configure_attitude_thresholds,
)
from sidequest.genre.cache import GenreCache
from sidequest.genre.error import GenreLoadError, GenreNotFoundError, PackError
from sidequest.genre.genre_code import GenreCode
from sidequest.genre.models.archetype_axes import BaseArchetypes
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.archetype_funnels import ArchetypeFunnels
from sidequest.genre.models.audio import AudioConfig
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.genre.models.axes import AxesConfig
from sidequest.genre.models.bestiary import Bestiary
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
from sidequest.genre.models.pack import (
    GenrePack,
    PackMeta,
    PortraitManifestEntry,
    World,
)
from sidequest.genre.models.premises import PremisesFile, WitnessedActsFile
from sidequest.genre.models.progression import ProgressionConfig
from sidequest.genre.models.psionics import PsionicDisciplineCatalog
from sidequest.genre.models.rigs_world import ChassisInstanceConfig, RigsWorldConfig
from sidequest.genre.models.rules import RulesConfig
from sidequest.genre.models.scenario import ScenarioNpc, ScenarioPack
from sidequest.genre.models.theme import GenreTheme
from sidequest.genre.models.tropes import SeedTrope, TropeDefinition
from sidequest.genre.models.world import CartographyConfig, NavigationMode, WorldConfig
from sidequest.genre.models.wwn_spell import WwnSpellCatalog
from sidequest.genre.premise_validate import validate_premises
from sidequest.genre.resolve import resolve_trope_inheritance
from sidequest.mutation.catalog import load_mutation_catalog
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.saints import SaintRegistry, load_saint_registry
from sidequest.mutation.stocks import StockRegistry, load_stock_registry

# ---------------------------------------------------------------------------
# Default search paths (mirrors Rust loader convention)
# ---------------------------------------------------------------------------

DEFAULT_GENRE_PACK_SEARCH_PATHS: list[Path] = [
    # Orchestrator root / sidequest-content — the canonical dev layout.
    # __file__ = sidequest-server/sidequest/genre/loader.py; parents[3] = orchestrator root.
    Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs",
    # CWD fallbacks for when the server is run from elsewhere.
    Path.cwd() / "sidequest-content" / "genre_packs",
    Path.cwd().parent / "sidequest-content" / "genre_packs",
    Path.home() / ".sidequest" / "genre_packs",
]


# ---------------------------------------------------------------------------
# Low-level YAML helpers
# ---------------------------------------------------------------------------


def _load_yaml[T](path: Path, type_: type[T]) -> T:
    """Load and parse a required YAML file.

    Port of Rust load_yaml<T>(path). Required; raises GenreLoadError on any
    failure (file missing, unreadable, or schema mismatch). No silent fallbacks.

    Raises:
        GenreLoadError: If the file cannot be read or parsed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GenreLoadError(path=path, detail=str(e)) from e

    try:
        raw = yaml.safe_load(text)
        return type_.model_validate(raw)  # type: ignore[attr-defined]
    except Exception as e:
        raise GenreLoadError(path=path, detail=str(e)) from e


def _load_yaml_optional[T](path: Path, type_: type[T]) -> T | None:
    """Load and parse an optional YAML file. Returns None if file doesn't exist.

    Port of Rust load_yaml_optional<T>(path). If present, failure is still loud.

    Raises:
        GenreLoadError: If the file exists but cannot be read or parsed.
    """
    if not path.exists():
        return None
    return _load_yaml(path, type_)


def _load_yaml_raw(path: Path) -> Any:
    """Load raw YAML as a Python object (no model validation).

    Used for flexible-schema files (legends, visual_style, history).

    Raises:
        GenreLoadError: If the file cannot be read or parsed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GenreLoadError(path=path, detail=str(e)) from e

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise GenreLoadError(path=path, detail=str(e)) from e


def _load_yaml_raw_optional(path: Path) -> Any | None:
    """Load raw YAML optionally. Returns None if file doesn't exist."""
    if not path.exists():
        return None
    return _load_yaml_raw(path)


def _parse_char_creation_scenes(raw: Any | None, *, path: Path) -> list[CharCreationScene]:
    """Validate and parse a ``char_creation.yaml`` payload into scenes.

    Contract (see ``server/dispatch/char_creation_resolve.py``): a world's
    char_creation list REPLACES the genre's wholesale — there is no per-scene
    merge, and ``inherits_scenes_from_genre_pack`` is **not** a supported key.
    So the on-disk shape is a BARE LIST of scene mappings.

    - ``None`` (absent or empty file) → ``[]``. For a world this means "inherit
      the genre's scenes"; for the genre it means "no chargen scenes".
    - a ``list`` → parsed scenes.
    - anything else (a mapping such as the unsupported
      ``inherits_scenes_from_genre_pack`` wrapper, or a scalar) → **fail loud**.

    The old code silently coerced any non-list to ``[]``, which let a
    mapping-shaped world file degrade to genre scenes with no signal — a No
    Silent Fallbacks violation (the_real_mccoy desert-chargen leak, 2026-06-01).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GenreLoadError(
            path=path,
            detail=(
                f"char_creation.yaml must be a bare list of chargen scenes, got "
                f"{type(raw).__name__}. A world's char_creation list REPLACES the "
                "genre's wholesale — there is no per-scene merge, and "
                "'inherits_scenes_from_genre_pack' is not a supported key. Provide "
                "the complete ordered scene list (origins first), or omit the file "
                "to inherit the genre's scenes."
            ),
        )
    return [CharCreationScene.model_validate(c) for c in raw]


def _load_text_optional(path: Path) -> str | None:
    """Read a text file (UTF-8) if it exists.

    Used for non-YAML pack assets like ``client_theme.css`` (ADR-079).
    Returns ``None`` if the file doesn't exist. Failure to read an existing
    file is loud — same no-silent-fallback rule as the YAML loaders.
    """
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise GenreLoadError(path=path, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Rules config loader (with _from: pointer resolution)
# ---------------------------------------------------------------------------


def _load_rules_config(rules_path: Path, pack_dir: Path) -> RulesConfig:
    """Load and resolve rules.yaml, honoring _from: pointers on confrontation
    interaction_table fields.

    Port of Rust load_rules_config().

    A confrontation may carry its interaction table inline or reference a
    sibling file pack-relative:
        interaction_table:
          _from: dogfight/interactions_mvp.yaml

    The resolver substitutes _from pointers, rejects absolute paths and
    parent-directory traversal, and rejects nested _from chains.

    Raises:
        GenreLoadError: If the file or any referenced file cannot be read.
    """
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError as e:
        raise GenreLoadError(path=rules_path, detail=str(e)) from e

    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise GenreLoadError(path=rules_path, detail=str(e)) from e

    # Walk confrontations[].interaction_table for _from pointers
    if isinstance(value, dict) and "confrontations" in value:
        confrontations = value["confrontations"]
        if isinstance(confrontations, list):
            for conf in confrontations:
                _resolve_confrontation_from_pointers(conf, pack_dir)

    try:
        return RulesConfig.model_validate(value)
    except Exception as e:
        raise GenreLoadError(path=rules_path, detail=str(e)) from e


def _resolve_confrontation_from_pointers(conf: Any, pack_dir: Path) -> None:
    """Walk a single confrontation dict and resolve any _from pointers on its
    interaction_table field. Mutates conf in place.

    Port of Rust resolve_confrontation_from_pointers().
    """
    if not isinstance(conf, dict):
        return
    it_value = conf.get("interaction_table")
    if it_value is None:
        return
    from_rel = _extract_from_pointer(it_value)
    if from_rel is None:
        return
    resolved = _resolve_from_pointer(from_rel, pack_dir)
    conf["interaction_table"] = resolved


def _extract_from_pointer(value: Any) -> str | None:
    """If value is a mapping of shape { _from: "relpath" } (single key), return the string.

    Port of Rust extract_from_pointer().
    """
    if not isinstance(value, dict) or len(value) != 1:
        return None
    return value.get("_from")


def _resolve_from_pointer(rel: str, pack_dir: Path) -> Any:
    """Read a _from-referenced sub-file, enforcing pack-relative path safety
    and rejecting nested _from chains.

    Port of Rust resolve_from_pointer().

    Raises:
        GenreLoadError: If the path is absolute, contains .., or the sub-file
            contains a nested _from pointer.
    """
    rel_path = Path(rel)

    if rel_path.is_absolute():
        raise GenreLoadError(
            path=rel,
            detail=f"_from path must be pack-relative (got absolute path: {rel})",
        )

    # Reject parent-directory traversal
    parts = rel_path.parts
    for part in parts:
        if part == "..":
            raise GenreLoadError(
                path=rel,
                detail=f"_from path must not contain parent-directory traversal: {rel}",
            )

    full = pack_dir / rel_path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError as e:
        raise GenreLoadError(path=full, detail=str(e)) from e

    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise GenreLoadError(path=full, detail=str(e)) from e

    # Reject nested _from chains
    if isinstance(value, dict) and "_from" in value:
        raise GenreLoadError(
            path=full,
            detail="nested _from pointers are not allowed",
        )

    return value


# ---------------------------------------------------------------------------
# Legends loader (flexible format)
# ---------------------------------------------------------------------------


def _load_legends_flexible(path: Path) -> tuple[list[Legend], Any]:
    """Load legends.yaml flexibly: accepts Vec<Legend> or a map with a "legends" key.

    Port of Rust load_legends_flexible().

    Returns:
        (legends, legends_raw) where legends_raw is the full raw value if the
        map format was used, else None.
    """
    if not path.exists():
        return [], None

    raw = _load_yaml_raw(path)

    # Try as list of Legend (low_fantasy format)
    if isinstance(raw, list):
        try:
            legends = [Legend.model_validate(item) for item in raw]
            return legends, None
        except Exception:
            pass

    # Try as a map — extract "legends" key if present, keep full raw value
    if isinstance(raw, dict):
        legends_raw_value: Any = raw
        if "legends" in raw:
            try:
                legends = [Legend.model_validate(item) for item in raw["legends"]]
                return legends, legends_raw_value
            except Exception as e:
                raise GenreLoadError(path=path, detail=str(e)) from e
        return [], legends_raw_value

    raise GenreLoadError(path=path, detail="unrecognized legends.yaml format")


# ---------------------------------------------------------------------------
# Subdirectory loader
# ---------------------------------------------------------------------------


def _load_subdirectories(
    pack_path: Path,
    subdir: str,
    loader: Any,
) -> dict[str, Any]:
    """Load all subdirectories of {pack_path}/{subdir}/ into a dict.

    Port of Rust load_subdirectories().
    """
    dir_path = pack_path / subdir
    if not dir_path.exists():
        return {}

    result: dict[str, Any] = {}
    try:
        entries = sorted(dir_path.iterdir())
    except OSError as e:
        raise GenreLoadError(path=dir_path, detail=str(e)) from e

    for entry in entries:
        if entry.is_dir():
            slug = entry.name
            item = loader(entry)
            result[slug] = item

    return result


# ---------------------------------------------------------------------------
# Cross-file validators (canned-openings spec §1.4)
# ---------------------------------------------------------------------------


def _validate_opening_setting_references(
    openings: list[Opening],
    chassis_instances: list[ChassisInstanceConfig],
    *,
    world_slug: str,
) -> None:
    """Validators 2, 3: chassis_instance + interior_room references resolve.

    Skipped for location-anchored openings (which have chassis_instance is None).
    """
    chassis_by_id = {c.id: c for c in chassis_instances}
    for op in openings:
        s = op.setting
        if s.chassis_instance is None:
            continue
        chassis = chassis_by_id.get(s.chassis_instance)
        if chassis is None:
            raise GenreLoadError(
                path=f"worlds/{world_slug}/openings.yaml",
                detail=(
                    f"opening {op.id!r} references "
                    f"unknown chassis_instance {s.chassis_instance!r}. "
                    f"Known chassis: {sorted(chassis_by_id.keys())}"
                ),
            )
        if s.interior_room not in chassis.interior_rooms:
            raise GenreLoadError(
                path=f"worlds/{world_slug}/openings.yaml",
                detail=(
                    f"opening {op.id!r} references "
                    f"interior_room {s.interior_room!r}, which is not in "
                    f"chassis {chassis.id!r}'s interior_rooms "
                    f"{chassis.interior_rooms}."
                ),
            )


def _validate_crew_npc_references(
    chassis_instances: list[ChassisInstanceConfig],
    authored_npcs: list[AuthoredNpc],
    *,
    world_slug: str,
) -> None:
    """Validator 4: every chassis_instance.crew_npcs entry must resolve to an
    AuthoredNpc.id in worlds/{slug}/npcs.yaml.

    A chassis with an empty crew_npcs list is valid.
    """
    npc_ids = {n.id for n in authored_npcs}
    for chassis in chassis_instances:
        unknown = [c for c in chassis.crew_npcs if c not in npc_ids]
        if unknown:
            raise GenreLoadError(
                path=f"worlds/{world_slug}/rigs.yaml",
                detail=(
                    f"chassis {chassis.id!r} declares crew_npcs {unknown!r} "
                    f"that do not resolve to any AuthoredNpc.id in "
                    f"worlds/{world_slug}/npcs.yaml. "
                    f"Known authored NPCs: {sorted(npc_ids)}"
                ),
            )


def _validate_authored_npc_uniqueness(
    authored_npcs: list[AuthoredNpc],
    *,
    world_slug: str,
) -> None:
    """Validator 5: AuthoredNpc.id unique per world."""
    seen: set[str] = set()
    for npc in authored_npcs:
        if npc.id in seen:
            raise GenreLoadError(
                path=f"worlds/{world_slug}/npcs.yaml",
                detail=(
                    f"duplicate AuthoredNpc.id {npc.id!r}. "
                    "Each NPC id must be unique within a world."
                ),
            )
        seen.add(npc.id)


def _validate_present_npcs_resolve(
    openings: list[Opening],
    authored_npcs: list[AuthoredNpc],
    *,
    world_slug: str,
) -> None:
    """Validator 12 part-b: every Opening.setting.present_npcs entry resolves
    to an AuthoredNpc.id."""
    npc_ids = {n.id for n in authored_npcs}
    for op in openings:
        unknown = [n for n in op.setting.present_npcs if n not in npc_ids]
        if unknown:
            raise GenreLoadError(
                path=f"worlds/{world_slug}/openings.yaml",
                detail=(
                    f"opening {op.id!r} declares present_npcs {unknown!r} "
                    f"that do not resolve to any AuthoredNpc. "
                    f"Known: {sorted(npc_ids)}"
                ),
            )


def _validate_opening_region_bindings(
    openings: list[Opening],
    cartography: CartographyConfig,
    *,
    world_slug: str,
) -> None:
    """Every opening's ``setting.region_id`` must resolve to a real cartography node.

    Playtest 2026-05-25 [BUG] flickering_reach: ``current_region`` is a
    load-bearing region id (it MUST be a declared cartography node — see
    :func:`sidequest.game.region_init.init_region_location`), but the narrator
    only emits a free-text ``location_label`` and never a region id. An opening
    that anchors the party somewhere other than ``cartography.starting_region``
    must declare an explicit ``setting.region_id`` so the server can rebind
    ``current_region`` to the opening's location at chargen-complete.

    This validator enforces the authored binding at load time: every
    location-anchored opening that declares a ``region_id`` must name a real
    region in ``cartography.regions``. No silent fallback / no fuzzy free-text
    matching (CLAUDE.md No-Silent-Fallbacks): a dangling ``region_id`` is a
    pack-authoring bug and fails the world load loudly.

    Worlds whose cartography declares no regions (legacy flavor-only
    cartography) cannot validate region ids; any ``region_id`` declared there
    is treated as an authoring error and reported, rather than silently
    accepted.
    """
    region_ids = set(cartography.regions)
    for op in openings:
        region_id = op.setting.region_id
        if region_id is None:
            continue
        if region_id not in region_ids:
            raise GenreLoadError(
                path=f"worlds/{world_slug}/openings.yaml",
                detail=(
                    f"opening {op.id!r} declares setting.region_id "
                    f"{region_id!r}, which is not a declared cartography "
                    f"region. Known regions: {sorted(region_ids)}"
                ),
            )


def _validate_opening_bank_coverage(
    openings: list[Opening],
    chargen_backgrounds: list[str],
    *,
    world_slug: str,
) -> None:
    """Validators 7 + 8 (canned-openings §1.4).

    7: world ships ≥1 solo opening AND ≥1 MP opening.
       (Mode 'either' counts toward both.)
    8: every chargen background must be reachable by some solo-eligible
       opening (matching ``triggers.backgrounds: [...]`` OR a fallback entry
       with ``triggers.backgrounds: []``).

    An empty ``chargen_backgrounds`` list disables Validator 8 (no constraint
    to satisfy). World-load currently passes ``[]`` whenever a world's
    ``char_creation.yaml`` has no scene with id ``"background"`` — see the
    wiring site in ``_load_single_world``.
    """
    path = f"worlds/{world_slug}/openings.yaml"

    has_solo = any(op.triggers.mode in ("solo", "either") for op in openings)
    has_mp = any(op.triggers.mode in ("multiplayer", "either") for op in openings)

    if not has_solo:
        raise GenreLoadError(
            path=path,
            detail=(
                "no solo opening declared. openings.yaml must include "
                "at least one entry with triggers.mode in {'solo', 'either'}."
            ),
        )
    if not has_mp:
        raise GenreLoadError(
            path=path,
            detail=(
                "no multiplayer opening declared. openings.yaml must include "
                "at least one entry with triggers.mode in {'multiplayer', 'either'}."
            ),
        )

    # Validator 8: every chargen background reachable by a solo-eligible opening.
    solo_eligible = [op for op in openings if op.triggers.mode in ("solo", "either")]
    has_fallback = any(not op.triggers.backgrounds for op in solo_eligible)
    if has_fallback:
        return  # fallback covers all backgrounds

    covered: set[str] = set()
    for op in solo_eligible:
        covered.update(op.triggers.backgrounds)

    uncovered = [bg for bg in chargen_backgrounds if bg not in covered]
    if uncovered:
        raise GenreLoadError(
            path=path,
            detail=(
                f"chargen backgrounds {uncovered!r} are not reachable by "
                "any solo opening. Either add a background-keyed entry per "
                "uncovered background OR add a fallback entry with "
                "`triggers.backgrounds: []`."
            ),
        )


# ---------------------------------------------------------------------------
# Class / beat cross-reference validators (Task 5: C&C B/X class beats)
# ---------------------------------------------------------------------------


def _validate_class_filter_refs(rules: RulesConfig, classes: list[ClassDef]) -> None:
    """Loud-fail if any beat.class_filter references a class not in classes.yaml,
    if any class.encounter_beat_choices references a missing beat ID,
    or if a class in allowed_classes has empty encounter_beat_choices.

    Only runs when classes list is non-empty (packs without classes.yaml are
    not subject to these rules).
    """
    if not classes:
        return

    declared_classes = {c.display_name for c in classes}
    all_beat_ids: set[str] = set()
    for cd in rules.confrontations:
        for beat in cd.beats:
            all_beat_ids.add(beat.id)
            if beat.class_filter is not None:
                missing = [c for c in beat.class_filter if c not in declared_classes]
                if missing:
                    raise PackError(
                        f"beat '{beat.id}' class_filter references class(es) "
                        f"{missing!r} not declared in classes.yaml"
                    )

    for c in classes:
        if c.display_name in rules.allowed_classes:
            if not c.encounter_beat_choices:
                raise PackError(
                    f"class '{c.display_name}' encounter_beat_choices is empty "
                    f"(class is in allowed_classes and must declare beat choices)"
                )
            missing_beats = [b for b in c.encounter_beat_choices if b not in all_beat_ids]
            if missing_beats:
                raise PackError(
                    f"class '{c.display_name}' encounter_beat_choices "
                    f"references beat id(s) {missing_beats!r} not in pool"
                )


def _validate_saving_throws_refs(classes: list[ClassDef], *, has_spell_catalogs: bool) -> None:
    """When the pack ships any spell catalog, every class must declare
    saving_throws. Otherwise spells with save effects cannot resolve.

    No-op for packs without spells (heavy_metal, tea_and_murder, etc.) where
    saves aren't a wired subsystem yet.

    Task 8 — C&C B/X saving throws pack-load validation.
    """
    if not has_spell_catalogs:
        return
    if not classes:
        return
    missing = [c.display_name for c in classes if c.saving_throws is None]
    if missing:
        raise PackError(
            f"pack has spell catalogs but classes missing saving_throws: {missing}. "
            f"Spells with save effects cannot resolve without a B/X B26 table per class."
        )


def _load_wwn_spell_catalog(
    path: Path,
    rules: RulesConfig,
    classes: list[ClassDef],
) -> WwnSpellCatalog | None:
    """Load spells_wwn.yaml for wwn packs; return None for non-wwn packs.

    Fail-loud contract: if ruleset == 'wwn' AND the pack declares at least one
    caster class (magic_access == 'wwn' with non-empty casts_per_day_by_level)
    AND spells_wwn.yaml is absent, raise GenreLoadError.  No silent fallback.
    """
    if rules.ruleset != "wwn":
        return None

    spells_file = path / "spells_wwn.yaml"

    if not spells_file.exists():
        # Fail loud if there are caster classes — they need a spell catalog.
        caster_classes = [
            c
            for c in classes
            if c.magic_access == "wwn"
            and c.wwn_magic is not None
            and bool(c.wwn_magic.casts_per_day_by_level)
        ]
        if caster_classes:
            names = [c.id for c in caster_classes]
            raise GenreLoadError(
                path=spells_file,
                detail=(
                    f"wwn pack has caster classes {names} but spells_wwn.yaml is absent. "
                    "Author a spell catalog or remove the casts_per_day_by_level entries."
                ),
            )
        return None

    from sidequest.genre.models.wwn_spell import load_wwn_spell_catalog as _load

    try:
        return _load(spells_file)
    except Exception as exc:
        raise GenreLoadError(path=spells_file, detail=str(exc)) from exc


def _validate_wwn_starting_prepared_refs(
    classes: list[ClassDef],
    catalog: WwnSpellCatalog | None,
) -> None:
    """For a wwn pack with a loaded catalog, every starting_prepared spell id
    in every class must exist in the catalog.  Fail loud on any unknown id.

    No-op when catalog is None (packs with no spell catalog are covered by the
    caster-without-catalog branch in _load_wwn_spell_catalog).  No-op when no
    class has starting_prepared entries.
    """
    if catalog is None:
        return
    if not classes:
        return

    catalog_ids = {s.id for s in catalog.spells}
    for cls in classes:
        if cls.wwn_magic is None:
            continue
        for spell_id in cls.wwn_magic.starting_prepared:
            if spell_id not in catalog_ids:
                raise GenreLoadError(
                    path=Path("spells_wwn.yaml"),
                    detail=(
                        f"class '{cls.id}' starting_prepared references unknown spell id "
                        f"'{spell_id}'. Add the spell to spells_wwn.yaml or fix the id."
                    ),
                )


# ---------------------------------------------------------------------------
# World loader
# ---------------------------------------------------------------------------


def _load_cartography(yaml_path: Path) -> CartographyConfig:
    """Load cartography.yaml + (optional) sibling rooms.yaml.

    Used by both world (leaf) and dungeon loaders. The rooms.yaml sibling
    is only consulted when navigation_mode == room_graph.
    """
    cartography: CartographyConfig = _load_yaml(yaml_path, CartographyConfig)
    if cartography.navigation_mode == NavigationMode.room_graph:
        rooms_raw = _load_yaml_raw_optional(yaml_path.parent / "rooms.yaml")
        if rooms_raw is not None:
            from sidequest.genre.models.world import RoomDef

            rooms = (
                [RoomDef.model_validate(r) for r in rooms_raw]
                if isinstance(rooms_raw, list)
                else None
            )
            cartography = cartography.model_copy(update={"rooms": rooms})
    return cartography


def _load_openings(
    openings_path: Path,
    *,
    scope: str,
    missing_detail: str,
) -> list[Opening]:
    """Load openings.yaml (mandatory). `scope` is used in error messages
    (e.g. ``worlds/foo`` or ``worlds/hub/dungeons/bar``).
    """
    if not openings_path.exists():
        raise GenreLoadError(path=openings_path, detail=missing_detail)
    openings_raw = _load_yaml_raw(openings_path)
    openings_list_raw = openings_raw.get("openings", []) if isinstance(openings_raw, dict) else []
    return [Opening.model_validate(o) for o in openings_list_raw]


def _load_portrait_manifest(path: Path) -> list[PortraitManifestEntry]:
    portrait_raw = _load_yaml_raw_optional(path)
    if isinstance(portrait_raw, dict) and "characters" in portrait_raw:
        if isinstance(portrait_raw["characters"], list):
            return [PortraitManifestEntry.model_validate(e) for e in portrait_raw["characters"]]
        return []
    if isinstance(portrait_raw, list):
        return [PortraitManifestEntry.model_validate(e) for e in portrait_raw]
    return []


def _load_world_items(items_path: Path, *, world_slug: str) -> WorldItemsCatalog | None:
    """Load a world's optional ``items.yaml`` into a ``WorldItemsCatalog``.

    Returns ``None`` if the file is absent — distinguishes "world has no
    items file" from "world authored empty sections". Raises
    ``GenreLoadError`` for any other failure: malformed yaml, schema
    mismatch, or a duplicate item ``id`` across sections. Loud-fails per
    the project's no-silent-fallback rule.

    Emits a ``state_transition`` watcher event on successful load with
    per-section item counts, mirroring the genre-pack-loaded event so
    the GM panel can prove items wiring actually engaged.
    """
    if not items_path.exists():
        return None

    raw = _load_yaml_raw(items_path)
    try:
        catalog = WorldItemsCatalog.model_validate(raw)
    except Exception as e:
        raise GenreLoadError(path=items_path, detail=str(e)) from e

    # Duplicate-id check across all sections — items are addressed by id
    # in narrator context and game state, so a collision is a content bug
    # we must surface, not paper over.
    seen: dict[str, str] = {}
    for section_name, items in (
        ("named_items", catalog.named_items),
        ("modifier_items", catalog.modifier_items),
        ("reliquaries", catalog.reliquaries),
        ("crimson_remnants", catalog.crimson_remnants),
        ("consumable_items", catalog.consumable_items),
    ):
        for item in items:
            prior = seen.get(item.id)
            if prior is not None:
                raise GenreLoadError(
                    path=items_path,
                    detail=(
                        f"duplicate item id {item.id!r}: first in {prior!r}, "
                        f"again in {section_name!r}. Item ids must be unique "
                        "across the whole items.yaml."
                    ),
                )
            seen[item.id] = section_name

    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "world_items",
            "op": "loaded",
            "world_slug": world_slug,
            "item_count": len(seen),
            **catalog.section_counts(),
            "source": str(items_path),
        },
        component="genre",
    )
    return catalog


def _emit_world_flavor_loaded(field: str, *, world_slug: str, source: Path) -> None:
    """Emit a ``state_transition`` watcher event for a world-tier flavor load.

    Epic 74: the world tier is authoritative for flavor (theme/audio/
    visual_style). Each load fires a span — mirroring the ``world_items`` and
    ``genre_pack`` spans — so the GM panel can prove the world-tier read fired
    rather than the engine improvising from a genre default.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": field,
            "op": "loaded",
            "world_slug": world_slug,
            "source": str(source / f"{field.removeprefix('world_')}.yaml"),
        },
        component="genre",
    )


def _world_lore_seedable_count(lore: WorldLore) -> int:
    """Count the LoreStore fragments a world's ``lore.yaml`` will seed.

    Mirrors the seedable fields read by ``seed_lore_from_world``
    (history / geography / cosmology / factions). A text field counts only when
    it is non-empty AFTER stripping — a whitespace-only string seeds a junk
    fragment and is treated as empty here, matching both the validator
    (``validate/pack._validate_world_lore_seedable``, which ``.strip()``s) and
    the stripped guards in ``seed_lore_from_world``. Inlined rather than
    importing ``game.lore_seeding`` — the genre layer must not depend on the
    game layer (dependency graph, server CLAUDE.md). Keep in sync with that seeder.
    """

    def _seedable_text(value: str | None) -> bool:
        return bool(value and value.strip())

    return (
        int(_seedable_text(lore.history))
        + int(_seedable_text(lore.geography))
        + int(_seedable_text(lore.cosmology))
        + len(lore.factions)
    )


def _require_seedable_world_lore(lore: WorldLore, world_path: Path) -> int:
    """Return the world's seedable lore-fragment count, or raise.

    Epic 74 (story 74-3): lore is world-only and authoritative — genre lore is
    no longer seeded. A world whose lore.yaml carries no SEEDABLE content
    (history/geography/cosmology/factions all empty — e.g. prose stranded under
    non-seedable extra keys) would leave the narrator's LoreStore empty. Raise
    ``GenreLoadError`` naming the world (No Silent Fallbacks; mirrors the
    visibility_baseline / lethality_policy required-surface guards).
    """
    count = _world_lore_seedable_count(lore)
    if count == 0:
        raise GenreLoadError(
            path=world_path / "lore.yaml",
            detail=(
                f"world {world_path.name!r} has no seedable lore — lore.yaml must "
                "populate at least one of history / geography / cosmology / "
                "factions (content under other keys is not seeded into the "
                "LoreStore). An empty world LoreStore is forbidden."
            ),
        )
    return count


def _emit_world_lore_loaded(*, world_slug: str, source: Path, fragment_count: int) -> None:
    """Emit a ``state_transition`` watcher event for a world-tier lore load.

    Epic 74: lore is world-only and authoritative. The load fires a span —
    mirroring the ``world_items`` and ``world_theme``/``world_audio`` spans — so
    the GM panel can prove the world LoreStore was fed from world-tier lore
    rather than the narrator improvising from a deleted genre default.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "world_lore",
            "op": "loaded",
            "world_slug": world_slug,
            "lore_fragment_count": fragment_count,
            "source": str(source / "lore.yaml"),
        },
        component="genre",
    )


def _emit_world_chassis_classes_loaded(*, world_slug: str, source: Path, class_count: int) -> None:
    """Emit a ``state_transition`` watcher event for a world-tier chassis load.

    Epic 94 (genre/world boundary correction): chassis classes are a world-tier
    CAST/CATALOG surface, not a genre mechanic. The load fires a span — mirroring
    the ``world_items`` / ``world_lore`` spans — so the GM panel can prove the
    rig cast was read from the world tier rather than improvised from a removed
    genre default.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "world_chassis_classes",
            "op": "loaded",
            "world_slug": world_slug,
            "class_count": class_count,
            "source": str(source),
        },
        component="genre",
    )


def _emit_world_seed_tropes_loaded(*, world_slug: str, source: Path, seed_count: int) -> None:
    """Emit a ``state_transition`` watcher event for a world-tier seed-deck load.

    Epic 94 (genre/world boundary correction): the seed-trope deck is world-tier
    CAST/CATALOG (the seeds a world plants), not a genre mechanic. The load fires
    a span so the GM panel can prove the deck was read from the world tier.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "world_seed_tropes",
            "op": "loaded",
            "world_slug": world_slug,
            "seed_count": seed_count,
            "source": str(source),
        },
        component="genre",
    )


def _emit_world_classes_loaded(*, world_slug: str, source: Path, class_count: int) -> None:
    """Emit a ``state_transition`` watcher event for a world-tier class load.

    Epic 94 (genre/world boundary correction): a world's classes/callings are a
    world-tier CAST/CATALOG surface — the roster of playable archetypes a world
    ships (C&C kits, Victoria callings) — not a genre mechanic. The genre tier is
    the rulebook only. The load fires a span (mirroring the chassis_classes /
    seed_tropes spans) so the GM panel can prove the class roster the chargen
    pipeline picked up was read from the world tier, not improvised from a
    removed genre default.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "world_classes",
            "op": "loaded",
            "world_slug": world_slug,
            "class_count": class_count,
            "source": str(source),
        },
        component="genre",
    )


def _emit_world_inventory_loaded(
    *, world_slug: str, source: Path, catalog_count: int, class_kit_count: int
) -> None:
    """Emit a ``state_transition`` watcher event for a world-tier inventory load.

    Epic 94 (genre/world boundary correction, supersedes ADR-120
    "mechanics-in-genre"): a world's item catalog, class starting-kits, gold, and
    currency are a world-tier CAST/CATALOG surface — the loot a world ships — not
    a genre mechanic. The genre tier is the rulebook only. The load fires a span
    (mirroring the world_classes / world_spell_catalog spans) so the GM panel can
    prove the loadout the chargen pipeline picked up was read from the world tier,
    not improvised from a removed genre default.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "world_inventory",
            "op": "loaded",
            "world_slug": world_slug,
            "catalog_count": catalog_count,
            "class_kit_count": class_kit_count,
            "source": str(source),
        },
        component="genre",
    )


def _emit_world_spell_catalog_loaded(*, world_slug: str, source: Path, spell_count: int) -> None:
    """Emit a ``state_transition`` watcher event for a world-tier spell-catalog load.

    Epic 94 (genre/world boundary correction, supersedes ADR-120
    "mechanics-in-genre"): a world's WWN spell catalog is a world-tier
    CAST/CATALOG surface — the catalog of magic a world ships — not a genre
    mechanic. The genre tier is the rulebook only. The load fires a span
    (mirroring the world_classes / world_seed_tropes spans) so the GM panel can
    prove the spell catalog the cast pipeline picked up was read from the world
    tier, not improvised from a removed genre default.
    """
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "world_spell_catalog",
            "op": "loaded",
            "world_slug": world_slug,
            "spell_count": spell_count,
            "source": str(source),
        },
        component="genre",
    )


def _load_single_world(
    world_path: Path,
    genre_tropes: list[TropeDefinition],
    genre_root: Path,
    *,
    genre_theme: GenreTheme | None = None,
    valid_act_ids: frozenset[str] = frozenset(),
    mutations: MutationCatalog | None = None,
) -> World | None:
    """Load a single world from its directory.

    Port of Rust load_single_world(). Every world is a leaf — carries
    cartography.yaml + openings.yaml at world level. Worlds with multiple
    dungeon-style regions (e.g. caverns_sunden) author them as additional
    regions in the world's cartography.

    Args:
        world_path: Path to the world directory (e.g. ``.../worlds/coyote_star``).
        genre_tropes: Genre-tier tropes used for inheritance resolution.
        genre_root: Path to the genre pack root (e.g. ``.../space_opera``).
            Used to locate the genre-tier ``magic.yaml`` so the magic loader
            can compose genre+world layers — both files are required by
            ``load_world_magic`` (see ``magic_loader.py``).
        genre_theme: Genre-tier theme passed as a fallback when the world
            authors no ``worlds/<slug>/theme.yaml`` (epic 74). ``None`` when the
            genre pack ships no theme (mechanics-only pack). The effective theme
            is the world's own ``theme.yaml`` if present, else ``genre_theme``;
            when both are absent the pack-level invariant in ``load_genre_pack``
            raises ``GenreLoadError`` naming the world (No Silent Fallbacks).

    Returns:
        A fully assembled World, or None if the world's world.yaml declares
        ``draft: true`` (draft worlds are silently skipped at pack load time).

    Raises:
        GenreLoadError: If required files are missing or malformed.
    """
    config: WorldConfig = _load_yaml(world_path / "world.yaml", WorldConfig)
    if config.draft:
        return None
    lore: WorldLore = _load_yaml(world_path / "lore.yaml", WorldLore)
    # Epic 74 (story 74-3): lore is world-only and authoritative — genre lore is
    # no longer seeded. A world whose lore.yaml carries no SEEDABLE content
    # (history/geography/cosmology/factions all empty — e.g. prose stranded under
    # non-seedable extra keys) would leave the narrator's LoreStore empty. Fail
    # loud at load, naming the world (No Silent Fallbacks; mirrors the
    # visibility_baseline / lethality_policy required-surface guards). Emit a
    # state_transition span so the GM panel can prove the world-tier lore load
    # fired.
    _lore_fragment_count = _require_seedable_world_lore(lore, world_path)
    _emit_world_lore_loaded(
        world_slug=world_path.name, source=world_path, fragment_count=_lore_fragment_count
    )

    cartography: CartographyConfig = _load_cartography(world_path / "cartography.yaml")

    cultures_dir = world_path / "cultures"
    if cultures_dir.is_dir():
        cultures: list[Culture] = []
        for f in sorted(cultures_dir.glob("*.yaml")):
            if f.name == ".gitkeep":
                continue
            raw = _load_yaml_raw(f)
            # Skip art-pipeline visual-token overlays (have visual_tokens, not name).
            # These live in cultures/ for the daemon image pipeline and are not
            # name-generation Culture objects.
            if not isinstance(raw, dict) or "name" not in raw:
                continue
            cultures.append(Culture.model_validate(raw))
    else:
        cultures_raw = _load_yaml_raw_optional(world_path / "cultures.yaml")
        cultures = (
            [Culture.model_validate(c) for c in cultures_raw]
            if isinstance(cultures_raw, list)
            else []
        )

    # Legends: accept either Vec<Legend> (low_fantasy) or map with "legends" key (road_warrior).
    legends_dir = world_path / "legends"
    if legends_dir.is_dir():
        legends_files = sorted(legends_dir.glob("*.yaml"))
        legends_files = [
            f for f in legends_files if f.name != "_meta.yaml" and f.name != ".gitkeep"
        ]
        legends: list[Legend] = [Legend.model_validate(_load_yaml_raw(f)) for f in legends_files]
        meta_path = legends_dir / "_meta.yaml"
        legends_raw: Any = _load_yaml_raw(meta_path) if meta_path.exists() else None
    else:
        legends, legends_raw = _load_legends_flexible(world_path / "legends.yaml")

    # Load world tropes and resolve inheritance from genre-level tropes
    world_tropes_raw = _load_yaml_raw_optional(world_path / "tropes.yaml")
    raw_world_tropes: list[TropeDefinition] = (
        [TropeDefinition.model_validate(t) for t in world_tropes_raw]
        if isinstance(world_tropes_raw, list)
        else []
    )
    tropes = resolve_trope_inheritance(genre_tropes, raw_world_tropes) if raw_world_tropes else []

    # Optional world-level overrides
    archetypes_raw = _load_yaml_raw_optional(world_path / "archetypes.yaml")
    archetypes: list[NpcArchetype] = (
        [NpcArchetype.model_validate(a) for a in archetypes_raw]
        if isinstance(archetypes_raw, list)
        else []
    )

    visual_style: Any = _load_yaml_raw_optional(world_path / "visual_style.yaml")
    history: Any = _load_yaml_raw_optional(world_path / "history.yaml")

    # === World-tier flavor — theme / audio / visual_style (epic 74) ===
    # The genre tier is mechanics-only; flavor is world-authoritative. Load each
    # surface the world authors; emit a state_transition span per load so the GM
    # panel can prove world-tier flavor actually fired (mirrors _load_world_items
    # and the genre_pack span). Loaded RAW like visual_style: world flavor files
    # are free-form hard-overrides (e.g. five_points/audio.yaml), not the strict
    # genre schemas. Absence yields None — no silent fabrication.
    world_theme: Any = _load_yaml_raw_optional(world_path / "theme.yaml")
    world_audio: Any = _load_yaml_raw_optional(world_path / "audio.yaml")

    # Shape guard (story 74-4): theme/audio are loaded RAW, so a non-mapping
    # document (a YAML list or bare scalar) would otherwise flow into World(...)
    # and surface an OPAQUE pydantic ValidationError that names neither the file
    # nor the world. Fail loud and world-scoped here instead — mirroring the
    # fail-loud shape guard in _parse_char_creation_scenes. Absence (None) stays
    # valid: the genre tier is the transitional fallback (No Silent Fallbacks
    # applies to wrong SHAPE, not to a legitimately-absent override).
    for _flavor_file, _flavor_value in (("theme.yaml", world_theme), ("audio.yaml", world_audio)):
        if _flavor_value is not None and not isinstance(_flavor_value, dict):
            raise GenreLoadError(
                path=world_path / _flavor_file,
                detail=(
                    f"expected a mapping (world {_flavor_file} is loaded as a dict), "
                    f"got {type(_flavor_value).__name__}"
                ),
            )

    for field_name, value in (
        ("world_theme", world_theme),
        ("world_audio", world_audio),
        ("world_visual_style", visual_style),
    ):
        if value is not None:
            _emit_world_flavor_loaded(field_name, world_slug=world_path.name, source=world_path)

    # Theme is world-authoritative; the genre theme is a fallback only during the
    # transitional refactor (story 74-1) while live packs still ship genre flavor.
    # The two branches yield DIFFERENT runtime types — a raw ``dict`` (world tier)
    # or a ``GenreTheme`` (genre fallback). The union annotation is deliberate so
    # consumers branch on the type rather than assume one shape (World.theme docs).
    # ``effective_theme`` is None only when NEITHER tier supplies one — the
    # loud-fail for that lives in ``load_genre_pack`` (pack-level invariant: every
    # world in a real pack must resolve a theme), so direct ``_load_single_world``
    # callers building themeless fixtures aren't forced to author a theme.
    effective_theme: GenreTheme | dict[str, Any] | None = (
        world_theme if world_theme is not None else genre_theme
    )

    archetype_funnels: ArchetypeFunnels | None = _load_yaml_optional(
        world_path / "archetype_funnels.yaml", ArchetypeFunnels
    )

    # === World-tier openings.yaml — MANDATORY ===
    # The unified Opening schema. Both solo and MP entries live here,
    # distinguished by triggers.mode. Replaces both the old genre-tier
    # fallback path and the per-world side file that previously held
    # MP-only openings.
    openings: list[Opening] = _load_openings(
        world_path / "openings.yaml",
        scope=f"worlds/{world_path.name}",
        missing_detail=(
            f"World {world_path.name!r} is missing required openings.yaml. "
            "World-tier openings became mandatory in the canned-openings story; "
            "every world must author at least one solo and one MP opening. "
            "See docs/superpowers/specs/2026-05-01-canned-openings-design.md §1."
        ),
    )

    # === World-tier npcs.yaml — OPTIONAL ===
    # AuthoredNpc list. If a chassis_instance references crew_npcs from
    # this list, validator 4 (Phase 2) catches missing references.
    npcs_path = world_path / "npcs.yaml"
    authored_npcs: list[AuthoredNpc] = []
    if npcs_path.exists():
        npcs_raw = _load_yaml_raw(npcs_path)
        npcs_list_raw = npcs_raw.get("npcs", []) if isinstance(npcs_raw, dict) else []
        authored_npcs = [AuthoredNpc.model_validate(n) for n in npcs_list_raw]

    char_creation_path = world_path / "char_creation.yaml"
    char_creation: list[CharCreationScene] = _parse_char_creation_scenes(
        _load_yaml_raw_optional(char_creation_path), path=char_creation_path
    )

    # === World-tier rigs.yaml — OPTIONAL ===
    # Chassis instances. Required for cross-file validation of
    # chassis-anchored openings (validators 2 + 3, canned-openings §1.4).
    # Worlds that don't use the rig framework simply omit this file;
    # any chassis-anchored openings will then fail validator 2.
    rigs_path = world_path / "rigs.yaml"
    chassis_instances: list[ChassisInstanceConfig] = []
    if rigs_path.exists():
        rigs_raw = _load_yaml_raw(rigs_path)
        rigs_cfg = RigsWorldConfig.model_validate(rigs_raw)
        chassis_instances = list(rigs_cfg.chassis_instances)

    # Cross-file validators run on the world's own openings list.
    _validate_opening_setting_references(openings, chassis_instances, world_slug=world_path.name)
    _validate_crew_npc_references(chassis_instances, authored_npcs, world_slug=world_path.name)
    _validate_authored_npc_uniqueness(authored_npcs, world_slug=world_path.name)
    _validate_present_npcs_resolve(openings, authored_npcs, world_slug=world_path.name)
    _validate_opening_region_bindings(openings, cartography, world_slug=world_path.name)

    # Validators 7 + 8 (opening bank coverage). Derive chargen backgrounds
    # from the canonical "background" scene in char_creation.yaml. Worlds
    # whose chargen uses a different scene id (e.g. coyote_star uses
    # "origins") fall through to []; that disables Validator 8 for those
    # worlds but Validator 7 still enforces solo+MP.
    background_scene = next(
        (s for s in char_creation if s.id == "background"),
        None,
    )
    chargen_backgrounds: list[str] = (
        [c.label for c in background_scene.choices] if background_scene else []
    )
    _validate_opening_bank_coverage(openings, chargen_backgrounds, world_slug=world_path.name)

    # === World-tier magic.yaml — OPTIONAL (silent-skip when absent) ===
    # The magic_loader requires BOTH genre-tier and world-tier magic.yaml.
    # Genres without a magic system simply omit the files; that's a deliberate
    # authoring choice (matches sidequest/server/magic_init.py behavior).
    # Any other failure (malformed yaml, schema error) propagates as LoaderError
    # — no silent fallbacks per project rule.
    genre_magic_path = genre_root / "magic.yaml"
    world_magic_path = world_path / "magic.yaml"
    magic_register = ""
    if genre_magic_path.exists() and world_magic_path.exists():
        from sidequest.genre.magic_loader import load_world_magic

        magic_cfg = load_world_magic(genre_yaml=genre_magic_path, world_yaml=world_magic_path)
        magic_register = magic_cfg.narrator_register or ""

    portrait_manifest = _load_portrait_manifest(world_path / "portrait_manifest.yaml")

    # === World-tier items.yaml — OPTIONAL ===
    # Surfaces named_items / modifier_items / reliquaries / crimson_remnants /
    # consumable_items to the narrator and downstream subsystems (Cleric
    # divine_favor wiring reads reliquaries[].divine_favor_effect at
    # >= 0.7). See docs/design/magic-plugins/item_legacy_v1.md and
    # docs/research/items-as-confrontation-modifiers.md.
    items = _load_world_items(world_path / "items.yaml", world_slug=world_path.name)

    # === World-tier bestiary.yaml — OPTIONAL (genre/world repoint) ===
    # "Genre is rulebook only, world owns cast/catalog": creature rosters moved
    # to the world tier. Ruleset-module packs (wwn/cwn/swn/awn) author their
    # hostiles here; the world set REPLACES the genre-tier pool via
    # GenrePack.effective_bestiary (world-over-genre, like cultures/archetypes).
    # Absent file → None (the genre-tier bestiary serves; encountergen fails loud
    # only when NEITHER tier supplies one for a ruleset-module pack).
    world_bestiary = _load_yaml_optional(world_path / "bestiary.yaml", Bestiary)

    # ADR-079: optional world-level theme override (worlds/<slug>/client_theme.css).
    # When present, this CSS replaces the genre-level theme at connect time.
    client_theme_css = _load_text_optional(world_path / "client_theme.css")

    # ADR-053 / Story 71-32: world-tier scenarios (worlds/<slug>/scenarios/).
    # Each world owns its scenarios; bind_scenario binds only the active world's.
    # Absent dir → {} (no silent fallback to pack-level GenrePack.scenarios).
    world_scenarios: dict[str, ScenarioPack] = _load_subdirectories(
        world_path, "scenarios", _load_single_scenario
    )

    # === World-tier premises.yaml — OPTIONAL (spec 2026-06-02) ===
    # The world's political illusions and the blocs that prop them. Absent file
    # → no political layer (a valid authoring choice, NOT a fallback). When
    # present, cross-references are validated fail-loud against this world's
    # authored NPCs and the genre-tier witnessed-act vocabulary.
    premises_file = _load_yaml_optional(world_path / "premises.yaml", PremisesFile)
    world_premises = list(premises_file.premises) if premises_file is not None else []
    world_blocs = list(premises_file.blocs) if premises_file is not None else []
    if premises_file is not None:
        validate_premises(
            premises=world_premises,
            blocs=world_blocs,
            authored_npc_ids={npc.id for npc in authored_npcs},
            valid_act_ids=valid_act_ids,
            world_slug=world_path.name,
        )

    # === World-tier chassis_classes.yaml — OPTIONAL (epic 94) ===
    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # chassis classes are a world-tier CAST/CATALOG surface — the cast of rigs a
    # world ships — not a genre mechanic. The genre tier is the rulebook only.
    # Worlds that don't use the rig framework omit the file → None (distinguishes
    # "world has no rigs" from "empty config"; no silent fallback to a genre
    # default). Station cross-validation runs at load (same as the old genre-tier
    # path) so a malformed chassis fails loud, world-scoped.
    chassis_classes: ChassisClassesConfig | None = _load_yaml_optional(
        world_path / "chassis_classes.yaml", ChassisClassesConfig
    )
    if chassis_classes is not None:
        from sidequest.interior.loader import validate_chassis_stations

        for cc in chassis_classes.classes:
            validate_chassis_stations(cc)
        _emit_world_chassis_classes_loaded(
            world_slug=world_path.name,
            source=world_path / "chassis_classes.yaml",
            class_count=len(chassis_classes.classes),
        )

    # === World-tier seed_tropes.yaml — OPTIONAL (epic 94) ===
    # Genre/world boundary correction: the seed-trope deck is world-tier CAST/
    # CATALOG (the seeds a world plants), not a genre mechanic. Absent file →
    # empty list (no silent fallback to a shared default deck — per "No Silent
    # Fallbacks"; missing file is fine, the field reflects reality).
    seed_tropes_raw = _load_yaml_raw_optional(world_path / "seed_tropes.yaml")
    world_seed_tropes: list[SeedTrope] = (
        [SeedTrope.model_validate(s) for s in seed_tropes_raw]
        if isinstance(seed_tropes_raw, list)
        else []
    )
    if world_seed_tropes:
        _emit_world_seed_tropes_loaded(
            world_slug=world_path.name,
            source=world_path / "seed_tropes.yaml",
            seed_count=len(world_seed_tropes),
        )

    # === World-tier classes.yaml — OPTIONAL (epic 94) ===
    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # a world's classes/callings are a world-tier CAST/CATALOG surface (C&C kits,
    # Victoria callings), not a genre mechanic — the genre tier is the rulebook
    # only. Absent file → empty list (a world may be axis-archetype-only). A
    # malformed file still fails loud, world-scoped (no silent fallback). The
    # genre-tier ``classes_list`` remains the shared default for packs that have
    # not migrated classes down.
    world_classes_path = world_path / "classes.yaml"
    world_classes: list[ClassDef] = []
    if world_classes_path.exists():
        raw_world_classes = _load_yaml_raw_optional(world_classes_path)
        if raw_world_classes is not None and not isinstance(raw_world_classes, list):
            raise GenreLoadError(
                path=world_classes_path,
                detail="expected a list of class definitions",
            )
        world_classes = [
            ClassDef.model_validate(item)
            for item in (raw_world_classes if isinstance(raw_world_classes, list) else [])
        ]
    if world_classes:
        _emit_world_classes_loaded(
            world_slug=world_path.name,
            source=world_classes_path,
            class_count=len(world_classes),
        )

    # === World-tier spells_wwn.yaml — OPTIONAL (epic 94) ===
    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # a world's WWN spell catalog is a world-tier CAST/CATALOG surface — the
    # catalog of magic a world ships — NOT a genre mechanic. The genre tier is
    # the rulebook only (resolution rules + the WWN magic block on ``rules.wwn``).
    # Absent file → None (a valid choice for a pack that keeps a shared catalog at
    # the genre tier). A malformed file fails loud, world-scoped (no silent
    # fallback). The genre-tier catalog remains the shared default; the
    # caster-without-catalog and starting_prepared fail-loud invariants are
    # enforced at pack level where the ruleset and class roster are both in hand.
    world_spell_catalog_path = world_path / "spells_wwn.yaml"
    world_spell_catalog: WwnSpellCatalog | None = None
    if world_spell_catalog_path.exists():
        from sidequest.genre.models.wwn_spell import load_wwn_spell_catalog as _load_catalog

        try:
            world_spell_catalog = _load_catalog(world_spell_catalog_path)
        except Exception as exc:
            raise GenreLoadError(path=world_spell_catalog_path, detail=str(exc)) from exc
        _emit_world_spell_catalog_loaded(
            world_slug=world_path.name,
            source=world_spell_catalog_path,
            spell_count=len(world_spell_catalog.spells),
        )

    # === World-tier disciplines_psionic.yaml — OPTIONAL (Story 102-6) ===
    # A world's psionic discipline catalog is a world-tier CAST/CATALOG surface
    # (the disciplines a world ships, ADR-140 "Crunch in the Genre"), NOT a genre
    # mechanic. Absent file → None (a valid choice — psionics is optional content,
    # and a pack may keep a shared catalog at the genre tier). A malformed file
    # fails loud, world-scoped (dup-id / unknown-field / non-safe-load via the
    # catalog model). Consumers resolve world-first via
    # ``server.dispatch.psionic_discipline_resolve.resolve_psionic_discipline_catalog``.
    world_psionic_catalog_path = world_path / "disciplines_psionic.yaml"
    world_psionic_catalog: PsionicDisciplineCatalog | None = None
    if world_psionic_catalog_path.exists():
        from sidequest.genre.models.psionics import (
            load_psionic_discipline_catalog as _load_disc,
        )

        try:
            world_psionic_catalog = _load_disc(world_psionic_catalog_path)
        except Exception as exc:
            raise GenreLoadError(path=world_psionic_catalog_path, detail=str(exc)) from exc

    # === World-tier inventory.yaml — OPTIONAL (epic 94) ===
    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # a world's item catalog, class starting-kits, gold, and currency are a
    # world-tier CAST/CATALOG surface — the loot a world ships — NOT a genre
    # mechanic. The genre tier is the rulebook only. Absent file → None (a valid
    # choice for a pack that keeps a shared catalog at the genre tier, e.g.
    # caverns_and_claudes). A malformed file fails loud, world-scoped (no silent
    # fallback). The genre-tier ``GenrePack.inventory`` remains the shared default
    # for packs that have not migrated the catalog down; consumers resolve
    # world-first via ``server.dispatch.inventory_resolve.resolve_inventory``.
    world_inventory: InventoryConfig | None = _load_yaml_optional(
        world_path / "inventory.yaml", InventoryConfig
    )
    if world_inventory is not None:
        _emit_world_inventory_loaded(
            world_slug=world_path.name,
            source=world_path / "inventory.yaml",
            catalog_count=len(world_inventory.item_catalog),
            class_kit_count=len(world_inventory.starting_equipment),
        )

    # === World-tier Saint canon (worlds/<slug>/saints.yaml, story 103-1) ===
    # Curated presets over the genre mutation catalog. Absence = the world
    # ships no Saints (valid authored choice). Presence REQUIRES the genre
    # mutation catalog — there is nothing else to validate bundle/drawback
    # ids against — and every id must resolve, loudly (No Silent Fallbacks).
    saints_path = world_path / "saints.yaml"
    world_saints: SaintRegistry | None = None
    if saints_path.is_file():
        if mutations is None:
            raise GenreLoadError(
                path=saints_path,
                detail=(
                    f"World {world_path.name!r} authors saints.yaml but the pack has "
                    "no mutations.yaml catalog — Saints are curated bundles of genre "
                    "mutation ids and cannot be validated without one"
                ),
            )
        try:
            world_saints = load_saint_registry(saints_path, mutations)
        except ValueError as e:
            # pydantic ValidationError subclasses ValueError — both shapes land
            # here. Re-raise as GenreLoadError so pack load failures carry the
            # file path; the detail keeps the saint id + offending mutation id.
            raise GenreLoadError(path=saints_path, detail=str(e)) from e

    # === World-tier stock roster (worlds/<slug>/stocks.yaml, story 103-2) ===
    # Chargen entry-path trait sets over the genre mutation catalog. Absence =
    # single-path chargen (valid authored choice — the flickering_reach shape).
    # Presence REQUIRES the genre mutation catalog to validate granted ids
    # against, and every id must resolve, loudly (No Silent Fallbacks).
    stocks_path = world_path / "stocks.yaml"
    world_stocks: StockRegistry | None = None
    if stocks_path.is_file():
        if mutations is None:
            raise GenreLoadError(
                path=stocks_path,
                detail=(
                    f"World {world_path.name!r} authors stocks.yaml but the pack has "
                    "no mutations.yaml catalog — stock trait sets grant genre "
                    "mutation ids and cannot be validated without one"
                ),
            )
        try:
            world_stocks = load_stock_registry(stocks_path, mutations)
        except ValueError as e:
            # pydantic ValidationError subclasses ValueError — both shapes land
            # here; the detail keeps the stock id + offending mutation id.
            raise GenreLoadError(path=stocks_path, detail=str(e)) from e

    return World(
        config=config,
        lore=lore,
        legends=legends,
        cartography=cartography,
        cultures=cultures,
        tropes=tropes,
        archetypes=archetypes,
        visual_style=visual_style,
        theme=effective_theme,
        audio=world_audio,
        history=history,
        legends_raw=legends_raw,
        portrait_manifest=portrait_manifest,
        archetype_funnels=archetype_funnels,
        openings=openings,
        authored_npcs=authored_npcs,
        char_creation=char_creation,
        classes=world_classes,
        wwn_spell_catalog=world_spell_catalog,
        psionic_discipline_catalog=world_psionic_catalog,
        chassis_instances=chassis_instances,
        chassis_classes=chassis_classes,
        seed_tropes=world_seed_tropes,
        magic_register=magic_register,
        items=items,
        inventory=world_inventory,
        bestiary=world_bestiary,
        saints=world_saints,
        stocks=world_stocks,
        scenarios=world_scenarios,
        premises=world_premises,
        blocs=world_blocs,
        client_theme_css=client_theme_css,
    )


# ---------------------------------------------------------------------------
# Scenario loader
# ---------------------------------------------------------------------------


def _load_single_scenario(scenario_path: Path) -> ScenarioPack:
    """Load a single scenario from its directory.

    Port of Rust load_single_scenario().
    """
    scenario: ScenarioPack = _load_yaml(scenario_path / "scenario.yaml", ScenarioPack)

    # Overlay supplementary files
    matrix_raw = _load_yaml_raw_optional(scenario_path / "assignment_matrix.yaml")
    if matrix_raw is not None:
        from sidequest.genre.models.scenario import AssignmentMatrix

        scenario = scenario.model_copy(
            update={"assignment_matrix": AssignmentMatrix.model_validate(matrix_raw)}
        )

    graph_raw = _load_yaml_raw_optional(scenario_path / "clue_graph.yaml")
    if graph_raw is not None:
        from sidequest.genre.models.scenario import ClueGraph

        scenario = scenario.model_copy(update={"clue_graph": ClueGraph.model_validate(graph_raw)})

    atmo_raw = _load_yaml_raw_optional(scenario_path / "atmosphere_matrix.yaml")
    if atmo_raw is not None:
        from sidequest.genre.models.scenario import AtmosphereMatrix

        scenario = scenario.model_copy(
            update={"atmosphere_matrix": AtmosphereMatrix.model_validate(atmo_raw)}
        )

    npcs_raw = _load_yaml_raw_optional(scenario_path / "npcs.yaml")
    if npcs_raw is not None and isinstance(npcs_raw, list):
        npcs = [ScenarioNpc.model_validate(n) for n in npcs_raw]
        scenario = scenario.model_copy(update={"npcs": npcs})

    return scenario


# ---------------------------------------------------------------------------
# Top-level pack loader
# ---------------------------------------------------------------------------


def load_genre_pack(path: Path | str) -> GenrePack:
    """Load a complete genre pack from a directory.

    Reads all YAML files, loads worlds and scenarios, resolves trope inheritance,
    and returns a fully assembled GenrePack.

    Port of Rust load_genre_pack(path).

    Args:
        path: Path to the genre pack directory (e.g. `.../genre_packs/caverns_and_claudes`).

    Returns:
        A fully assembled GenrePack.

    Raises:
        GenreLoadError: If any required file is missing, unreadable, or malformed.
    """
    path = Path(path)

    if not path.exists() or not path.is_dir():
        raise GenreLoadError(
            path=path,
            detail="directory does not exist",
        )

    # Load required mechanics files
    meta = _load_yaml(path / "pack.yaml", PackMeta)
    rules = _load_rules_config(path / "rules.yaml", path)
    # Epic 74 — genre tier is mechanics-only. Flavor (lore/theme/archetypes/
    # cultures/audio/visual_style) becomes OPTIONAL at the genre tier; the world
    # tier is authoritative. Live packs still ship these files until the
    # per-world migration, so absence is tolerated, not assumed. No silent
    # fallback: a malformed file still raises (the *_optional helpers raise on
    # parse/schema error, only absence yields None).
    lore = _load_yaml_optional(path / "lore.yaml", Lore)
    theme = _load_yaml_optional(path / "theme.yaml", GenreTheme)
    archetypes_raw = _load_yaml_raw_optional(path / "archetypes.yaml")
    archetypes: list[NpcArchetype] = (
        [NpcArchetype.model_validate(a) for a in archetypes_raw]
        if isinstance(archetypes_raw, list)
        else []
    )
    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # char_creation is a world-tier CAST/CATALOG surface, not a genre mechanic.
    # The genre tier MAY ship a shared default, but absence is valid — the world
    # tier is authoritative. Absent → []; a malformed file still raises (the
    # *_optional helper only returns None on absence). The per-world resolution
    # invariant below fails loud if a world resolves no chargen from either tier.
    char_creation_path = path / "char_creation.yaml"
    char_creation: list[CharCreationScene] = _parse_char_creation_scenes(
        _load_yaml_raw_optional(char_creation_path), path=char_creation_path
    )
    # Pack-level visual_style is optional (2026-05-29 directive — visual
    # prompts live at world level). Absent → None; the daemon resolves style
    # from the world scope and fails loud if neither scope supplies it.
    visual_style = _load_yaml_optional(path / "visual_style.yaml", VisualStyle)
    progression = _load_yaml(path / "progression.yaml", ProgressionConfig)
    axes = _load_yaml(path / "axes.yaml", AxesConfig)
    # Epic 74 — genre audio optional / world-authoritative.
    audio = _load_yaml_optional(path / "audio.yaml", AudioConfig)
    if audio is not None:
        _resolve_audio_urls(audio, genre_slug=path.name)
    cultures_raw = _load_yaml_raw_optional(path / "cultures.yaml")
    cultures: list[Culture] = (
        [Culture.model_validate(c) for c in cultures_raw] if isinstance(cultures_raw, list) else []
    )
    prompts = _load_yaml(path / "prompts.yaml", Prompts)

    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # tropes are a world-tier CAST surface, not a genre mechanic — a genre's
    # tropes authored for one world are wrong for its siblings (the
    # the_real_mccoy vs dust_and_lead problem). The genre tier MAY ship tropes
    # to serve as an inheritance base; absence is valid (mechanics-only genre).
    # Absent → []; a malformed file still raises. World tropes are authoritative
    # (each world ships its own tropes.yaml per the world required-file contract).
    genre_tropes_raw = _load_yaml_raw_optional(path / "tropes.yaml")
    genre_tropes: list[TropeDefinition] = (
        [TropeDefinition.model_validate(t) for t in genre_tropes_raw]
        if isinstance(genre_tropes_raw, list)
        else []
    )

    # Epic 22 — optional seed trope deck. File is per-pack, not per-world
    # (Phase 1 dogfoods tea_and_murder only; other packs ship without
    # the file and get an empty list — no silent fallback to a shared
    # default deck).
    seed_tropes_raw = _load_yaml_raw_optional(path / "seed_tropes.yaml")
    seed_tropes: list[SeedTrope] = (
        [SeedTrope.model_validate(s) for s in seed_tropes_raw]
        if isinstance(seed_tropes_raw, list)
        else []
    )

    # Load optional files
    achievements_raw = _load_yaml_raw_optional(path / "achievements.yaml")
    achievements: list[Achievement] = (
        [Achievement.model_validate(a) for a in achievements_raw]
        if isinstance(achievements_raw, list)
        else []
    )

    power_tiers_raw = _load_yaml_raw_optional(path / "power_tiers.yaml")
    power_tiers: dict[str, list[PowerTier]] = {}
    if isinstance(power_tiers_raw, dict):
        for k, v in power_tiers_raw.items():
            if isinstance(v, list):
                power_tiers[k] = [PowerTier.model_validate(pt) for pt in v]

    beat_vocabulary: BeatVocabulary | None = _load_yaml_optional(
        path / "beat_vocabulary.yaml", BeatVocabulary
    )
    chassis_classes: ChassisClassesConfig | None = _load_yaml_optional(
        path / "chassis_classes.yaml", ChassisClassesConfig
    )
    if chassis_classes is not None:
        from sidequest.interior.loader import validate_chassis_stations

        for cc in chassis_classes.classes:
            validate_chassis_stations(cc)
    drama_thresholds: DramaThresholds | None = _load_yaml_optional(
        path / "pacing.yaml", DramaThresholds
    )
    inventory: InventoryConfig | None = _load_yaml_optional(
        path / "inventory.yaml", InventoryConfig
    )

    # Genre-tier openings.yaml is dead. Per the canned-openings design
    # (§1, locked decision #2), all openings now live at the world tier
    # (worlds/{slug}/openings.yaml), and the genre-tier file is deleted.
    # The GenrePack.openings field remains for the Opening type but is
    # always populated empty here; callers should read worlds[slug].openings.
    openings: list[Opening] = []

    backstory_tables: BackstoryTables | None = _load_yaml_optional(
        path / "backstory_tables.yaml", BackstoryTables
    )
    equipment_tables: EquipmentTables | None = _load_yaml_optional(
        path / "equipment_tables.yaml", EquipmentTables
    )

    classes_path = path / "classes.yaml"
    classes_list: list[ClassDef] = []
    if classes_path.exists():
        with classes_path.open("r", encoding="utf-8") as f:
            raw_classes = yaml.safe_load(f) or []
        if not isinstance(raw_classes, list):
            raise GenreLoadError(
                path=classes_path,
                detail="expected a list of class definitions",
            )
        classes_list = [ClassDef.model_validate(item) for item in raw_classes]

    archetype_constraints: ArchetypeConstraints | None = _load_yaml_optional(
        path / "archetype_constraints.yaml", ArchetypeConstraints
    )

    # Cross-reference validation: class_filter / encounter_beat_choices consistency.
    # Only enforced when a classes.yaml is present (classes_list is non-empty).
    _validate_class_filter_refs(rules, classes_list)

    # Task 8 — saving_throws required on every class when the pack ships spell catalogs.
    # Detection: spells/ directory adjacent to magic.yaml at genre pack root.
    # Packs without a spells/ dir (heavy_metal, tea_and_murder, …) are exempt.
    _validate_saving_throws_refs(
        classes_list,
        has_spell_catalogs=(path / "spells").is_dir(),
    )

    # WWN spell catalog — load spells_wwn.yaml when ruleset == "wwn".
    # Fail loud: a wwn pack that declares a caster class (magic_access == "wwn"
    # AND non-empty casts_per_day_by_level) but has no spells_wwn.yaml is an
    # authoring bug. No silent fallback.
    wwn_catalog = _load_wwn_spell_catalog(path, rules, classes_list)

    # Genre-tier psionic discipline catalog — load disciplines_psionic.yaml when
    # present (Story 102-6). OPTIONAL and uncoupled to classes (unlike WWN
    # spells): psionics is optional content, and the catalog may instead live at
    # the world tier. Absent → None (no silent fallback). A malformed file fails
    # loud via the catalog model (dup-id / unknown-field / non-safe-load).
    genre_psionic_catalog: PsionicDisciplineCatalog | None = None
    _genre_psionic_path = path / "disciplines_psionic.yaml"
    if _genre_psionic_path.exists():
        from sidequest.genre.models.psionics import (
            load_psionic_discipline_catalog as _load_disc,
        )

        try:
            genre_psionic_catalog = _load_disc(_genre_psionic_path)
        except Exception as exc:
            raise GenreLoadError(path=_genre_psionic_path, detail=str(exc)) from exc

    # Pack-root bestiary (story 90-1) — SRD-aligned combat stat blocks for
    # ruleset-module packs. Optional at load (synthetic fixtures and non-
    # encounter consumers don't need it); a malformed file still fails loud.
    # The REQUIRED-for-ruleset-module-packs contract is enforced at the
    # generation seam: encountergen exits loud when ruleset != native and
    # this is None, and pregen.seed_manual raises rather than silently
    # seeding an empty encounters pool.
    bestiary = _load_yaml_optional(path / "bestiary.yaml", Bestiary)

    # Fail loud: every starting_prepared spell id on every class must resolve
    # against the loaded catalog.  Unknown ids are authoring bugs.
    _validate_wwn_starting_prepared_refs(classes_list, wwn_catalog)

    # Base archetypes and npc_traits live at content root (parent of genre_packs/)
    content_root: Path | None = None
    parent = path.parent  # genre_packs/
    if parent.exists():
        grandparent = parent.parent  # content root
        if grandparent.exists():
            content_root = grandparent

    base_archetypes: BaseArchetypes | None = None
    npc_traits: NpcTraitsDatabase | None = None
    if content_root is not None:
        base_archetypes = _load_yaml_optional(content_root / "archetypes_base.yaml", BaseArchetypes)
        npc_traits = _load_yaml_optional(content_root / "npc_traits.yaml", NpcTraitsDatabase)

    # Genre-tier witnessed-act vocabulary (spec 2026-06-02). OPTIONAL — packs
    # without a political layer omit the file. World premises/blocs validate
    # their act bindings against these ids.
    witnessed_acts_file = _load_yaml_optional(path / "witnessed_acts.yaml", WitnessedActsFile)
    genre_witnessed_acts = (
        list(witnessed_acts_file.witnessed_acts) if witnessed_acts_file is not None else []
    )
    valid_act_ids = frozenset(a.id for a in genre_witnessed_acts)

    # === Genre-tier mutations.yaml — OPTIONAL (silent-skip when absent) ===
    # Packs without a mutation system simply omit the file; that's a deliberate
    # authoring choice (mirrors the magic.yaml pattern above). A present-but-
    # invalid file still fails loud via ValidationError. Loaded BEFORE the
    # worlds so each world's saints.yaml (story 103-1) can cross-validate its
    # bundle/drawback ids against the catalog at load time.
    mutations_path = path / "mutations.yaml"
    mutations = load_mutation_catalog(mutations_path) if mutations_path.is_file() else None

    # Load worlds and scenarios from subdirectories.
    # _load_single_world returns None for worlds with draft: true — filter them out.
    worlds_raw: dict[str, World | None] = _load_subdirectories(
        path,
        "worlds",
        lambda p: _load_single_world(
            p,
            genre_tropes,
            path,
            genre_theme=theme,
            valid_act_ids=valid_act_ids,
            mutations=mutations,
        ),
    )
    worlds: dict[str, World] = {slug: w for slug, w in worlds_raw.items() if w is not None}

    # Epic 74 — theme is world-authoritative and required. Every world must
    # resolve a theme from its own tier or the genre fallback; a world that
    # resolves none fails loud, named (No Silent Fallbacks). A themeless client
    # (connect-time + reference-chrome) is broken. This pack-level check lets
    # direct ``_load_single_world`` unit fixtures stay themeless while real packs
    # enforce the invariant.
    for slug, w in worlds.items():
        if w.theme is None:
            raise GenreLoadError(
                path=path / "worlds" / slug / "theme.yaml",
                detail=(
                    f"World {slug!r} resolves no theme — neither worlds/{slug}/theme.yaml "
                    f"nor the genre theme.yaml is present. Theme is world-authoritative "
                    "(epic 74); every world must supply or inherit a theme."
                ),
            )

    # Genre/world boundary correction: char_creation is world-authoritative with
    # an optional genre default (see the genre-tier load above). Now that the
    # genre file is optional, guard against a world that resolves NO chargen from
    # either tier — a world you cannot build a character in is broken, not empty.
    # Mirrors the theme invariant; fails loud, named (No Silent Fallbacks).
    for slug, w in worlds.items():
        if not w.char_creation and not char_creation:
            raise GenreLoadError(
                path=path / "worlds" / slug / "char_creation.yaml",
                detail=(
                    f"World {slug!r} resolves no character creation — neither "
                    f"worlds/{slug}/char_creation.yaml nor the genre char_creation.yaml "
                    "is present. char_creation is world-authoritative with an optional "
                    "genre default; every world must supply or inherit one."
                ),
            )

    # === Pack-level class roster — world-first aggregation (epic 94) ===
    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # classes/callings are a world-tier CAST/CATALOG surface. When the genre tier
    # ships no classes.yaml (tea_and_murder, which moved its callings down to
    # blackthorn_moor/glenross), the pack-level ``GenrePack.classes`` roster is
    # the union of every world's classes — that roster is what the chargen
    # builder, confrontation, dice, and views consumers read to resolve a
    # ``char_class`` → ClassDef. Worlds that share a calling (identical id) must
    # agree on its definition; a genuine divergence fails loud rather than
    # silently picking one (No Silent Fallbacks). When the genre tier DOES ship
    # classes (space_opera, heavy_metal, C&C), that genre roster is authoritative
    # and worlds are not aggregated up — the genre default is intentional.
    if not classes_list:
        aggregated_classes: dict[str, ClassDef] = {}
        for slug, w in worlds.items():
            for cls in w.classes:
                existing = aggregated_classes.get(cls.id)
                if existing is not None and existing != cls:
                    raise GenreLoadError(
                        path=path / "worlds" / slug / "classes.yaml",
                        detail=(
                            f"class id {cls.id!r} is defined differently across worlds "
                            f"in this pack — world-tier class rosters that share an id "
                            f"must agree on its definition (epic 94 genre/world "
                            f"boundary). Reconcile the divergent definitions or give "
                            f"them distinct ids."
                        ),
                    )
                aggregated_classes.setdefault(cls.id, cls)
        classes_list = list(aggregated_classes.values())

    # === Pack-level WWN spell catalog — world-first aggregation (epic 94) ===
    # Genre/world boundary correction (supersedes ADR-120 "mechanics-in-genre"):
    # the WWN spell catalog is a world-tier CAST/CATALOG surface. When the genre
    # tier ships no spells_wwn.yaml, the pack-level ``GenrePack.wwn_spell_catalog``
    # is the union of every world's catalog — that is what the cast pipeline
    # (``narration_apply._resolve_wwn_cast_for_beat``) and the long_rest reprepare
    # tool read to resolve a spell id → spell. Worlds that share a spell id must
    # agree on its definition; a genuine divergence fails loud (No Silent
    # Fallbacks). When the genre tier DOES ship a catalog (elemental_harmony keeps
    # one shared catalog for both worlds), that genre catalog is authoritative and
    # worlds are not aggregated up — the genre default is intentional.
    if wwn_catalog is None:
        from sidequest.genre.models.wwn_spell import WwnSpell

        aggregated_spells: dict[str, WwnSpell] = {}
        for slug, w in worlds.items():
            if w.wwn_spell_catalog is None:
                continue
            for spell in w.wwn_spell_catalog.spells:
                existing = aggregated_spells.get(spell.id)
                if existing is not None and existing != spell:
                    raise GenreLoadError(
                        path=path / "worlds" / slug / "spells_wwn.yaml",
                        detail=(
                            f"spell id {spell.id!r} is defined differently across worlds "
                            f"in this pack — world-tier spell catalogs that share an id "
                            f"must agree on its definition (epic 94 genre/world "
                            f"boundary). Reconcile the divergent definitions or give "
                            f"them distinct ids."
                        ),
                    )
                aggregated_spells.setdefault(spell.id, spell)
        if aggregated_spells:
            wwn_catalog = WwnSpellCatalog(
                version="aggregated", spells=list(aggregated_spells.values())
            )

    # Re-run the WWN fail-loud invariants against the world-first-resolved roster
    # and catalog. The genre-tier pass at load time only saw the genre roster /
    # catalog; for a pack that migrated classes + the catalog down to worlds, the
    # caster-without-catalog and starting_prepared checks must see the aggregated
    # values (epic 94). No-op for genre-authoritative packs (already validated).
    if rules.ruleset == "wwn":
        caster_classes = [
            c
            for c in classes_list
            if c.magic_access == "wwn"
            and c.wwn_magic is not None
            and bool(c.wwn_magic.casts_per_day_by_level)
        ]
        if caster_classes and wwn_catalog is None:
            raise GenreLoadError(
                path=path / "spells_wwn.yaml",
                detail=(
                    f"wwn pack has caster classes {[c.id for c in caster_classes]} but no "
                    "spells_wwn.yaml at the genre tier OR any world tier (epic 94). Author a "
                    "spell catalog or remove the casts_per_day_by_level entries."
                ),
            )
        _validate_wwn_starting_prepared_refs(classes_list, wwn_catalog)

    # === Pack-level chargen scenes — world-first aggregation (epic 94) ===
    # Same boundary correction for char_creation: when the genre tier ships no
    # char_creation.yaml (spaghetti_western, tea_and_murder — both moved chargen
    # down to the world tier), the pack-level ``GenrePack.char_creation`` is the
    # union of every world's scenes. Production reads chargen world-first via
    # ``resolve_char_creation_scenes``; this aggregate keeps pack-level
    # introspection (and the reputation_bonus / archetype-hint drift consumers)
    # honest about what the migrated pack actually offers. The genre default,
    # when present, stays authoritative (no aggregation).
    if not char_creation:
        aggregated_scenes: list[CharCreationScene] = []
        seen_scene_keys: set[tuple[str, int]] = set()
        for w in worlds.values():
            for idx, scene in enumerate(w.char_creation):
                key = (scene.id, idx)
                if key in seen_scene_keys:
                    continue
                seen_scene_keys.add(key)
                aggregated_scenes.append(scene)
        char_creation = aggregated_scenes

    scenarios: dict[str, ScenarioPack] = _load_subdirectories(
        path, "scenarios", _load_single_scenario
    )

    # Task 22: load optional projection.yaml.
    projection_yaml = path / "projection.yaml"
    projection_rules = None
    if projection_yaml.exists():
        from sidequest.game.projection.rules import load_rules_from_yaml_path
        from sidequest.game.projection.validator import validate_projection_rules

        projection_rules = load_rules_from_yaml_path(projection_yaml)
        validate_projection_rules(projection_rules)  # raises on error — no silent fallback

    # Group G Task 2: required visibility baseline — decomposer reads this at
    # session init. No silent fallback: missing file is a pack-authoring bug.
    from sidequest.genre.models.visibility import load_baseline

    visibility_baseline_path = path / "visibility_baseline.yaml"
    try:
        visibility_baseline = load_baseline(visibility_baseline_path)
    except FileNotFoundError as e:
        raise GenreLoadError(path=visibility_baseline_path, detail=str(e)) from e
    except Exception as e:
        raise GenreLoadError(path=visibility_baseline_path, detail=str(e)) from e

    # Group C Task 4: required lethality policy — LethalityArbiter reads this
    # at every turn. No silent fallback: missing file is a pack-authoring bug,
    # same pattern as visibility_baseline above.
    from sidequest.genre.lethality_policy_loader import (
        LethalityPolicyMissingError,
        load_lethality_policy,
    )

    try:
        lethality_policy = load_lethality_policy(path)
    except LethalityPolicyMissingError as e:
        raise GenreLoadError(path=path / "lethality_policy.yaml", detail=str(e)) from e
    except Exception as e:
        raise GenreLoadError(path=path / "lethality_policy.yaml", detail=str(e)) from e

    # ADR-079: genre-level theme CSS (client_theme.css at pack root).
    # Optional — packs in workshop without a theme yet simply omit it. When
    # absent, the UI keeps its pre-genre fallback (dark-mode shadcn defaults).
    client_theme_css = _load_text_optional(path / "client_theme.css")

    pack = GenrePack(
        meta=meta,
        rules=rules,
        lore=lore,
        theme=theme,
        archetypes=archetypes,
        witnessed_acts=genre_witnessed_acts,
        char_creation=char_creation,
        visual_style=visual_style,
        progression=progression,
        axes=axes,
        audio=audio,
        cultures=cultures,
        prompts=prompts,
        tropes=genre_tropes,
        seed_tropes=seed_tropes,
        beat_vocabulary=beat_vocabulary,
        chassis_classes=chassis_classes,
        achievements=achievements,
        power_tiers=power_tiers,
        worlds=worlds,
        scenarios=scenarios,
        drama_thresholds=drama_thresholds,
        inventory=inventory,
        openings=openings,
        backstory_tables=backstory_tables,
        equipment_tables=equipment_tables,
        classes=classes_list,
        base_archetypes=base_archetypes,
        archetype_constraints=archetype_constraints,
        npc_traits=npc_traits,
        projection_rules=projection_rules,
        visibility_baseline=visibility_baseline,
        lethality_policy=lethality_policy,
        wwn_spell_catalog=wwn_catalog,
        psionic_discipline_catalog=genre_psionic_catalog,
        bestiary=bestiary,
        mutations=mutations,
        source_dir=path,
        client_theme_css=client_theme_css,
    )

    # Fail loud at load if the pack names an unregistered ruleset (no silent default).
    from sidequest.game.ruleset import get_ruleset_module  # local import avoids a load-time cycle

    get_ruleset_module(pack.rules.ruleset)

    # Sprint 3 cold-subsystem audit: pack load was invisible to the GM
    # panel. A failed load raises GenreLoadError above (caught by callers,
    # which is its own dashboard-visible path) — this event covers the
    # success path so the panel can prove a pack actually loaded vs.
    # serving stale cache state.
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "genre_pack",
            "op": "loaded",
            "genre_slug": path.name,
            "world_count": len(worlds),
            "scenario_count": len(scenarios),
            "archetype_count": len(archetypes),
            "trope_count": len(genre_tropes),
            "source_dir": str(path),
        },
        component="genre",
    )

    # Story 50-13: apply this pack's disposition→attitude bands process-
    # wide. ``or DEFAULT`` overwrites any prior pack's custom band when
    # this pack opts out, so two sessions on different packs cannot
    # cross-contaminate NPC attitudes. Applied only here, on the fully-
    # assembled success path — a malformed block already failed loudly at
    # _load_rules_config (GenreLoadError) before reaching this line, so a
    # failed load never half-applies a partial config.
    configure_attitude_thresholds(rules.disposition_thresholds or DEFAULT_ATTITUDE_THRESHOLDS)

    return pack


# ---------------------------------------------------------------------------
# GenreLoader — multi-path search class
# ---------------------------------------------------------------------------


class GenreLoader:
    """Multi-path genre pack loader.

    Searches a list of directories in order for genre packs, loading the first
    match found. Supports the search order: local, home, install.

    Port of Rust GenreLoader struct (loader.rs).
    """

    def __init__(self, search_paths: list[Path] | None = None) -> None:
        """Create a loader with the given search paths (checked in order).

        If search_paths is None, uses DEFAULT_GENRE_PACK_SEARCH_PATHS.
        """
        self.search_paths: list[Path] = (
            search_paths if search_paths is not None else DEFAULT_GENRE_PACK_SEARCH_PATHS
        )

    def find(self, code: str | GenreCode) -> Path:
        """Find the directory for a genre code by searching all paths.

        Returns the first path where {search_path}/{genre_code}/ exists as a directory.

        Port of Rust GenreLoader::find().

        Raises:
            GenreNotFoundError: If not found in any search path.
        """
        code_str = str(code)
        searched: list[str] = []
        for base in self.search_paths:
            candidate = base / code_str
            if candidate.is_dir():
                return candidate
            searched.append(str(base))
        raise GenreNotFoundError(code=code_str, searched=searched)

    def load(self, code: str | GenreCode) -> GenrePack:
        """Find and load a genre pack by code.

        Port of Rust GenreLoader::load().

        Raises:
            GenreNotFoundError: If the pack directory is not found.
            GenreLoadError: If loading fails.
        """
        path = self.find(code)
        return load_genre_pack(path)


def _resolve_audio_urls(audio: AudioConfig, *, genre_slug: str) -> None:
    """In-place: rewrite every relative path in audio config to an absolute URL.

    Audio YAML stores paths relative to the genre-pack root, e.g.
    ``audio/music/combat.ogg``. The UI fetches these directly, so the server
    publishes them as full URLs at load time. Routing through
    :func:`resolve_asset_url` makes the cutover one env var.

    Path-bearing fields covered (per ``sidequest.genre.models.audio``):

    * ``mood_tracks[mood][i].path`` — :class:`MoodTrack`
    * ``sfx_library[bucket][i]`` — bare strings
    * ``themes[i].variations[j].path`` — :class:`AudioVariation`
    * ``faction_themes[i].track.path`` — :class:`MoodTrack`

    If new path-bearing fields are added to ``AudioConfig`` later, audit-extend
    here AND add a parallel test in
    ``tests/genre/test_audio_url_resolution.py`` (per CLAUDE.md "Verify
    Wiring").
    """
    from sidequest.genre.audio_paths import resolve_audio_relpath

    def _fix(rel: str) -> str:
        return resolve_audio_relpath(rel, genre_slug=genre_slug)

    for tracks in audio.mood_tracks.values():
        for track in tracks:
            track.path = _fix(track.path)
    for bucket, paths in audio.sfx_library.items():
        audio.sfx_library[bucket] = [_fix(p) for p in paths]
    for theme in audio.themes:
        for variation in theme.variations:
            variation.path = _fix(variation.path)
    for faction_theme in audio.faction_themes:
        faction_theme.track.path = _fix(faction_theme.track.path)


def find_pack_dir(code: str | GenreCode, search_paths: list[Path]) -> Path:
    """Find the pack directory for a genre code. Returns the first match.

    Raises:
        GenreNotFoundError: If not found in any search path.
    """
    return GenreLoader(search_paths=search_paths).find(code)


# ---------------------------------------------------------------------------
# Cached loader
# ---------------------------------------------------------------------------

_default_cache: GenreCache | None = None


def _get_default_cache() -> GenreCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = GenreCache()
    return _default_cache


def clear_default_cache() -> None:
    """Evict all entries from the default process-lifetime cache.

    Exposed for test isolation; not present in the Rust original.
    """
    _get_default_cache().clear()


def load_genre_pack_cached(
    genre_code: str | GenreCode,
    search_paths: list[Path] | None = None,
) -> GenrePack:
    """As load_genre_pack but with process-lifetime caching.

    Args:
        genre_code: Genre code string or GenreCode.
        search_paths: Search paths (uses defaults if None).

    Returns:
        GenrePack — same object returned on repeated calls for the same code.
    """
    code_str = str(genre_code)
    loader = GenreLoader(search_paths=search_paths)
    return _get_default_cache().get_or_load(code_str, loader)
