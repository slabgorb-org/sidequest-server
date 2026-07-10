"""World configuration, cartography, and navigation types.

Port of sidequest-genre/src/models/world.rs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from sidequest.protocol.models import LocationEntity


class NavigationMode(StrEnum):
    """Navigation mode for a world's cartography."""

    region = "region"
    room_graph = "room_graph"
    hierarchical = "hierarchical"


# ---------------------------------------------------------------------------
# RoomExit — tagged union (type discriminator)
# ---------------------------------------------------------------------------


class RoomExitDoor(BaseModel):
    model_config = {"extra": "forbid"}
    type: Literal["door"]
    target: str
    is_locked: bool = False


class RoomExitCorridor(BaseModel):
    model_config = {"extra": "forbid"}
    type: Literal["corridor"]
    target: str


class RoomExitChuteDown(BaseModel):
    model_config = {"extra": "forbid"}
    type: Literal["chute_down"]
    target: str


class RoomExitChuteUp(BaseModel):
    model_config = {"extra": "forbid"}
    type: Literal["chute_up"]
    target: str


class RoomExitSecret(BaseModel):
    model_config = {"extra": "forbid"}
    type: Literal["secret"]
    target: str
    discovered: bool = False


# Rust uses serde(tag = "type") on RoomExit.
RoomExit = Annotated[
    RoomExitDoor | RoomExitCorridor | RoomExitChuteDown | RoomExitChuteUp | RoomExitSecret,
    Field(discriminator="type"),
]


class LegendEntry(BaseModel):
    """A legend entry mapping a glyph character to a feature type and label."""

    model_config = {"extra": "forbid"}

    # "type" is a Python keyword — use alias
    feature_type: str = Field(alias="type", serialization_alias="type")
    label: str

    model_config = {"extra": "forbid", "populate_by_name": True}


class RoomDef(BaseModel):
    """A room in the dungeon room graph."""

    model_config = {"extra": "forbid"}

    id: str
    name: str
    room_type: str
    size: list[int] = Field(default_factory=lambda: [1, 1])
    keeper_awareness_modifier: float = 1.0
    exits: list[RoomExit] = Field(default_factory=list)
    description: str | None = None
    grid: str | None = None
    tactical_scale: int | None = None
    legend: dict[str, LegendEntry] | None = None


# ---------------------------------------------------------------------------
# Landmark — untagged union (string or detailed object)
# ---------------------------------------------------------------------------


class LandmarkDetailed(BaseModel):
    """Detailed landmark with type and description."""

    model_config = {"extra": "forbid"}

    name: str
    landmark_type: str = Field(alias="type", serialization_alias="type")
    description: str

    model_config = {"extra": "forbid", "populate_by_name": True}


# Landmark is either a plain string or a LandmarkDetailed dict.
# We handle this at the Region level with a custom validator.
Landmark = str | LandmarkDetailed


class Region(BaseModel):
    """A map region."""

    # No extra="forbid": uses flatten extras bag (chase_profile, etc.)
    model_config = {"extra": "allow"}

    name: str
    summary: str
    description: str
    adjacent: list[str] = Field(default_factory=list)
    landmarks: list[Any] = Field(default_factory=list)
    # Story 54-2 / ADR-109: typed location-entity manifest. Coexists
    # with the legacy untyped ``landmarks`` for backward compat —
    # content backfill in 54-4 and 54-5 ports authored worlds to the
    # typed shape. New code reads ``entities``; ``landmarks`` is
    # read-only legacy and slated for removal once all packs backfill.
    entities: list[LocationEntity] = Field(default_factory=list)
    origin: str | None = None
    rivers: list[Any] = Field(default_factory=list)
    settlements: list[Any] = Field(default_factory=list)
    terrain: str | None = None
    controlled_by: str | None = None
    # Spec §2 A2: binds this cartography region to a climate zone declared in
    # the world's weather.yaml. Typed (not just an extra="allow" bag entry) so
    # it is accessible/documentable and the validator + bootstrap can consume
    # it. Absent → the genre bootstrap default drives the opening weather.
    weather_zone: str | None = None


class Route(BaseModel):
    """A route between regions."""

    # No extra="forbid": uses flatten extras bag (faction_crossings, etc.)
    model_config = {"extra": "allow"}

    name: str
    description: str
    id: str | None = None
    from_id: str | None = None
    to_id: str | None = None
    distance: str | None = None
    danger: str | None = None
    waypoints: list[str] = Field(default_factory=list)
    difficulty: str | None = None
    # --- ADR-141 inter-system jump mechanics (Story 98-5) ---------------------
    # Additive, optional, typed jump-cost annotation the bound ruleset
    # (space_opera → SWN, ADR-117) reads when adjudicating a campaign-scale jump
    # across this edge. Authored by C2 (Story 98-4); absent on a bare adjacency
    # (the ruleset then computes an explicit, OTEL-logged default). Distinct from
    # the narrative ``danger`` descriptor above: ``hazard`` is mechanical crunch,
    # ``danger`` is free-text flavor — flavor must never silently drive mechanics.
    jump_fuel: int | None = Field(
        default=None,
        description="Drive fuel loads this jump consumes (SWN spike-drive). "
        "None → the ruleset's default fuel cost for the edge.",
    )
    transit_days: int | None = Field(
        default=None,
        description="Subjective transit time of the jump in days. "
        "None → the ruleset's default transit time.",
    )
    drive_rating_min: int | None = Field(
        default=None,
        description="Minimum spike-drive rating to jump this edge unstrained. "
        "A ship below it still crosses but burns an extra fuel load (not a block).",
    )
    hazard: str | None = Field(
        default=None,
        description="Mechanical hazard tag the ruleset applies on the jump "
        "(distinct from the narrative ``danger`` descriptor). None → no hazard.",
    )


