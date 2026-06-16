"""Tests for sidequest.game.session — GameSnapshot, WorldStatePatch, NpcPatch.

Includes the wiring test: GameSnapshot with a Character + StateDelta
round-trips through model_dump_json()/validate_json().
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.delta import compute_delta, snapshot
from sidequest.game.session import (
    GameSnapshot,
    NarrativeEntry,
    Npc,
    NpcPatch,
    WorldStatePatch,
)
from tests.game.test_character import make_test_character


def _make_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="iron_mines",
        characters=[make_test_character()],
        location="The Upper Gallery",
        time_of_day="evening",
        atmosphere="tense silence",
        current_region="Ironhold",
        discovered_regions=["Ironhold"],
        quest_log={"Find the Warden": "active: search the lower mines"},
        active_stakes="escape before the collapse",
        lore_established=["The mines were sealed after the accident"],
    )


# ---------------------------------------------------------------------------
# GameSnapshot construction
# ---------------------------------------------------------------------------


def test_snapshot_defaults():
    s = GameSnapshot()
    assert s.genre_slug == ""
    assert s.characters == []
    # Wave 2B (story 45-48): party-level location field removed; per-character
    # locations live in ``character_locations`` (default empty dict).
    assert s.character_locations == {}
    assert s.player_dead is False


def test_snapshot_with_character():
    s = _make_snapshot()
    assert len(s.characters) == 1
    assert s.characters[0].core.name == "Thorn Ironhide"


# ---------------------------------------------------------------------------
# WIRING TEST: GameSnapshot + StateDelta round-trip
# ---------------------------------------------------------------------------


def test_gamesnapshot_with_character_and_delta_roundtrip():
    """Wiring test: GameSnapshot with a Character + StateDelta round-trips through JSON."""
    original = _make_snapshot()
    before_snap = snapshot(original)

    # Mutate state — Wave 2B (story 45-48): per-character location, not the
    # removed party-level field.
    original.character_locations["Thorn Ironhide"] = "The Lower Gallery"
    original.quest_log["Rescue the Miners"] = "active"

    after_snap = snapshot(original)
    delta = compute_delta(before_snap, after_snap)

    assert delta.location_changed()
    assert delta.quest_log_changed()
    assert not delta.is_empty()

    # Round-trip the snapshot
    json_str = original.model_dump_json()
    restored = GameSnapshot.model_validate_json(json_str)

    assert restored.character_locations["Thorn Ironhide"] == "The Lower Gallery"
    assert restored.genre_slug == "caverns_and_claudes"
    assert restored.characters[0].core.name == "Thorn Ironhide"
    assert "Rescue the Miners" in restored.quest_log


# ---------------------------------------------------------------------------
# apply_world_patch
# ---------------------------------------------------------------------------


def test_apply_patch_location():
    """Wave 2B (story 45-48): a party-frame ``WorldStatePatch.location``
    propagates to every character's per-character entry. Pre-chargen
    (no player_seats yet) falls back to ``snapshot.characters``."""
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(location="The Deep Caverns"))
    # _make_snapshot seats no players but has one character — falls back
    # to character iteration.
    assert s.character_locations["Thorn Ironhide"] == "The Deep Caverns"


def test_apply_patch_atmosphere():
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(atmosphere="eerie"))
    assert s.atmosphere == "eerie"


def test_apply_patch_quest_updates():
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(quest_updates={"New Quest": "active"}))
    assert "New Quest" in s.quest_log


def test_apply_patch_discover_regions():
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(discover_regions=["Sunken Vale"]))
    assert "Sunken Vale" in s.discovered_regions


def test_apply_patch_dedup_regions():
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(discover_regions=["Ironhold"]))
    assert s.discovered_regions.count("Ironhold") == 1


def test_apply_patch_lore_extends():
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(lore_established=["A new lore entry"]))
    assert "A new lore entry" in s.lore_established
    assert "The mines were sealed after the accident" in s.lore_established


def test_apply_patch_hp_changes():
    s = _make_snapshot()
    before = s.characters[0].core.hp.current
    s.apply_world_patch(WorldStatePatch(hp_changes={"Thorn Ironhide": -3}))
    assert s.characters[0].core.hp.current == before - 3


def test_apply_patch_hp_floored_at_zero():
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(hp_changes={"Thorn Ironhide": -1000}))
    assert s.characters[0].core.hp.current == 0


def test_apply_patch_npc_attitudes():
    npc = Npc(
        core=CreatureCore(
            name="Grog",
            description="A surly bouncer",
            personality="Gruff",
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        disposition=0,
    )
    s = _make_snapshot()
    s.npcs.append(npc)
    s.apply_world_patch(WorldStatePatch(npc_attitudes={"Grog": 10}))
    assert int(s.npcs[0].disposition) == 10


def test_apply_patch_npc_upsert_new():
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(npcs_present=[NpcPatch(name="Mira")]))
    assert any(n.core.name == "Mira" for n in s.npcs)


def test_apply_patch_npc_upsert_existing():
    npc = Npc(
        core=CreatureCore(
            name="Mira",
            description="An old woman",
            personality="Wise",
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        )
    )
    s = _make_snapshot()
    s.npcs.append(npc)
    s.apply_world_patch(
        WorldStatePatch(npcs_present=[NpcPatch(name="Mira", description="A young woman")])
    )
    mira = next(n for n in s.npcs if n.core.name == "Mira")
    assert mira.core.description == "A young woman"


def test_apply_patch_none_fields_unchanged():
    """Wave 2B (story 45-48): ``WorldStatePatch.location=None`` must not
    touch ``character_locations``."""
    s = _make_snapshot()
    original_locations = dict(s.character_locations)
    s.apply_world_patch(WorldStatePatch(atmosphere="eerie"))
    assert s.character_locations == original_locations


# ---------------------------------------------------------------------------
# NpcPatch validation
# ---------------------------------------------------------------------------


def test_npc_patch_blank_name_rejected():
    with pytest.raises(ValidationError):
        NpcPatch(name="")


def test_npc_patch_whitespace_name_rejected():
    with pytest.raises(ValidationError):
        NpcPatch(name="   ")


# ---------------------------------------------------------------------------
# Monster Manual creature patches (ADR-059 + ADR-078 HP→Edge)
# ---------------------------------------------------------------------------


def test_npc_patch_creature_fields_default_to_none() -> None:
    """The creature-shape fields are optional and don't affect human NPC patches."""
    patch = NpcPatch(name="Mira", description="A traveling scholar")
    assert patch.creature_id is None
    assert patch.threat_level is None
    assert patch.hp is None
    assert patch.abilities is None
    assert patch.morale is None


