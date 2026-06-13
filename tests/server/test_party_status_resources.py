"""PARTY_STATUS carries the genre/world resource pools (2026-06-13 wiring fix).

The UI's CharacterPanel light gauge consumes
``PARTY_STATUS.payload.resources["light"]``. Before this fix the
``PartyStatusPayload`` had no ``resources`` field and ``views.py`` never
projected ``snapshot.resources`` — so the consumer read a path the producer
never sent and the gauge was dead in production.

This is the producer-side wiring test: build a snapshot with a ``light``
pool, run the real ``build_session_start_party_status`` emit path, and assert
the projected payload carries ``resources["light"]`` with the field names the
UI reads (``value``/``max``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.resource_pool import ResourcePool, ResourceThreshold
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server import views
from sidequest.server.session_handler import _SessionData

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _char(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory=f"{name}'s tale.",
        char_class="Delver",
        race="Human",
    )


def _sd(character: Character, resources: dict[str, ResourcePool]) -> _SessionData:
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name="P",
        player_id="player:1",
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            turn_manager=TurnManager(interaction=1),
            characters=[character],
            resources=resources,
        ),
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes"),
        orchestrator=MagicMock(),
        mode=GameMode.SOLO,
    )


def _handler() -> MagicMock:
    # Solo path: no room → build_session_start_party_status takes the
    # self-only branch and never touches seat/identity lookups.
    handler = MagicMock()
    handler._room = None
    return handler


def test_party_status_projects_light_pool() -> None:
    character = _char("Kael")
    light = ResourcePool(
        name="light",
        label="Light",
        current=3,
        min=0,
        max=6,
        voluntary=False,
        decay_per_turn=1,
        thresholds=[
            ResourceThreshold(
                at=0,
                event_id="dark",
                narrator_hint="The last torch gutters out.",
                direction="down",
            )
        ],
    )
    sd = _sd(character, {"light": light})

    msg = views.build_session_start_party_status(_handler(), sd, character, player_id="player:1")

    assert "light" in msg.payload.resources
    pool = msg.payload.resources["light"]
    # Field names the UI's CharacterPanel reads: value (engine `current`) + max.
    assert pool.value == 3
    assert pool.max == 6
    assert pool.min == 0
    assert pool.name == "light"
    assert pool.label == "Light"
    # Threshold projected to the UI shape (at→value, narrator_hint→label, down→low).
    assert len(pool.thresholds) == 1
    assert pool.thresholds[0].value == 0
    assert pool.thresholds[0].label == "The last torch gutters out."
    assert pool.thresholds[0].direction == "low"


def test_party_status_empty_resources_is_empty_dict_not_none() -> None:
    character = _char("Kael")
    sd = _sd(character, {})

    msg = views.build_session_start_party_status(_handler(), sd, character, player_id="player:1")

    # Empty dict (never None) so the UI's optional handling sees `{}`.
    assert msg.payload.resources == {}


def test_party_status_dark_pool_serializes_for_gauge() -> None:
    """At current 0 the UI renders the −2-in-the-dark affordance; prove the
    producer serializes value=0 through model_dump (the real wire path)."""
    character = _char("Kael")
    light = ResourcePool(
        name="light",
        label="Light",
        current=0,
        min=0,
        max=6,
        voluntary=False,
        decay_per_turn=1,
    )
    sd = _sd(character, {"light": light})

    msg = views.build_session_start_party_status(_handler(), sd, character, player_id="player:1")
    dumped = msg.payload.model_dump()
    assert dumped["resources"]["light"]["value"] == 0
    assert dumped["resources"]["light"]["max"] == 6
