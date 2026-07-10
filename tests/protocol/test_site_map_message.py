"""SITE_MAP protocol cutover (Track B, Task 8) — one cutover, no alias.

RED (story 164-4): this file fails with ImportError until the
``DUNGEON_MAP -> SITE_MAP`` rename lands in ``enums.py`` / ``messages.py``.

Contract under test (plan §Task 8, consumed by Task 9 / story 164-5):
``SiteMapMessage``/``SiteMapPayload`` keep the DungeonMap* behavioral shape
(``current_location``, ``region``, ``explored``, ``fog_bounds``) and gain
``site_id``/``site_name``/``archetype``/``extent``. Wire string ``"SITE_MAP"``.
The Dungeon* symbols and the ``DUNGEON_MAP`` enum member are GONE — a rename,
not an addition; no back-compat alias.
"""

from __future__ import annotations

from typing import Any

from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    _KIND_TO_MESSAGE_CLS,
    SiteMapExit,
    SiteMapLocation,
    SiteMapMessage,
    SiteMapPayload,
)


def _payload(**overrides: Any) -> SiteMapPayload:
    kw: dict[str, Any] = {
        "current_location": "gilded_boar:r2",
        "region": "gilded_boar:r2",
        "site_id": "gilded_boar",
        "site_name": "The Gilded Boar",
        "archetype": "tavern",
        "extent": "bounded",
    }
    kw.update(overrides)
    return SiteMapPayload(**kw)


def test_site_map_wire_type_and_fields() -> None:
    msg = SiteMapMessage(payload=_payload(), player_id="p1")
    assert msg.type == MessageType.SITE_MAP
    dumped = msg.model_dump()
    assert dumped["type"] == "SITE_MAP"
    assert dumped["payload"]["site_id"] == "gilded_boar"
    assert dumped["payload"]["site_name"] == "The Gilded Boar"
    assert dumped["payload"]["archetype"] == "tavern"
    assert dumped["payload"]["extent"] == "bounded"


def test_site_map_wire_string() -> None:
    assert MessageType.SITE_MAP == "SITE_MAP"


def test_site_fields_default_empty() -> None:
    """The four site fields are additive with empty defaults so the payload
    builder can be cut over field-by-field without a hard construction
    dependency (plan §Task 8 'add after fog_bounds')."""
    p = SiteMapPayload(current_location="x", region="x")
    assert (p.site_id, p.site_name, p.archetype, p.extent) == ("", "", "", "")


def test_explored_shape_preserved_through_rename() -> None:
    """Same behavioral contract: the renamed classes keep the UI
    ``MapState``/``ExploredLocation`` shape the MapWidget consumes with no
    adapter (``room_exits`` drives the graph renderer; ``is_current_room``
    is the per-PC YOU-ARE-HERE marker)."""
    loc = SiteMapLocation(
        id="gilded_boar:entrance",
        name="Taproom",
        room_exits=[SiteMapExit(target="gilded_boar:r2", exit_type="corridor", bearing="north")],
        room_type="entrance",
        is_current_room=True,
    )
    p = _payload(explored=[loc])
    assert p.explored[0].room_exits[0].target == "gilded_boar:r2"
    assert p.explored[0].connections == []
    assert p.fog_bounds == {"width": 0, "height": 0}


def test_registry_and_validation_roundtrip() -> None:
    """The dispatch registry routes the new wire string, and a raw SITE_MAP
    wire dict validates through the message class (the UI-facing seam)."""
    assert _KIND_TO_MESSAGE_CLS["SITE_MAP"] is SiteMapMessage
    parsed = SiteMapMessage.model_validate(
        {
            "type": "SITE_MAP",
            "payload": {
                "current_location": "gilded_boar:r2",
                "region": "gilded_boar:r2",
                "site_id": "gilded_boar",
                "site_name": "The Gilded Boar",
                "archetype": "tavern",
                "extent": "bounded",
            },
        }
    )
    assert parsed.payload.site_id == "gilded_boar"
    assert parsed.payload.current_location == "gilded_boar:r2"


def test_dungeon_map_symbols_are_gone() -> None:
    """One cutover, no alias (plan §Task 8): the old names must not survive
    as re-exports — a lingering alias would let half the codebase keep
    emitting the dead frame and the UI cutover (164-5) would miss it."""
    import sidequest.protocol.messages as m

    assert not hasattr(m, "DungeonMapMessage"), "one cutover, no alias"
    assert not hasattr(m, "DungeonMapPayload"), "one cutover, no alias"
    assert not hasattr(m, "DungeonMapLocation"), "one cutover, no alias"
    assert not hasattr(m, "DungeonMapExit"), "one cutover, no alias"
    assert not hasattr(MessageType, "DUNGEON_MAP"), "a rename, not an addition"
    assert "DUNGEON_MAP" not in _KIND_TO_MESSAGE_CLS