def test_apply_patch_creature_translates_hp_to_hp_pool() -> None:
    """Patch with creature ``hp`` materializes an Npc with HpPool seeded from hp."""
    s = _make_snapshot()
    s.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(
                    name="Chalk Moth",
                    creature_id="chalk_moth",
                    threat_level=1,
                    hp=1,
                    abilities=["Color Feed — Drains pigment from cloth."],
                    morale="cowardly",
                )
            ]
        )
    )
    moth = next(n for n in s.npcs if n.core.name == "Chalk Moth")
    assert moth.creature_id == "chalk_moth"
    assert moth.threat_level == 1
    assert moth.morale == "cowardly"
    assert moth.abilities == ["Color Feed — Drains pigment from cloth."]
    # HpPool seeded full from B/X hp
    assert moth.core.hp.current == 1
    assert moth.core.hp.max == 1
    assert moth.core.hp.base_max == 1
    # Level reflects threat_level
    assert moth.core.level == 1
    # Creatures default hostile (matches encountergen output)
    assert int(moth.disposition) == -20


def test_apply_patch_creature_hp_zero_clamps_to_one() -> None:
    """A creature authored with ``hp: 0`` clamps to EdgePool max=1 (positive ceiling)."""
    s = _make_snapshot()
    s.apply_world_patch(
        WorldStatePatch(npcs_present=[NpcPatch(name="Faint Echo", creature_id="echo", hp=0)])
    )
    echo = next(n for n in s.npcs if n.core.name == "Faint Echo")
    assert echo.core.hp.max == 1


def test_apply_patch_creature_threat_level_seeds_level_field() -> None:
    """``CreatureCore.level`` reflects ``threat_level`` for creature patches."""
    s = _make_snapshot()
    s.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(
                    name="Patient Butcher",
                    creature_id="patient_butcher",
                    threat_level=4,
                    hp=30,
                )
            ]
        )
    )
    boss = next(n for n in s.npcs if n.core.name == "Patient Butcher")
    assert boss.core.level == 4
    assert boss.core.hp.max == 30


def test_apply_patch_human_npc_unchanged_by_creature_signal_absence() -> None:
    """A patch with no creature fields still produces a human-shape NPC."""
    s = _make_snapshot()
    s.apply_world_patch(
        WorldStatePatch(npcs_present=[NpcPatch(name="Mira", description="scholar")])
    )
    mira = next(n for n in s.npcs if n.core.name == "Mira")
    assert mira.creature_id is None
    assert mira.threat_level is None
    assert mira.abilities == []
    assert mira.morale is None
    # Human NPC default disposition is neutral
    assert int(mira.disposition) == 0
    # Placeholder hp pool (not creature-seeded)
    assert mira.core.hp.max > 1  # PLACEHOLDER_EDGE_BASE_MAX is the constant


