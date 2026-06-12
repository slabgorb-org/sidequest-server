"""WWN Killing Blow — wiring proof for dispatch_dice_throw strike + Shock seam.

Proves the Killing Blow rider is wired into ``dispatch_dice_throw``'s:
  1. HIT damage block (strike-damage path, after trauma seam)
  2. MISS Shock block (shock path, on Fail/CritFail)

...for a WWN Warrior-archetype actor ONLY. Non-Warriors and non-wwn packs are
unaffected (no span emitted, no bonus added).

Test strategy mirrors ``test_neon_combat_lethality_dispatch.py``:
  - Synthetic wwn-bound pack (MagicMock) with a strike beat carrying
    ``damage_override`` DamageSpec so ``resolve_damage`` resolves without
    inventory wiring (Priority-1: damage_override is checked first).
  - A Warrior class (``warrior: true`` on ClassDef) and a non-Warrior class.
  - ``dispatch_dice_throw`` called directly through the REAL production dispatcher.
  - OTEL spans captured via ``otel_capture`` fixture from server conftest.
  - ``face=[20]`` = guaranteed HIT; ``face=[1]`` = guaranteed MISS for Shock test.

Determinism:
  The Killing Blow bonus is pure math: ``ceil(level / killing_blow_divisor)``.
  With level=4 and divisor=2 (WwnConfig default), bonus=2.
  The 1d6 damage roll uses ``damage_roll.random`` (a different module), NOT
  ``dice.random``, so the Killing Blow rider is deterministically additive.
  For the Shock test (face=[1] miss) we need a wwn pack whose weapon has a
  ``shock`` rating; the chip is fixed so the rider adds a deterministic bonus.

NOT a source-grep test — behavior is proven by OTEL spans + HP state.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

# WWN attribute map.
_ATTRIBUTE_MAP = {
    "STRENGTH": "Might",
    "CONSTITUTION": "Vigor",
    "DEXTERITY": "Grace",
    "INTELLIGENCE": "Lore",
    "WISDOM": "Wit",
    "CHARISMA": "Bearing",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_STATS = {name: 12 for name in _ABILITY_SCORE_NAMES}

_OPPONENT_AC = 10
_OPPONENT_HP = 30  # high enough to survive a single strike without complicating downed seam
_ATTACKER_LEVEL = 4  # level 4 → bonus = ceil(4/2) = 2
_KB_DIVISOR = 2  # WwnConfig default
_EXPECTED_KB_BONUS = math.ceil(_ATTACKER_LEVEL / _KB_DIVISOR)  # 2


def _make_wwn_pack(*, shock_weapon: bool = False):
    """A MagicMock pack with a real wwn-bound RulesConfig and two classes.

    The strike beat carries a ``damage_override`` DamageSpec (Priority-1
    resolution — no inventory needed).

    ``classes`` is a list with:
      - "Guardian"  — warrior=True (Task 11 will use this name on the real pack)
      - "Sage"      — warrior=False

    ``pack.classes`` is set as a Python attribute (not a MagicMock attr) so
    the is_warrior lookup works identically to production.

    When ``shock_weapon=True``, the damage_override carries a shock/shock_ac
    pair so the miss path (face=[1]) triggers the Shock seam.
    """
    from sidequest.genre.models.character import ClassDef
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        SystemStrainConfig,
        WwnConfig,
    )

    if shock_weapon:
        damage_override = DamageSpec(
            dice="1d6",
            shock=3,  # chip amount X
            shock_ac=15,  # AC ceiling Y; opponent AC (10) <= 15 → chip applies on miss
        )
    else:
        damage_override = DamageSpec(dice="1d6")

    strike_beat = BeatDef.model_validate(
        {
            "id": "slash",
            "label": "Slash",
            "kind": "strike",
            "base": 2,
            "stat_check": "Might",
            "damage_channel": "strike",
            "effect": "A precise strike.",
            "narrator_hint": "Steel rings.",
            "damage_override": damage_override,
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Battle",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
        opponent_default_stats={name: 12 for name in _ABILITY_SCORE_NAMES},
    )

    wwn_cfg = WwnConfig(
        attribute_map=dict(_ATTRIBUTE_MAP),
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        wwn=wwn_cfg,
    )
    pack.inventory = None

    # Two classes: one Warrior (warrior=True), one non-Warrior.
    guardian = ClassDef(
        id="guardian",
        display_name="Guardian",
        rpg_role="tank",
        jungian_default="hero",
        prime_requisite="STRENGTH",
        minimum_score=9,
        kit_table="guardian_kit",
        warrior=True,
    )
    sage = ClassDef(
        id="sage",
        display_name="Sage",
        rpg_role="caster",
        jungian_default="sage",
        prime_requisite="INTELLIGENCE",
        minimum_score=9,
        kit_table="sage_kit",
        warrior=False,
    )
    pack.classes = [guardian, sage]
    return pack


def _make_snapshot_and_encounter(
    attacker: str,
    opponent: str,
    *,
    char_class: str = "Guardian",
    level: int = _ATTACKER_LEVEL,
):
    """Snapshot + StructuredEncounter for a wwn strike test.

    ``char_class`` controls which ClassDef the attacker resolves to (and
    therefore whether is_warrior fires). ``level`` is seeded on the attacker's
    CreatureCore so ``actor_core.level`` is correct for the KB formula.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    atk_core = CreatureCore(
        name=attacker,
        description="A grizzled warrior.",
        personality="resolute",
        inventory=Inventory(),
        hp={"current": 20, "max": 20, "base_max": 20},
        level=level,
    )
    attacker_char = Character(
        core=atk_core,
        char_class=char_class,
        race="Human",
        backstory="Carved from hard stone.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="A foe.",
        personality="aggressive",
        inventory=Inventory(),
        hp={"current": _OPPONENT_HP, "max": _OPPONENT_HP, "base_max": _OPPONENT_HP},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(attacker_char)
    snap.npcs.append(Npc(core=opp_core))

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=attacker, role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    return snap, enc


def _drive_strike(
    *,
    snap,
    enc,
    pack,
    attacker: str,
    request_id: str,
    round_number: int,
    face: int = 20,
):
    """Drive one 'slash' beat through REAL dispatch_dice_throw.

    face=20 → guaranteed HIT (strike-damage path fires).
    face=1  → guaranteed MISS (Shock path fires if weapon has shock rating).
    """
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    return dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id=request_id,
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[face],
            beat_id="slash",
        ),
        rolling_player_id=f"player-{attacker.lower()}",
        character_name=attacker,
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="elemental_harmony",
        session_id="wwn-killing-blow-dispatch",
        round_number=round_number,
        room_broadcast=[].append,
        snapshot=snap,
    )


