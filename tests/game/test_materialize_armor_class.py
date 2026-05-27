"""Task 9 (space_opera -> SWN binding) — opponent CreatureCore seeds hp + AC.

A hp_depletion combat opponent, seated via the REAL production seating path
(``instantiate_encounter_from_trigger``), must end up with a runtime
``CreatureCore`` whose ``armor_class`` and ``hp.current`` come from the
content-authored ``opponent_default_stats`` reserved keys, and that core must
be reachable via ``snapshot.find_creature_core(name)`` so the SWN attack can
roll against its AC and hp_depletion can resolve at ``hp.current <= 0``.

These tests drive the real ``space_opera`` pack (which authors ``hp`` /
``armor_class`` on both ``combat`` and ``ship_combat``) — that is the wiring
proof: the values flow content -> pack -> seating seam -> CreatureCore, not a
synthetic ``CreatureCore(...)`` construction.

The reserved-keys representation (``hp`` / ``armor_class`` popped out of the
ability-score map) is also unit-checked against a synthetic dial_threshold
confrontation to confirm ability-score-only packs are unaffected.
"""

from __future__ import annotations

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import ConfrontationDef
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound
from tests.genre.test_resolution_mode import load_pack

# Content-authored placeholders for space_opera personal Firefight combat.
_EXPECTED_HP = 7
_EXPECTED_AC = 12
# Ship combat (hull / ship AC).
_EXPECTED_SHIP_HP = 30
_EXPECTED_SHIP_AC = 14

_LOCATION = "Docking Ring"
_PC = "Vance"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_space_opera():
    try:
        return load_pack("space_opera")
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _snap() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations[_PC] = _LOCATION
    return snap


def _opponent_actors(enc) -> list:
    return [a for a in enc.actors if a.side == "opponent"]


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_router_named_opponent_with_no_backing_npc_gets_seeded_core() -> None:
    """Item 3 wiring: a router-named opponent that has NO pre-existing Npc in
    snapshot.npcs must get a backing CreatureCore created at seating, seeded
    from content hp/armor_class, and reachable via find_creature_core."""
    snap = _snap()
    pack = _load_space_opera()
    assert not snap.npcs, "precondition: no NPCs materialized yet"

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=_PC,
        npcs_present=[NpcMention(name="Pirate Boarder", side="opponent")],
        genre_slug="space_opera",
    )

    enc = snap.encounter
    assert enc is not None
    assert "Pirate Boarder" in [a.name for a in _opponent_actors(enc)]

    core = snap.find_creature_core("Pirate Boarder")
    assert core is not None, (
        "opponent core not reachable via find_creature_core — hp_depletion "
        "could never resolve and the SWN attack would have no AC to roll against"
    )
    assert core.armor_class == _EXPECTED_AC
    assert core.hp.current == _EXPECTED_HP
    assert core.hp.max == _EXPECTED_HP


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_existing_backing_npc_is_seeded_from_content() -> None:
    """When the opponent already has a backing Npc (location-fallback shape),
    its core.hp/armor_class are overwritten from content — not left at the
    default AC=10 / generic HP."""
    snap = _snap()
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Dock Thug",
                description="A wharf-rat with a stun baton.",
                personality="Belligerent.",
                inventory=Inventory(),
                hp=HpPool(current=7, max=7, base_max=7),
                armor_class=10,
            ),
            last_seen_location=_LOCATION,
        )
    )
    pack = _load_space_opera()

    # Empty npcs_present -> location fallback seats the Dock Thug as opponent.
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=_PC,
        npcs_present=[],
        genre_slug="space_opera",
    )

    enc = snap.encounter
    assert enc is not None
    assert "Dock Thug" in [a.name for a in _opponent_actors(enc)]

    core = snap.find_creature_core("Dock Thug")
    assert core is not None
    assert core.armor_class == _EXPECTED_AC
    assert core.hp.current == _EXPECTED_HP


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_ship_combat_seeds_hull_and_ship_ac() -> None:
    """ship_combat hp = HULL, armor_class = ship AC; same seeding seam."""
    snap = _snap()
    pack = _load_space_opera()

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="ship_combat",
        player_name=_PC,
        npcs_present=[NpcMention(name="Raider Frigate", side="opponent")],
        genre_slug="space_opera",
    )

    core = snap.find_creature_core("Raider Frigate")
    assert core is not None
    assert core.armor_class == _EXPECTED_SHIP_AC
    assert core.hp.current == _EXPECTED_SHIP_HP


# ---------------------------------------------------------------------------
# Reserved-keys representation: ability-score consumption is unaffected.
# ---------------------------------------------------------------------------


def test_reserved_keys_excluded_from_ability_scores() -> None:
    """opponent_ability_scores() pops hp/armor_class; the property accessors
    expose them. Ability-score-only packs (no reserved keys) are unchanged."""
    cdef = ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Firefight",
            "category": "combat",
            "win_condition": "hp_depletion",
            "opponent_default_stats": {
                "Physique": 9,
                "Reflex": 8,
                "hp": 12,
                "armor_class": 13,
            },
            "beats": [
                {"id": "strike", "label": "Strike", "kind": "strike", "stat_check": "Physique"}
            ],
        }
    )
    assert cdef.opponent_ability_scores() == {"Physique": 9, "Reflex": 8}
    assert cdef.opponent_hp == 12
    assert cdef.opponent_armor_class == 13

    # Ability-score-only pack: no reserved keys, accessors return None,
    # the score map is returned untouched.
    plain = ConfrontationDef.model_validate(
        {
            "type": "negotiation",
            "label": "Parley",
            "category": "social",
            "resolution_mode": "opposed_check",
            "win_condition": "hp_depletion",
            "opponent_default_stats": {"STR": 10, "DEX": 9},
            "beats": [
                {"id": "press", "label": "Press", "kind": "strike", "stat_check": "STR"}
            ],
        }
    )
    assert plain.opponent_ability_scores() == {"STR": 10, "DEX": 9}
    assert plain.opponent_hp is None
    assert plain.opponent_armor_class is None
