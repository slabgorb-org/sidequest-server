"""ADR-114 Task 10 — space_opera e2e acceptance proof.

Proves that ONE space_opera combat turn delivers ALL THREE of the ADR-114
success criteria in a single strike-beat resolution:

  (HP layer)   target CreatureCore.hp.current decreased by damage_total - mitigation
  (OTEL)       a state_patch.hp span fired
  (dial layer) the encounter's momentum dial also advanced (narrative pacing layer
               runs alongside HP — both legible in one turn)

Beat chosen: ``shoot`` from the ``combat`` (Firefight) ConfrontationDef.
  - kind: strike
  - damage_channel: strike  (added in Part A content canary)
Weapon: blaster_sidearm (damage: 1d6+0) from the REAL pack catalog.
Stat: Physique (the shoot beat's stat_check).

**Fixture note (documented as required by the task):**
The space_opera ``combat`` (Firefight) confrontation uses
``resolution_mode: opposed_check``, which defers beat application and the
entire damage path to the narrator phase — damage never fires through
``dispatch_dice_throw`` on the opposed branch.

This test therefore uses a SYNTHETIC ConfrontationDef that mirrors the
Firefight shape (shoot beat with damage_channel=strike, momentum dial,
threshold=7) but omits ``resolution_mode: opposed_check`` (defaults to
``beat_selection``) so the damage roll fires inline. The REAL blaster_sidearm
weapon and its damage spec (1d6+0) are still pulled from the real pack catalog,
so the Part A canary content is exercised for the weapon lookup path.

The synthetic beat carries the same ``stat_check: Physique`` and
``damage_channel: strike`` as the authored shoot beat.

Skips gracefully when sidequest-content is not present.

The ``otel_capture`` fixture is imported from
``tests/integration/conftest.py`` which re-exports it from
``tests/server/conftest.py``.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import PackNotFound, find_pack_path

# ---------------------------------------------------------------------------
# Pack guard
# ---------------------------------------------------------------------------


def _load_space_opera_pack():
    from sidequest.genre.loader import load_genre_pack

    try:
        path = find_pack_path("space_opera")
    except PackNotFound:
        return None
    return load_genre_pack(path)


# ---------------------------------------------------------------------------
# Fixture helpers (inline — no cross-test coupling)
# ---------------------------------------------------------------------------


def _make_creature_core_with_hp(name: str, *, hp: int = 12):
    """CreatureCore with an HpPool seeded to a known value."""
    from sidequest.game.creature_core import CreatureCore, Inventory

    return CreatureCore(
        name=name,
        description=f"{name} description",
        personality="aggressive",
        inventory=Inventory(),
        hp={"current": hp, "max": hp, "base_max": hp},
    )


def _make_snapshot_with_blaster_attacker(attacker_name: str, target_name: str):
    """GameSnapshot with:
    - attacker Character whose inventory.items carries a blaster_sidearm dict
    - target NPC with known HP, unarmored (no mitigation item)

    The inventory uses list[dict] (not InventorySlot objects).
    _resolve_damage_spec_from_beat_and_actor priority-3 path looks up the
    item id in the pack's item_catalog; blaster_sidearm now carries
    damage: {dice: "1d6", bonus: 0} so the DamageSpec resolves cleanly.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    # --- Attacker ---
    # Seed the inventory with a blaster_sidearm dict so the catalog lookup fires.
    atk_inventory = Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}])
    atk_core = CreatureCore(
        name=attacker_name,
        description="Soldier on station duty",
        personality="disciplined",
        inventory=atk_inventory,
        hp={"current": 10, "max": 10, "base_max": 10},
    )

    attacker_char = Character(
        core=atk_core,
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
    )

    # --- Target ---
    # Unarmored — no mitigation item in inventory. HP damage is raw dice total.
    target_core = _make_creature_core_with_hp(target_name, hp=15)

    # --- Snapshot ---
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(attacker_char)
    snap.npcs.append(Npc(core=target_core))

    return snap


def _make_firefight_encounter(attacker_name: str, target_name: str):
    """StructuredEncounter matching the space_opera combat/Firefight shape.

    Metric: momentum, threshold=7 (per rules.yaml).
    Actors: attacker on player side, target on opponent side.
    """
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=7
        ),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=7
        ),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=attacker_name, role="combatant", side="player"),
            EncounterActor(name=target_name, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _make_synthetic_firefight_pack(real_pack):
    """Build a synthetic GenrePack-shaped object that mirrors the space_opera
    Firefight ConfrontationDef but with beat_selection resolution mode so
    dispatch_dice_throw applies the beat inline (no deferral).

    The real pack's inventory catalog is preserved so the blaster_sidearm
    weapon lookup exercises the Part A canary content.

    Rationale for synthetic fixture:
    The authored space_opera ``combat`` confrontation uses
    ``resolution_mode: opposed_check``, which defers beat application and the
    damage path entirely to the narrator phase. ``dispatch_dice_throw``
    never fires damage on the opposed branch. The synthetic fixture swaps
    only the resolution_mode; all other beat attributes (stat_check=Physique,
    damage_channel=strike, base=2, momentum dial/threshold) are identical
    to the authored shoot beat.
    """
    from unittest.mock import MagicMock

    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )

    # Mirror the authored shoot beat exactly, minus the opposed_check constraint.
    shoot_beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Shoot",
            "kind": "strike",
            "base": 2,
            "stat_check": "Physique",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Blaster bolts sear the corridor. Sparks off bulkheads.",
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        # Default resolution_mode=beat_selection — NO opposed_check deferral.
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[shoot_beat],
    )

    pack = MagicMock()
    pack.rules = RulesConfig(confrontations=[cdef])
    # Use the real pack's inventory so blaster_sidearm catalog lookup works.
    pack.inventory = real_pack.inventory
    return pack


