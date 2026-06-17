"""Story 106-4 Part C — item-use beats reach the CONFRONTATION payload.

build_confrontation_payload must thread the recipient PC's carried inventory
(resolved via ``core_resolver``) into ``beats_available_for`` so a heal
consumable surfaces as a "Drink <potion>" beat in the per-PC overlay. The
transient item beat carries NO server-authored ``difficulty`` — it is an
auto-success, no-roll action (Keith, 2026-06-14), and the absent key is the
wire signal the UI reads to commit it without a dice tray.
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import (
    BeatDef,
    BeatKind,
    ConfrontationDef,
    MetricDef,
)
from sidequest.server.dispatch.confrontation import build_confrontation_payload


def _fighter() -> ClassDef:
    return ClassDef(
        id="fighter",
        display_name="Fighter",
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table="fighter_kit",
        flavor="-",
        encounter_beat_choices=["strike"],
    )


def _hp_combat_cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        win_condition="hp_depletion",
        player_metric=MetricDef(name="m", starting=0, threshold=10),
        opponent_metric=MetricDef(name="m", starting=0, threshold=10),
        beats=[BeatDef(id="strike", label="Strike", kind=BeatKind.strike, stat_check="STR")],
        opponent_default_stats={
            "STR": 10,
            "DEX": 10,
            "CON": 10,
            "INT": 10,
            "WIS": 10,
            "CHA": 10,
            "hp": 10,
            "armor_class": 10,
            "dexterity": 10,
        },
    )


def _encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="m", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="m", current=0, starting=0, threshold=10),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(name="Groucho", role="combatant", side="player"),
            EncounterActor(name="Nearest shape", role="combatant", side="opponent"),
        ],
    )


def _core_with_items(items: list[dict]) -> SimpleNamespace:
    """Minimal stand-in for the recipient CreatureCore the resolver returns —
    only ``spellcasting`` and ``inventory.items`` are read by the payload."""
    return SimpleNamespace(spellcasting=None, inventory=SimpleNamespace(items=items))


_POTION = {
    "name": "Potion of Mending",
    "category": "consumable",
    "tags": ["consumable", "healing"],
    "heal_amount": "1d6+2",
}


def test_heal_potion_surfaces_drink_beat_in_payload() -> None:
    core = _core_with_items([_POTION])
    payload = build_confrontation_payload(
        encounter=_encounter(),
        cdef=_hp_combat_cdef(),
        genre_slug="caverns_and_claudes",
        recipient_pc=(_fighter(), 0.0, None),
        recipient_actor_name="Groucho",
        core_resolver=lambda name: core if name == "Groucho" else None,
    )
    ids = [b["id"] for b in payload["beats"]]
    assert "strike" in ids
    assert "use_item:potion_of_mending" in ids
    drink = next(b for b in payload["beats"] if b["id"] == "use_item:potion_of_mending")
    assert drink["label"] == "Drink Potion of Mending"


def test_no_drink_beat_when_recipient_carries_no_consumable() -> None:
    core = _core_with_items([{"name": "Iron Mace", "category": "weapon", "tags": ["weapon"]}])
    payload = build_confrontation_payload(
        encounter=_encounter(),
        cdef=_hp_combat_cdef(),
        genre_slug="caverns_and_claudes",
        recipient_pc=(_fighter(), 0.0, None),
        recipient_actor_name="Groucho",
        core_resolver=lambda name: core,
    )
    assert not any(b["id"].startswith("use_item:") for b in payload["beats"])


def test_item_use_beat_carries_no_server_difficulty() -> None:
    """With ``rules`` threaded (DC-authoring active), the authored strike gets a
    ``difficulty`` but the item-use beat must NOT — its absence is how the UI
    knows to skip the dice tray."""
    from sidequest.genre.models.rules import RulesConfig

    core = _core_with_items([_POTION])
    rules = RulesConfig(ruleset="dial")
    payload = build_confrontation_payload(
        encounter=_encounter(),
        cdef=_hp_combat_cdef(),
        genre_slug="caverns_and_claudes",
        recipient_pc=(_fighter(), 0.0, None),
        recipient_actor_name="Groucho",
        core_resolver=lambda name: core,
        rules=rules,
    )
    strike = next(b for b in payload["beats"] if b["id"] == "strike")
    drink = next(b for b in payload["beats"] if b["id"] == "use_item:potion_of_mending")
    assert "difficulty" in strike
    assert "difficulty" not in drink
