"""Story 106-1 (RED) — AC3: the opponent reprisal rolls vs the DERIVED armor class.

End-to-end proof that the chargen armor-derivation step (story 106-1) flows all the
way to the lethality seam: ``resolve_opponent_attack`` (dispatch/dice.py) reads
``player_core.armor_class`` for the opponent reprisal, so once ``equip_starting_armor``
raises a Warrior's AC from the unarmored 10 to the WWN-SRD leather value (13), the
reprisal's ``encounter.opponent_attack_resolved`` span must show ``target_ac = 13``,
not 10.

Reuses the canonical reprisal harness (``tests/integration/test_opponent_reprisal_e2e``
patterns) against the REAL space_opera pack. Skips gracefully when sidequest-content
is not on disk. ``otel_capture`` is re-exported from ``tests/integration/conftest.py``.

This is the gate that distinguishes a real mechanical fix from convincing narration:
the GM panel sees the higher target_ac, so a creature that used to hit ~65% now hits
~45% with zero change to damage.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import PackNotFound, find_pack_path

# The opponent-attack to-hit span (already emitted by the reprisal path).
SPAN_OPPONENT_ATTACK = "encounter.opponent_attack_resolved"

PLAYER = "Grix"
OPPONENT = "Corsair"

LEATHER_AC = 13  # WWN-SRD leather value (content-sourced, story 106-1 AC2)


def _load_space_opera_pack():
    from sidequest.genre.loader import load_genre_pack

    try:
        path = find_pack_path("space_opera")
    except PackNotFound:
        return None
    return load_genre_pack(path)


def _leather_catalog(*, leather_ac: int = LEATHER_AC):
    from sidequest.genre.models.inventory import CatalogItem, InventoryConfig

    return InventoryConfig(
        item_catalog=[
            CatalogItem(
                id="leather_armor",
                name="Leather Armor",
                description="Hardened leather.",
                category="armor",
                armor_class=leather_ac,
            )
        ]
    )


def _make_warrior_with_unequipped_leather():
    """A freshly built Warrior the way the kit-roll leaves them: Leather Armor in
    inventory ``equipped:false`` and ``core.armor_class`` at the unarmored 10."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name=PLAYER,
        description="A scarred delver.",
        personality="grim",
        inventory=Inventory(
            items=[
                {"id": "blaster_sidearm", "name": "Sidearm Blaster", "equipped": True},
                {
                    "id": "leather_armor",
                    "name": "Leather Armor",
                    "category": "armor",
                    "equipped": False,
                    "state": "Carried",
                    "quantity": 1,
                },
            ]
        ),
        hp={"current": 12, "max": 12, "base_max": 12},
    )
    return Character(core=core, char_class="Warrior", race="Human", backstory="Born underground.")


def _make_snapshot(player):
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    opponent_core = CreatureCore(
        name=OPPONENT,
        description="Corsair raider",
        personality="brutal",
        inventory=Inventory(items=[]),
        hp={"current": 99, "max": 99, "base_max": 99},
        armor_class=12,
    )
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(player)
    snap.npcs.append(Npc(core=opponent_core))
    return snap


def _make_encounter():
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(name=PLAYER, role="combatant", side="player"),
            EncounterActor(name=OPPONENT, role="combatant", side="opponent"),
        ],
        resolved=False,
    )


def _drive_player_shoot(snap, enc, pack, *, broadcasts):
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    return dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="reprisal-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[18],
            beat_id="shoot",
        ),
        rolling_player_id="player-grix",
        character_name=PLAYER,
        character_stats={"STR": 10},
        encounter=enc,
        pack=pack,
        genre_slug="space_opera",
        session_id="reprisal-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )


def _target_ac_from_span(otel_capture) -> int:
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_OPPONENT_ATTACK]
    assert len(spans) == 1, f"exactly one {SPAN_OPPONENT_ATTACK} span must fire"
    attrs = dict(spans[0].attributes or {})
    assert "target_ac" in attrs, "opponent-attack span must carry target_ac"
    return int(attrs["target_ac"])


def test_reprisal_rolls_vs_derived_ac_after_chargen_armor_equip(otel_capture):
    """AC3: after the chargen armor step derives AC 13 from content, the opponent
    reprisal rolls against 13 — proving the derivation reaches
    ``resolve_opponent_attack`` (it reads ``player_core.armor_class``)."""
    from sidequest.server.dispatch.chargen_loadout import equip_starting_armor

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("space_opera pack not on disk")

    player = _make_warrior_with_unequipped_leather()
    # The story's new step: equip the kit-rolled armor and derive AC from content.
    equip_starting_armor(player, _leather_catalog(leather_ac=LEATHER_AC))
    assert player.core.armor_class == LEATHER_AC, "precondition: derivation set AC to 13"

    _drive_player_shoot(_make_snapshot(player), _make_encounter(), pack, broadcasts=[])

    assert _target_ac_from_span(otel_capture) == LEATHER_AC


def test_reprisal_rolls_vs_unarmored_ac_without_armor(otel_capture):
    """AC3 control: a Warrior whose armor was NOT derived (e.g. no armor) is still
    hit at the unarmored 10 — the higher AC is genuinely a consequence of the
    derivation, not a constant the test baked in."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("space_opera pack not on disk")

    core = CreatureCore(
        name=PLAYER,
        description="A scarred delver.",
        personality="grim",
        inventory=Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}]),
        hp={"current": 12, "max": 12, "base_max": 12},
    )
    player = Character(core=core, char_class="Warrior", race="Human", backstory="b")
    assert player.core.armor_class == 10, "precondition: unarmored default"

    _drive_player_shoot(_make_snapshot(player), _make_encounter(), pack, broadcasts=[])

    assert _target_ac_from_span(otel_capture) == 10
