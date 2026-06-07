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
# Hierarchical world graph
# ---------------------------------------------------------------------------


class Terrain(StrEnum):
    """Terrain type for graph edges.

    Genre-spanning vocabulary. Terrestrial values (road/wilderness/water/underground)
    cover most ground-bound worlds; space values (vacuum/atmospheric/jump_lane/orbit)
    cover orbital and interstellar worlds. New values can be added as new genres
    require them — the engine treats Terrain as opaque metadata for narrator color
    and renderer hints, not as a closed mechanical category.
    """

    # Terrestrial
    road = "road"
    wilderness = "wilderness"
    water = "water"
    underground = "underground"
    # Space / orbital
    vacuum = "vacuum"
    atmospheric = "atmospheric"
    jump_lane = "jump_lane"
    orbit = "orbit"


class WorldGraphNode(BaseModel):
    """A node in the world graph — a major location.

    ``extra="allow"`` so genre packs can decorate nodes with genre-specific flavor
    (e.g. ``kind: gas_giant``, ``provenance: pre-collapse-relic``) without bloating
    the engine schema. Unknown fields are preserved on the model so narrator and
    renderer code can read them; the engine itself treats them as opaque.
    """

    model_config = {"extra": "allow"}

    id: str
    name: str
    description: str = ""


class GraphEdge(BaseModel):
    """An edge between two world graph nodes.

    ``extra="allow"`` so genre packs can tag edges with relationship metadata
    (e.g. ``relation: orbits``, ``seasonal: true``) without forcing those
    concepts into the engine schema. The engine reads only the typed fields;
    extras are narrator/renderer flavor.
    """

    from_: str = Field(alias="from", serialization_alias="from")
    to: str
    danger: int
    terrain: Terrain = Terrain.road
    distance: int = 1
    encounter_table_key: str | None = None

    model_config = {"extra": "allow", "populate_by_name": True}


class SubGraph(BaseModel):
    """A sub-graph: internal topology for a world graph node."""

    model_config = {"extra": "forbid"}

    nodes: list[WorldGraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class WorldGraph(BaseModel):
    """The top-level world graph."""

    model_config = {"extra": "forbid"}

    nodes: list[WorldGraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


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
    rooms: list[RoomDef] | None = None
    world_graph: WorldGraph | None = None
    sub_graphs: dict[str, SubGraph] | None = None


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