SiteExtent = Literal["bounded", "frontier"]


class SiteDecl(BaseModel):
    """One authored site on a world node (cartography ``sites:`` entry).

    Defined here rather than in ``sidequest.game.sites.models`` so
    ``CartographyConfig`` can type its ``sites`` field without a ``game ->
    genre`` import cycle; ``game.sites.models`` re-exports it. ``seed`` is
    never authored — it is derived ``blake2b(campaign_seed, site_id)`` at
    materialization (Track B, Task 12). ``extra="allow"`` lets future
    per-archetype flavor ride along without failing load.
    """

    model_config = {"extra": "allow"}

    site_id: str
    name: str
    archetype: str
    attached_to: str
    extent: SiteExtent = "bounded"


class CartographyConfig(BaseModel):
    """Map and region configuration.

    ``extra="ignore"``: the Rust loader does not use ``#[serde(deny_unknown_fields)]``
    on CartographyConfig, so packs can authorially attach cartography flavor
    the engine doesn't consume — e.g. ``the_real_mccoy`` declares a top-level
    ``landmarks`` list and a ``train_cars`` object for narrator color. Those
    fields are dropped silently on the Rust side; we match that behavior here
    so content doesn't fail to load. Explicitly-typed fields below still
    enforce their shapes.
    """

    model_config = {"extra": "ignore"}

    world_name: str = ""
    starting_region: str = ""
    map_style: str = ""
    map_resolution: list[int] | None = None
    navigation_mode: NavigationMode = NavigationMode.region
    # Player-map disclosure policy (sq-playtest 2026-06-07: perseus_cloud's
    # full 35-system sector catalog — secret systems included — rendered at
    # discovered=1). ``public`` (default): the whole region catalog ships to
    # the client, correct for small worlds whose map is common knowledge (a
    # town, a neighborhood). ``fog``: only discovered regions ship with full
    # lore; regions adjacent to a discovered one ship name-only (the
    # explorable frontier); everything else is absent from the wire.
    # Content-expressible per world in cartography.yaml — no engine change
    # needed to make a new world spoiler-safe.
    discovery_mode: Literal["public", "fog"] = "public"
    regions: dict[str, Region] = Field(default_factory=dict)
    routes: list[Route] = Field(default_factory=list)
    # Authored sites attached to world nodes (Track B). Empty for every
    # existing world — a pure/additive field, no behavior change when unset.
    sites: list[SiteDecl] = Field(default_factory=list)
    rooms: list[RoomDef] | None = None


class MapProvenance(BaseModel):
    """Public-domain sourcing metadata for a raster main-map scan (spec §2).

    Required for a ``raster`` treatment — enforced by the pack validator, not
    here, so a non-raster treatment can omit it. The composer's PD-provenance
    pattern applied to maps: every scan names its source, date, archive, and
    the basis on which it is public domain.
    """

    model_config = {"extra": "forbid"}

    source: str
    date: str
    archive: str
    pd_basis: str


class MapTreatmentConfig(BaseModel):
    """Optional per-world main-map presentation layer, loaded from
    ``worlds/<slug>/map.yaml`` (spec §2).

    The cartography graph stays coordinate-free and semantic; this declares
    HOW it is drawn. Absent ``map.yaml`` → no treatment → d3-dag fallback (by
    design). This model enforces STRUCTURE only (enum kind, types,
    ``extra="forbid"`` — a malformed map.yaml fails loud at load). Content
    completeness (raster requires ``image`` + ``provenance``; every region
    has a ``node_anchor``) is enforced by the pack validator, which can see
    the sibling cartography.yaml.
    """

    model_config = {"extra": "forbid"}

    treatment: Literal["raster", "orrery", "dag", "generated"]
    image: str | None = None
    provenance: MapProvenance | None = None
    node_anchors: dict[str, list[float]] = Field(default_factory=dict)
    style_hints: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# WorldConfig — uses flatten extras
# ---------------------------------------------------------------------------


class WorldConfig(BaseModel):
    """World metadata."""

    # No extra="forbid": uses flatten extras (keeper, tagline, etc.)
    model_config = {"extra": "allow"}

    name: str
    slug: str = ""
    description: str
    starting_location: str = ""
    axis_snapshot: dict[str, float] = Field(default_factory=dict)
    # Era accepts int (a year like 1878) or str (a named period like "Victorian Era").
    era: str | int | None = None
    tone: str | None = None
    cover_poi: str | None = None
    draft: bool = False
    extensions: list[str] = Field(default_factory=list)
