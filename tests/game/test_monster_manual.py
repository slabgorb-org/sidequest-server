"""Tests for ``sidequest.game.monster_manual``.

Ported from ``crates/sidequest-game/src/monster_manual.rs`` tests block.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from sidequest.game.monster_manual import EntryState, ManualNpc, MonsterManual


def test_new_manual_is_empty() -> None:
    manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
    assert manual.npcs == []
    assert manual.encounters == []
    assert manual.needs_seeding()


def test_add_npc_and_lookup() -> None:
    manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
    data = {
        "name": "Krag Dustwelder",
        "role": "mechanic",
        "culture": "Scrapborn",
        "ocean_summary": "blunt and competitive",
        "dialogue_quirks": ["quotes prices in three barter systems"],
    }
    manual.add_npc(data, [])

    assert len(manual.npcs) == 1
    assert manual.get_npc("Krag Dustwelder", "Scrapborn") is not None
    # Case-insensitive
    assert manual.get_npc("krag dustwelder", "scrapborn") is not None
    # Substring match
    assert manual.find_npc_by_name("Krag") is not None


def test_dedup_prevents_double_add() -> None:
    manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
    data = {"name": "Krag", "role": "mechanic", "culture": "Scrapborn"}
    manual.add_npc(dict(data), [])
    manual.add_npc(dict(data), [])
    assert len(manual.npcs) == 1


def test_lifecycle_transitions() -> None:
    manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
    manual.add_npc({"name": "A", "role": "r", "culture": "c"}, [])
    manual.add_npc({"name": "B", "role": "r", "culture": "c"}, [])

    assert len(manual.available_npcs()) == 2

    manual.mark_active("A", "The Collapsed Transit Hub")
    assert len(manual.available_npcs()) == 1
    assert manual.npcs[0].state == EntryState.ACTIVE
    assert manual.npcs[0].activated_location == "The Collapsed Transit Hub"

    manual.mark_all_dormant()
    assert manual.npcs[0].state == EntryState.DORMANT
    # B was never Active, so mark_all_dormant leaves it Available
    assert manual.npcs[1].state == EntryState.AVAILABLE


def test_format_nearby_npcs_filters_by_location() -> None:
    manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
    manual.add_npc(
        {
            "name": "Krag Dustwelder",
            "role": "mechanic",
            "culture": "Scrapborn",
            "ocean_summary": "blunt and competitive",
            "dialogue_quirks": ["quotes prices", "mentions danger casually"],
        },
        [],
    )
    manual.add_npc(
        {
            "name": "Zara Volt",
            "role": "trader",
            "culture": "Vaultborn",
            "ocean_summary": "calm and shrewd",
        },
        [],
    )

    manual.mark_active("Krag Dustwelder", "The Hub")

    # At the hub: Krag full profile, Zara name-only
    output = manual.format_nearby_npcs("The Hub")
    assert "NPCs present at this location" in output
    assert "Krag Dustwelder" in output
    assert "quotes prices" in output
    assert "Other known NPCs" in output
    assert "Zara Volt" in output

    # At a different location: Krag is anchored elsewhere, omitted
    other = manual.format_nearby_npcs("The Market")
    assert "Krag Dustwelder" not in other
    assert "Zara Volt" in other


def test_format_area_creatures_combat_vs_exploration() -> None:
    manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
    manual.add_encounter(
        {
            "enemies": [
                {
                    "name": "Salt Burrower",
                    "class": "Beastkin",
                    "tier_label": "tier-2",
                    "hp": 14,
                    "role": "ambush predator",
                    "abilities": ["Burrow Ambush", "Mandible Crush"],
                    "weaknesses": ["bright light", "fire"],
                }
            ]
        },
        2,
        [],
    )

    output = manual.format_area_creatures(True)
    assert "Hostile creatures" in output
    assert "Salt Burrower" in output
    assert "Burrow Ambush" in output
    assert "bright light" in output

    output_explore = manual.format_area_creatures(False)
    assert "Salt Burrower" in output_explore
    assert "Burrow Ambush" not in output_explore
    assert "bright light" not in output_explore


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    """Persistence round-trip uses ``~/.sidequest/manuals/{genre}_{world}.json``.

    Patch ``Path.home`` to redirect the manuals directory under tmp_path.
    """
    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="mutant_wasteland", world="flickering_reach")
        manual.add_npc({"name": "Krag", "role": "mechanic", "culture": "Scrapborn"}, [])
        manual.mark_active("Krag", "The Hub")
        manual.add_encounter(
            {"enemies": [{"name": "Salt Burrower", "hp": 14}]},
            tier=2,
            terrain_tags=["desert"],
        )
        manual.save()

        file_path = tmp_path / ".sidequest" / "manuals" / "mutant_wasteland_flickering_reach.json"
        assert file_path.exists()

        loaded = MonsterManual.load("mutant_wasteland", "flickering_reach")
        assert loaded.genre == "mutant_wasteland"
        assert loaded.world == "flickering_reach"
        assert len(loaded.npcs) == 1
        assert loaded.npcs[0].state == EntryState.ACTIVE
        assert loaded.npcs[0].activated_location == "The Hub"
        assert len(loaded.encounters) == 1
        assert loaded.encounters[0].tier == 2
        assert loaded.encounters[0].terrain_tags == ["desert"]


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """``load`` returns an empty manual when no file exists yet."""
    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual.load("does_not_exist_genre", "does_not_exist_world")
        assert manual.genre == "does_not_exist_genre"
        assert manual.world == "does_not_exist_world"
        assert manual.npcs == []
        assert manual.encounters == []


def test_load_corrupt_file_returns_empty(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Corrupt JSON on disk falls back to an empty Manual with a warning."""
    with mock.patch.object(Path, "home", return_value=tmp_path):
        manuals_dir = tmp_path / ".sidequest" / "manuals"
        manuals_dir.mkdir(parents=True)
        (manuals_dir / "g_w.json").write_text("not valid json", encoding="utf-8")

        manual = MonsterManual.load("g", "w")
        assert manual.npcs == []
        assert manual.encounters == []
        assert any("monster_manual.load_failed" in r.message for r in caplog.records)


