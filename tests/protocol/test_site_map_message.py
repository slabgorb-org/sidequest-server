"""SITE_MAP wire contract — Track B, Task 8 (story 164-4).

One-step ``DUNGEON_MAP -> SITE_MAP`` cutover, NO alias: the renamed message
classes gain site identity fields (``site_id``/``site_name``/``archetype``/
``extent``) so the UI can name and distinguish sites (story 164-5 consumes
them). A rename, not an addition — ``len(MessageType)`` stays 59 (asserted
in ``test_enums.py``).
"""

from __future__ import annotations

from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    _KIND_TO_MESSAGE_CLS,
    GameMessage,
    SiteMapExit,
    SiteMapLocation,
    SiteMapMessage,
    SiteMapPayload,
)


def _payload(**overrides: object) -> SiteMapPayload:
    fields: dict[str, object] = {
        "current_location": "frontier:entrance",
        "region": "frontier:entrance",
        "site_id": "frontier",
        "site_name": "The Deep",
        "archetype": "megadungeon",
        "extent": "frontier",
    }
    fields.update(overrides)
    return SiteMapPayload(**fields)  # type: ignore[arg-type]


def test_site_map_wire_type_and_fields() -> None:
    msg = SiteMapMessage(payload=_payload(), player_id="p1")

    assert msg.type == MessageType.SITE_MAP
    dumped = msg.model_dump()
    assert dumped["type"] == "SITE_MAP"
    assert dumped["payload"]["site_id"] == "frontier"
    assert dumped["payload"]["site_name"] == "The Deep"
    assert dumped["payload"]["archetype"] == "megadungeon"
    assert dumped["payload"]["extent"] == "frontier"


def test_site_fields_default_empty_for_world_scene_grace() -> None:
    """Scope fence (B1): if a world-scene emission ever builds this payload,
    the site fields default to empty strings — graceful, never a crash."""
    payload = SiteMapPayload(current_location="x", region="x")

    assert payload.site_id == ""
    assert payload.site_name == ""
    assert payload.archetype == ""
    assert payload.extent == ""


def test_site_map_location_shape_survives_the_rename() -> None:
    """The renamed location/exit shapes keep the UI ``ExploredLocation``
    contract: ``room_exits`` (graph-renderer selector), ``is_current_room``
    (per-PC marker), ``room_type`` defaults."""
    exit_ = SiteMapExit(target="frontier:exp001.r2", exit_type="shaft", bearing="down")
    loc = SiteMapLocation(
        id="frontier:entrance",
        name="The Ropemouth",
        room_exits=[exit_],
        room_type="entrance",
        is_current_room=True,
    )
    payload = _payload(explored=[loc])

    assert payload.explored[0].room_exits[0].target == "frontier:exp001.r2"
    assert payload.explored[0].room_exits[0].bearing == "down"
    assert payload.explored[0].is_current_room is True
    assert payload.explored[0].room_type == "entrance"
    # defaults preserved from the DungeonMap* shapes
    assert loc.type == "region"
    assert loc.x == 0.0 and loc.y == 0.0


def test_dungeon_map_symbols_are_gone() -> None:
    """One cutover, NO alias — the old names must not survive anywhere on
    the protocol surface (runtime reflection, not a source grep)."""
    import sidequest.protocol.messages as m

    for name in (
        "DungeonMapExit",
        "DungeonMapLocation",
        "DungeonMapPayload",
        "DungeonMapMessage",
    ):
        assert not hasattr(m, name), f"one cutover, no alias: {name} must be gone"
    assert not hasattr(MessageType, "DUNGEON_MAP")
    assert "DUNGEON_MAP" not in {member.value for member in MessageType}


def test_site_map_registered_for_emit_event() -> None:
    """The ``_emit_event`` kind registry must route "SITE_MAP" to the renamed
    class — and must NOT keep a "DUNGEON_MAP" route (no alias)."""
    assert _KIND_TO_MESSAGE_CLS["SITE_MAP"] is SiteMapMessage
    assert "DUNGEON_MAP" not in _KIND_TO_MESSAGE_CLS


def test_site_map_round_trips_through_game_message_union() -> None:
    """Wiring: the discriminated union must parse a raw SITE_MAP wire dict
    into the typed message (the inbound/replay path) and dump it back."""
    raw = {
        "type": "SITE_MAP",
        "payload": {
            "current_location": "gilded_boar:r2",
            "region": "gilded_boar:r2",
            "site_id": "gilded_boar",
            "site_name": "The Gilded Boar",
            "archetype": "tavern",
            "extent": "bounded",
        },
        "player_id": "p1",
    }

    parsed = GameMessage.model_validate(raw)

    assert parsed.type == MessageType.SITE_MAP
    assert isinstance(parsed.root, SiteMapMessage)
    assert parsed.payload.site_id == "gilded_boar"
    assert parsed.root.model_dump()["type"] == "SITE_MAP"