# ---------------------------------------------------------------------------
# Test 1 — WWN Warrior strike carries the Killing Blow bonus
# ---------------------------------------------------------------------------


def test_wwn_warrior_strike_emits_killing_blow_span(otel_capture):
    """A wwn Warrior's HIT emits the wwn.killing_blow span with the correct bonus.

    level=4, divisor=2 → bonus=2. The span proves the rider wired in.
    """
    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Torvin", "Boneyard", char_class="Guardian")

    _drive_strike(
        snap=snap,
        enc=enc,
        pack=pack,
        attacker="Torvin",
        request_id="kb-warrior-hit",
        round_number=1,
        face=20,
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.killing_blow" in span_names, (
        f"wwn.killing_blow span must fire when a WWN Warrior strikes; got spans: {span_names}"
    )
    # Assert correct bonus in span attributes.
    kb_spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.killing_blow"]
    assert len(kb_spans) == 1
    attrs = dict(kb_spans[0].attributes or {})
    assert attrs["bonus"] == _EXPECTED_KB_BONUS, (
        f"killing_blow bonus must be ceil({_ATTACKER_LEVEL}/{_KB_DIVISOR})={_EXPECTED_KB_BONUS}; "
        f"got {attrs.get('bonus')}"
    )


def test_wwn_warrior_strike_adds_bonus_to_hp_damage(otel_capture, monkeypatch):
    """A wwn Warrior's HIT deals extra HP damage equal to the Killing Blow bonus.

    We compare the target's HP loss against a non-Warrior baseline. The
    Warrior must drain the target MORE (by exactly the KB bonus).

    Determinism: both invocations use the same fixed damage roll (1d6=3) by
    monkeypatching ``sidequest.server.dispatch.damage_roll.random.randint``.
    With a fixed base roll the difference must be exactly the KB bonus.
    """
    # Fix the 1d6 damage roll to always return 3.
    monkeypatch.setattr(
        "sidequest.server.dispatch.damage_roll.random.randint",
        lambda a, b: 3,
    )

    # Warrior strike.
    pack_warrior = _make_wwn_pack()
    snap_w, enc_w = _make_snapshot_and_encounter("Torvin", "Boneyard", char_class="Guardian")
    target_w = snap_w.find_creature_core("Boneyard")
    assert target_w is not None
    hp_before_w = target_w.hp.current

    _drive_strike(
        snap=snap_w,
        enc=enc_w,
        pack=pack_warrior,
        attacker="Torvin",
        request_id="kb-hp-warrior",
        round_number=1,
        face=20,
    )
    warrior_dmg = hp_before_w - target_w.hp.current

    # Non-Warrior strike (Sage class, warrior=False).
    pack_sage = _make_wwn_pack()
    snap_s, enc_s = _make_snapshot_and_encounter("Mira", "Boneyard", char_class="Sage")
    target_s = snap_s.find_creature_core("Boneyard")
    assert target_s is not None
    hp_before_s = target_s.hp.current

    _drive_strike(
        snap=snap_s,
        enc=enc_s,
        pack=pack_sage,
        attacker="Mira",
        request_id="kb-hp-nonwarrior",
        round_number=1,
        face=20,
    )
    non_warrior_dmg = hp_before_s - target_s.hp.current

    # The damage difference must be exactly the KB bonus.
    assert warrior_dmg - non_warrior_dmg == _EXPECTED_KB_BONUS, (
        f"WWN Warrior must deal exactly {_EXPECTED_KB_BONUS} more damage than a "
        f"non-Warrior on a HIT; warrior_dmg={warrior_dmg}, non_warrior_dmg={non_warrior_dmg}"
    )