def test_apply_patch_creature_merge_updates_hp_and_flavor() -> None:
    """Re-emitting a creature patch updates EdgePool, abilities, and morale in place."""
    s = _make_snapshot()
    # First emission — chalk_moth at hp=1
    s.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(name="Chalk Moth", creature_id="chalk_moth", hp=1, morale="cowardly")
            ]
        )
    )
    # Second emission — same name, updated stats (e.g. enraged variant)
    s.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(
                    name="Chalk Moth",
                    hp=3,
                    abilities=["Shimmer Cloud — Disorients onlookers."],
                    morale="enraged",
                )
            ]
        )
    )
    moth = next(n for n in s.npcs if n.core.name == "Chalk Moth")
    assert moth.core.hp.max == 3
    assert moth.morale == "enraged"
    assert moth.abilities == ["Shimmer Cloud — Disorients onlookers."]


def test_creature_npc_roundtrips_through_json() -> None:
    """A creature-materialized Npc serializes and reloads losslessly.

    Required so the gaslighting payload reaches the narrator's
    ``state_summary`` JSON dump intact.
    """
    s = _make_snapshot()
    s.apply_world_patch(
        WorldStatePatch(
            npcs_present=[
                NpcPatch(
                    name="Chalk Moth",
                    creature_id="chalk_moth",
                    threat_level=1,
                    hp=1,
                    abilities=["Color Feed — Drains pigment."],
                    morale="cowardly",
                )
            ]
        )
    )
    serialized = s.model_dump_json()
    reloaded = GameSnapshot.model_validate_json(serialized)
    moth = next(n for n in reloaded.npcs if n.core.name == "Chalk Moth")
    assert moth.creature_id == "chalk_moth"
    assert moth.threat_level == 1
    assert moth.abilities == ["Color Feed — Drains pigment."]
    assert moth.morale == "cowardly"
    assert moth.core.hp.max == 1


def test_hp_pool_from_hp_helper() -> None:
    """Direct test of the HP seeding helper."""
    from sidequest.game.session import _hp_pool_from_hp

    pool = _hp_pool_from_hp(12)
    assert pool.current == 12
    assert pool.max == 12
    assert pool.base_max == 12


def test_creature_threat_level_only_still_signals_creature_branch() -> None:
    """``threat_level`` alone (no hp, no creature_id) still triggers creature defaults."""
    s = _make_snapshot()
    s.apply_world_patch(WorldStatePatch(npcs_present=[NpcPatch(name="Lurker", threat_level=2)]))
    lurker = next(n for n in s.npcs if n.core.name == "Lurker")
    assert int(lurker.disposition) == -20  # creature default hostile
    assert lurker.core.level == 2


# ---------------------------------------------------------------------------
# lowest_friendly_hp_ratio
# ---------------------------------------------------------------------------


def test_lowest_friendly_hp_ratio_full():
    s = _make_snapshot()
    assert s.lowest_friendly_hp_ratio() == 1.0


def test_lowest_friendly_hp_ratio_damaged():
    s = _make_snapshot()
    s.characters[0].core.hp.current = s.characters[0].core.hp.max // 2
    ratio = s.lowest_friendly_hp_ratio()
    assert 0.4 < ratio < 0.6


def test_lowest_friendly_hp_ratio_no_characters():
    s = GameSnapshot()
    assert s.lowest_friendly_hp_ratio() == 1.0


# ---------------------------------------------------------------------------
# NarrativeEntry
# ---------------------------------------------------------------------------


def test_narrative_entry_defaults():
    e = NarrativeEntry(author="narrator", content="The party enters the mines.")
    assert e.round == 0
    assert e.tags == []
    assert e.speaker is None


def test_narrative_entry_roundtrip():
    e = NarrativeEntry(
        timestamp=123,
        round=5,
        author="narrator",
        content="Darkness closes in.",
        tags=["exploration"],
    )
    back = NarrativeEntry.model_validate_json(e.model_dump_json())
    assert back.content == "Darkness closes in."
    assert back.tags == ["exploration"]


# ---------------------------------------------------------------------------
# GameSnapshot JSON round-trip (full)
# ---------------------------------------------------------------------------


def test_full_snapshot_roundtrip():
    s = _make_snapshot()
    json_str = s.model_dump_json()
    back = GameSnapshot.model_validate_json(json_str)
    assert back.genre_slug == "caverns_and_claudes"
    assert back.characters[0].core.name == "Thorn Ironhide"
    assert back.quest_log == s.quest_log
    assert back.lore_established == s.lore_established