def test_mark_active_preserves_first_location() -> None:
    """A second ``mark_active`` call does not move an already-anchored NPC."""
    manual = MonsterManual(genre="g", world="w")
    manual.add_npc({"name": "A", "role": "r", "culture": "c"}, [])
    manual.mark_active("A", "Location One")
    manual.mark_active("A", "Location Two")
    assert manual.npcs[0].activated_location == "Location One"


def test_available_encounters_filters_by_state() -> None:
    manual = MonsterManual(genre="g", world="w")
    manual.add_encounter({"enemies": []}, tier=1, terrain_tags=[])
    manual.add_encounter({"enemies": []}, tier=2, terrain_tags=[])
    manual.encounters[0].state = EntryState.ACTIVE
    available = manual.available_encounters()
    assert len(available) == 1
    assert available[0].tier == 2


def test_needs_seeding_when_few_npcs_or_no_encounters() -> None:
    manual = MonsterManual(genre="g", world="w")
    # 3 NPCs, no encounters → both conditions trigger
    for name in ("A", "B", "C"):
        manual.add_npc({"name": name, "role": "r", "culture": "c"}, [])
    assert manual.needs_seeding()

    # Add a fourth NPC and an encounter — both clear
    manual.add_npc({"name": "D", "role": "r", "culture": "c"}, [])
    manual.add_encounter({"enemies": []}, tier=1, terrain_tags=[])
    assert not manual.needs_seeding()


def test_format_returns_empty_when_no_state() -> None:
    manual = MonsterManual(genre="g", world="w")
    assert manual.format_nearby_npcs("anywhere") == ""
    assert manual.format_area_creatures(in_combat=False) == ""


def test_active_npc_without_location_anchors_to_current() -> None:
    """An Active NPC with ``activated_location=None`` is treated as present everywhere.

    Matches Rust ``unwrap_or(true)`` semantics in ``format_nearby_npcs``.
    """
    manual = MonsterManual(genre="g", world="w")
    manual.add_npc({"name": "Floater", "role": "r", "culture": "c"}, [])
    # Force Active without going through mark_active so activated_location stays None
    manual.npcs[0].state = EntryState.ACTIVE
    output = manual.format_nearby_npcs("Anywhere")
    assert "Floater" in output


def test_manual_npc_extra_fields_forbidden() -> None:
    """``model_config.extra='forbid'`` rejects unknown keys on load."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ManualNpc(
            data={},
            name="A",
            role="r",
            culture="c",
            unknown_field=True,  # type: ignore[call-arg]
        )