# ---------------------------------------------------------------------------
# Test 2 — WWN NON-Warrior: no span, no bonus
# ---------------------------------------------------------------------------


def test_wwn_non_warrior_strike_no_killing_blow_span(otel_capture):
    """A wwn non-Warrior's strike must NOT emit wwn.killing_blow."""
    pack = _make_wwn_pack()
    snap, enc = _make_snapshot_and_encounter("Mira", "Boneyard", char_class="Sage")

    _drive_strike(
        snap=snap,
        enc=enc,
        pack=pack,
        attacker="Mira",
        request_id="kb-nonwarrior",
        round_number=1,
        face=20,
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.killing_blow" not in span_names, (
        f"wwn.killing_blow span must NOT fire for a wwn non-Warrior; got spans: {span_names}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Non-wwn pack: no span, no bonus
# ---------------------------------------------------------------------------


def _make_native_pack():
    """A minimal native-ruleset pack with a strike beat.

    The ClassDef still has warrior=True to prove the gate checks the PACK
    ruleset first, not the class flag alone.
    """
    from sidequest.genre.models.character import ClassDef
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
    )

    strike_beat = BeatDef.model_validate(
        {
            "id": "slash",
            "label": "Slash",
            "kind": "strike",
            "base": 2,
            "stat_check": "STRENGTH",
            "damage_channel": "strike",
            "effect": "A strike.",
            "narrator_hint": "Steel rings.",
            "damage_override": DamageSpec(dice="1d6"),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Battle",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="native",
        ability_score_names=[
            "STRENGTH",
            "DEXTERITY",
            "CONSTITUTION",
            "INTELLIGENCE",
            "WISDOM",
            "CHARISMA",
        ],
        confrontations=[cdef],
    )
    pack.inventory = None

    # Warrior class — even with warrior=True the gate should not fire (wrong ruleset).
    fighter = ClassDef(
        id="fighter",
        display_name="Fighter",
        rpg_role="tank",
        jungian_default="hero",
        prime_requisite="STRENGTH",
        minimum_score=9,
        kit_table="fighter_kit",
        warrior=True,
    )
    pack.classes = [fighter]
    return pack


def test_non_wwn_warrior_no_killing_blow_span(otel_capture):
    """A native-pack Warrior (warrior=True) must NOT emit wwn.killing_blow."""
    pack = _make_native_pack()

    # Use the native pack's stat names for the character.
    atk_stats = {
        s: 12
        for s in ["STRENGTH", "DEXTERITY", "CONSTITUTION", "INTELLIGENCE", "WISDOM", "CHARISMA"]
    }
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    atk_core = CreatureCore(
        name="Aldric",
        description="A warrior",
        personality="bold",
        inventory=Inventory(),
        hp={"current": 20, "max": 20, "base_max": 20},
        level=4,
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Fighter",
        race="Human",
        backstory="Soldier.",
        stats=dict(atk_stats),
    )
    opp_core = CreatureCore(
        name="Foe",
        description="A foe",
        personality="hostile",
        inventory=Inventory(),
        hp={"current": 20, "max": 20, "base_max": 20},
        armor_class=10,
    )

    snap = GameSnapshot(
        genre_slug="caverns_and_claudes", world_slug="test", turn_manager=TurnManager()
    )
    snap.characters.append(attacker_char)
    snap.npcs.append(Npc(core=opp_core))

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name="Aldric", role="combatant", side="player"),
            EncounterActor(name="Foe", role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="kb-native",
            throw_params=ThrowParams(velocity=(0, 5, -2), angular=(1, 1, 1), position=(0.5, 0.5)),
            face=[20],
            beat_id="slash",
        ),
        rolling_player_id="player-aldric",
        character_name="Aldric",
        character_stats=dict(atk_stats),
        encounter=enc,
        pack=pack,
        genre_slug="caverns_and_claudes",
        session_id="native-warrior-dispatch",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.killing_blow" not in span_names, (
        f"wwn.killing_blow must NOT fire for a non-wwn pack; got spans: {span_names}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Shock path: WWN Warrior adds Killing Blow bonus to Shock chip
