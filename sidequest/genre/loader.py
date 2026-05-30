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
from sidequest.genre.models.progression import ProgressionConfig
from sidequest.genre.models.rigs_world import ChassisInstanceConfig, RigsWorldConfig
from sidequest.genre.models.rules import RulesConfig
from sidequest.genre.models.scenario import ScenarioNpc, ScenarioPack
from sidequest.genre.models.theme import GenreTheme
from sidequest.genre.models.tropes import SeedTrope, TropeDefinition
from sidequest.genre.models.world import CartographyConfig, NavigationMode, WorldConfig
from sidequest.genre.models.wwn_spell import WwnSpellCatalog
from sidequest.genre.resolve import resolve_trope_inheritance

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


def _load_single_world(
    world_path: Path,
    genre_tropes: list[TropeDefinition],
    genre_root: Path,
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

    char_creation_raw = _load_yaml_raw_optional(world_path / "char_creation.yaml")
    char_creation: list[CharCreationScene] = (
        [CharCreationScene.model_validate(c) for c in char_creation_raw]
        if isinstance(char_creation_raw, list)
        else []
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

    # ADR-079: optional world-level theme override (worlds/<slug>/client_theme.css).
    # When present, this CSS replaces the genre-level theme at connect time.
    client_theme_css = _load_text_optional(world_path / "client_theme.css")

    # ADR-053 / Story 71-32: world-tier scenarios (worlds/<slug>/scenarios/).
    # Each world owns its scenarios; bind_scenario binds only the active world's.
    # Absent dir → {} (no silent fallback to pack-level GenrePack.scenarios).
    world_scenarios: dict[str, ScenarioPack] = _load_subdirectories(
        world_path, "scenarios", _load_single_scenario
    )

    return World(
        config=config,
        lore=lore,
        legends=legends,
        cartography=cartography,
        cultures=cultures,
        tropes=tropes,
        archetypes=archetypes,
        visual_style=visual_style,
        history=history,
        legends_raw=legends_raw,
        portrait_manifest=portrait_manifest,
        archetype_funnels=archetype_funnels,
        openings=openings,
        authored_npcs=authored_npcs,
        char_creation=char_creation,
        chassis_instances=chassis_instances,
        magic_register=magic_register,
        items=items,
        scenarios=world_scenarios,
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

    # Load required files
    meta = _load_yaml(path / "pack.yaml", PackMeta)
    rules = _load_rules_config(path / "rules.yaml", path)
    lore = _load_yaml(path / "lore.yaml", Lore)
    theme = _load_yaml(path / "theme.yaml", GenreTheme)
    archetypes_raw = _load_yaml_raw(path / "archetypes.yaml")
    archetypes: list[NpcArchetype] = (
        [NpcArchetype.model_validate(a) for a in archetypes_raw]
        if isinstance(archetypes_raw, list)
        else []
    )
    char_creation_raw = _load_yaml_raw(path / "char_creation.yaml")
    char_creation: list[CharCreationScene] = (
        [CharCreationScene.model_validate(c) for c in char_creation_raw]
        if isinstance(char_creation_raw, list)
        else []
    )
    # Pack-level visual_style is optional (2026-05-29 directive — visual
    # prompts live at world level). Absent → None; the daemon resolves style
    # from the world scope and fails loud if neither scope supplies it.
    visual_style = _load_yaml_optional(path / "visual_style.yaml", VisualStyle)
    progression = _load_yaml(path / "progression.yaml", ProgressionConfig)
    axes = _load_yaml(path / "axes.yaml", AxesConfig)
    audio = _load_yaml(path / "audio.yaml", AudioConfig)
    _resolve_audio_urls(audio, genre_slug=path.name)
    cultures_raw = _load_yaml_raw(path / "cultures.yaml")
    cultures: list[Culture] = (
        [Culture.model_validate(c) for c in cultures_raw] if isinstance(cultures_raw, list) else []
    )
    prompts = _load_yaml(path / "prompts.yaml", Prompts)

    # Load required genre-level tropes
    genre_tropes_raw = _load_yaml_raw(path / "tropes.yaml")
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
    voice_presets: VoicePresets | None = _load_yaml_optional(
        path / "voice_presets.yaml", VoicePresets
    )
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

    # Load worlds and scenarios from subdirectories.
    # _load_single_world returns None for worlds with draft: true — filter them out.
    worlds_raw: dict[str, World | None] = _load_subdirectories(
        path, "worlds", lambda p: _load_single_world(p, genre_tropes, path)
    )
    worlds: dict[str, World] = {slug: w for slug, w in worlds_raw.items() if w is not None}
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
        voice_presets=voice_presets,
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
    from sidequest.server.asset_urls import resolve_asset_url

    def _fix(rel: str) -> str:
        if not rel:
            return rel
        if rel.startswith(("http://", "https://", "/")):
            return rel  # already resolved (e.g. test fixtures)
        return resolve_asset_url(f"genre_packs/{genre_slug}/{rel}")

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