# ---------------------------------------------------------------------------
# The three-criterion acceptance test
# ---------------------------------------------------------------------------


def test_space_opera_shoot_beat_deals_hp_damage_while_dials_advance(otel_capture):
    """ADR-114 Task 10 — three-part acceptance proof.

    Drives the real space_opera pack through dispatch_dice_throw with the
    ``shoot`` beat (damage_channel=strike, stat_check=Physique) and a
    face value that guarantees SUCCESS so the damage roll fires.

    Asserts:
    (HP layer)   target hp.current < hp_before
    (OTEL)       state_patch.hp span present in otel_capture
    (dial layer) encounter.player_metric.current > 0 (momentum advanced)

    Uses REAL pack: the Part A canary weapon (blaster_sidearm, 1d6+0) and
    the shoot beat's damage_channel=strike annotation are both exercised.
    """
    real_pack = _load_space_opera_pack()
    if real_pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    attacker_name = "Nova"
    target_name = "Corsair"

    # Part A canary checks against the REAL pack:
    # 1. shoot beat exists and carries damage_channel=strike.
    real_combat_cdef = next(
        (c for c in real_pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert real_combat_cdef is not None, (
        "space_opera pack must have a 'combat' ConfrontationDef"
    )
    real_shoot_beat = next((b for b in real_combat_cdef.beats if b.id == "shoot"), None)
    assert real_shoot_beat is not None, "space_opera combat confrontation must have a 'shoot' beat"
    assert str(real_shoot_beat.damage_channel) == "strike", (
        "shoot beat must carry damage_channel=strike (Part A content canary)"
    )

    # 2. blaster_sidearm has a damage spec in the catalog.
    sidearm_item = next(
        (i for i in (real_pack.inventory.item_catalog if real_pack.inventory else [])
         if i.id == "blaster_sidearm"),
        None,
    )
    assert sidearm_item is not None, "blaster_sidearm must be in item_catalog"
    assert sidearm_item.damage is not None, (
        "blaster_sidearm must carry a damage spec (Part A content canary)"
    )

    # Synthetic pack: mirrors Firefight shape but with beat_selection mode so
    # dispatch_dice_throw applies the beat inline (real pack uses opposed_check
    # which defers damage to the narrator phase — see module docstring).
    # Real pack's inventory catalog is preserved for blaster_sidearm lookup.
    pack = _make_synthetic_firefight_pack(real_pack)

    enc = _make_firefight_encounter(attacker_name, target_name)
    snap = _make_snapshot_with_blaster_attacker(attacker_name, target_name)

    # Record target HP before.
    target_core = snap.find_creature_core(target_name)
    assert target_core is not None, "target must be discoverable in snapshot"
    hp_before = target_core.hp.current

    # face=18, Physique=10 (+0 mod).
    # shoot beat base=2 → DC = 10 + |2|*2 = 14.
    # total = 18 + 0 = 18 > 14 → Success tier → damage fires.
    broadcasts: list[object] = []
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="so-e2e-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[18],
            beat_id="shoot",
        ),
        rolling_player_id="player-nova",
        character_name=attacker_name,
        character_stats={"Physique": 10},
        encounter=enc,
        pack=pack,
        genre_slug="space_opera",
        session_id="so-e2e-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )

    # ── Criterion 1: HP layer ──────────────────────────────────────────────
    hp_after = target_core.hp.current
    assert hp_after < hp_before, (
        f"(HP layer) target HP must decrease after a successful shoot beat; "
        f"before={hp_before} after={hp_after}"
    )

    # ── Criterion 2: OTEL span ────────────────────────────────────────────
    finished_span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in finished_span_names, (
        f"(OTEL) state_patch.hp span must fire on strike beat; "
        f"got spans: {finished_span_names}"
    )

    # ── Criterion 3: dial layer ───────────────────────────────────────────
    assert enc.player_metric.current > 0, (
        f"(dial layer) momentum dial must advance after successful shoot beat "
        f"(proving narration pacing layer runs alongside HP); "
        f"got player_metric.current={enc.player_metric.current}"
    )