# ---------------------------------------------------------------------------


def test_wwn_warrior_shock_adds_killing_blow_bonus(otel_capture):
    """A wwn Warrior's MISS against a low-AC target applies Killing Blow to Shock.

    Setup: shock=3 chip, opponent AC (10) <= shock_ac (15) → chip applies.
    Killing Blow adds ceil(4/2)=2 more. Target loses 5 HP from a MISS.

    Two invocations compared (Warrior vs Sage). The Warrior's Shock drain must
    exceed the Sage's by exactly the KB bonus. Both hits are MISSES (face=1).

    NOTE: The wwn.killing_blow span fires once per strike path that applies
    the rider. For Shock (miss path) a SEPARATE span fires for the Shock bonus.
    This test asserts exactly 2 wwn.killing_blow spans for the Warrior (one
    per invocation of apply_killing_blow). If Shock is the only path exercised
    here, it should be exactly 1 span.
    """
    # Warrior MISS — Shock + KB bonus.
    pack_warrior = _make_wwn_pack(shock_weapon=True)
    snap_w, enc_w = _make_snapshot_and_encounter("Torvin", "Boneyard", char_class="Guardian")
    target_w = snap_w.find_creature_core("Boneyard")
    assert target_w is not None
    hp_before_w = target_w.hp.current

    _drive_strike(
        snap=snap_w,
        enc=enc_w,
        pack=pack_warrior,
        attacker="Torvin",
        request_id="kb-shock-warrior",
        round_number=1,
        face=1,  # MISS
    )
    warrior_shock_dmg = hp_before_w - target_w.hp.current

    # Non-Warrior MISS — plain Shock chip, no KB bonus.
    pack_sage = _make_wwn_pack(shock_weapon=True)
    snap_s, enc_s = _make_snapshot_and_encounter("Mira", "Boneyard", char_class="Sage")
    target_s = snap_s.find_creature_core("Boneyard")
    assert target_s is not None
    hp_before_s = target_s.hp.current

    _drive_strike(
        snap=snap_s,
        enc=enc_s,
        pack=pack_sage,
        attacker="Mira",
        request_id="kb-shock-nonwarrior",
        round_number=1,
        face=1,  # MISS
    )
    non_warrior_shock_dmg = hp_before_s - target_s.hp.current

    assert warrior_shock_dmg - non_warrior_shock_dmg == _EXPECTED_KB_BONUS, (
        f"WWN Warrior Shock must carry the Killing Blow bonus "
        f"(+{_EXPECTED_KB_BONUS}); Warrior took {warrior_shock_dmg}, "
        f"Sage took {non_warrior_shock_dmg}"
    )

    # At least one wwn.killing_blow span must have fired (for the Shock path).
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.killing_blow" in span_names, (
        f"wwn.killing_blow span must fire on the WWN Warrior Shock path; got spans: {span_names}"
    )
