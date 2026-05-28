"""CWN Combat Lethality — Trauma seam wiring proof (plan 2026-05-28, Task 9).

Proves the Trauma seam is wired into ``dispatch_dice_throw``'s strike-damage
block (between ``dmg_total = dmg_resolved.total`` and the damage_resolver
lambda) by driving a REAL CWN strike through the production dispatcher and
asserting on OTEL spans + encounter scene state — NOT source text (CLAUDE.md
forbids source-grep wiring tests).

Fixture strategy (no content dependency):
  Neon's combat-lethality content (Tasks 13-14) is NOT authored yet, so these
  tests build a minimal in-memory ``cwn``-bound pack the same way the ADR-114
  space_opera hp e2e (``tests/integration/test_space_opera_hp_e2e.py``) builds
  its synthetic Firefight pack: a ``MagicMock`` pack whose ``.rules`` is a real
  ``RulesConfig(ruleset="cwn", ...)`` carrying a ``combat`` ConfrontationDef.
  The strike beat carries a ``damage_override`` DamageSpec with a ``trauma_die``
  so ``CwnRulesetModule.resolve_trauma`` has a die to roll (Priority-1 damage
  resolution — no inventory/catalog wiring needed).

Determinism of the Trauma roll:
  ``resolve_trauma`` rolls the trauma die via the ``rng`` the seam passes it; in
  production that ``rng`` is the ``random`` MODULE imported into
  ``sidequest.server.dispatch.dice``. We monkeypatch
  ``sidequest.server.dispatch.dice.random.randint`` so the trauma die is a known
  value:
    - Test 1 forces it LOW (the span fires for ANY roll when a trauma_die is
      present, so the value only needs to be deterministic, not traumatic).
    - Test 2 forces it HIGH (>= the Trauma Target) so ``traumatic`` is True and
      the scene tag is recorded.
  The damage faces are rolled by ``generate_server_faces`` in a DIFFERENT module
  (``damage_roll.random``), so patching ``dice.random`` does not perturb them.

Both tests use ``face=[20]`` on the d20 so the attack always beats the
opponent's AC and the strike-damage path fires.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# CWN attribute_map: the six SWN/CWN attributes -> this fixture pack's flavor
# stats. RulesConfig._validate_cwn requires all six keys and that each flavor is
# declared in ability_score_names (and that system_strain.max_source is a key).
_ATTRIBUTE_MAP = {
    "STRENGTH": "Brawn",
    "CONSTITUTION": "Grit",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Nerve",
    "CHARISMA": "Presence",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())

# Stat block for the attacker. The strike beat's stat_check is "Reflex" (the
# DEXTERITY flavor) so attack_params resolves; high enough that face=20 beats AC.
_STATS = {name: 12 for name in _ABILITY_SCORE_NAMES}

_OPPONENT_AC = 12
_OPPONENT_HP = 20


def _make_cwn_pack():
    """A MagicMock pack whose ``.rules`` is a real cwn-bound RulesConfig.

    The ``combat`` ConfrontationDef carries one strike beat whose
    ``damage_override`` is a DamageSpec with a ``trauma_die`` (so the Trauma
    seam has a die) and ``trauma_target`` set to the validator minimum (2) so a
    forced-high trauma roll is unambiguously traumatic in Test 2.
    """
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        CwnConfig,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        SystemStrainConfig,
        TraumaConfig,
    )

    strike_beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Open Fire",
            "kind": "strike",
            "base": 2,
            "stat_check": "Reflex",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Muzzle flash strobes the alley.",
            # Priority-1 damage resolution: spec lives directly on the beat, so
            # resolve_damage returns it without needing inventory/catalog wiring.
            "damage_override": DamageSpec(
                dice="1d6",
                trauma_die="1d6",
                trauma_rating=2,
                trauma_target=2,  # validator minimum (ge=2); Test 2 rolls >= this
            ),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
        # Opponent ability scores so the Task-11 downed seam can resolve a
        # Physical save target if a trauma-multiplied strike happens to drop the
        # opponent to 0 HP within these tests (a complete CWN combat pack carries
        # them; without them the fail-loud downed seam would raise).
        opponent_default_stats={name: 12 for name in _ABILITY_SCORE_NAMES},
    )

    cwn_cfg = CwnConfig(
        attribute_map=dict(_ATTRIBUTE_MAP),
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        trauma=TraumaConfig(default_trauma_target=6),
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        cwn=cwn_cfg,
    )
    # No inventory needed (damage_override is Priority 1), but the resolver reads
    # pack.inventory via getattr — give it an explicit None so the MagicMock
    # doesn't hand back an auto-attribute that confuses catalog logic.
    pack.inventory = None
    return pack


def _make_snapshot_and_encounter(attacker: str, opponent: str):
    """A GameSnapshot + StructuredEncounter shaped like a seated cwn combat.

    Attacker is a player Character with a CreatureCore; opponent is an Npc with
    a known AC/HP so the SWN/CWN attack rolls vs a concrete AC and the strike
    ablates real HP.
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
        description="Street runner.",
        personality="reckless",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Enforcer",
        race="Human",
        backstory="Ran with the franchise gangs.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="Corp security.",
        personality="cold",
        inventory=Inventory(),
        hp={"current": _OPPONENT_HP, "max": _OPPONENT_HP, "base_max": _OPPONENT_HP},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug="neon_dystopia",
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


def _drive_strike(*, snap, enc, pack, attacker: str, request_id: str, round_number: int):
    """Drive one ``shoot`` strike beat through the REAL dispatch_dice_throw.

    face=[20] guarantees the d20 beats the opponent's AC so the strike-damage
    block (and the Trauma seam inside it) fires.
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
            face=[20],
            beat_id="shoot",
        ),
        rolling_player_id=f"player-{attacker.lower()}",
        character_name=attacker,
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="neon_dystopia",
        session_id="cwn-trauma-dispatch",
        round_number=round_number,
        room_broadcast=[].append,
        snapshot=snap,
    )


def test_trauma_span_fires_on_cwn_strike_hit(otel_capture, monkeypatch):
    """The cwn.trauma.roll span fires when a CWN strike resolves damage.

    Determinism: trauma die forced LOW (the span fires regardless of whether the
    hit is traumatic, so the value only needs to be deterministic).
    """
    # Force the trauma die low. dice.random.randint is the rng the seam threads
    # into resolve_trauma; the damage faces use damage_roll.random, untouched.
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _make_cwn_pack()
    snap, enc = _make_snapshot_and_encounter("Razor", "Mr. Vex")

    _drive_strike(
        snap=snap, enc=enc, pack=pack, attacker="Razor",
        request_id="trauma-span", round_number=1,
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.trauma.roll" in span_names, (
        f"cwn.trauma.roll span must fire when a CWN strike (weapon with a "
        f"trauma_die) resolves damage through dispatch_dice_throw; "
        f"got spans: {span_names}"
    )


def test_traumatic_hit_records_scene_tag(otel_capture, monkeypatch):
    """A Traumatic Hit records a scene-wide 'Traumatic Hit Landed' EncounterTag.

    Determinism: trauma die forced HIGH (>= trauma_target=2) so ``traumatic`` is
    True and the seam appends the scene tag.
    """
    # Force the trauma die to its max so the roll meets/exceeds the target.
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: b)

    pack = _make_cwn_pack()
    snap, enc = _make_snapshot_and_encounter("Razor", "Mr. Vex")

    assert not any(t.text == "Traumatic Hit Landed" for t in enc.tags), (
        "precondition: no Traumatic Hit tag before the strike"
    )

    _drive_strike(
        snap=snap, enc=enc, pack=pack, attacker="Razor",
        request_id="trauma-tag", round_number=2,
    )

    assert any(t.text == "Traumatic Hit Landed" for t in enc.tags), (
        f"a Traumatic Hit must record a scene-wide 'Traumatic Hit Landed' tag on "
        f"encounter.tags so a later 0-HP drop can gate Major Injury; "
        f"got tags={[t.text for t in enc.tags]}"
    )
    # Spans still prove the seam ran on the traumatic path.
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.trauma.roll" in span_names

    # The seam de-dups: a second traumatic strike in the same scene must NOT
    # append a duplicate tag.
    _drive_strike(
        snap=snap, enc=enc, pack=pack, attacker="Razor",
        request_id="trauma-tag-2", round_number=3,
    )
    tagged = [t for t in enc.tags if t.text == "Traumatic Hit Landed"]
    assert len(tagged) == 1, (
        f"the Traumatic Hit scene tag must be recorded once per scene, not per "
        f"strike; got {len(tagged)} copies"
    )
    tag = tagged[0]
    assert tag.created_by == "Razor"
    assert tag.target is None
    assert tag.fleeting is False


# ---------------------------------------------------------------------------
# Task 10 — Shock-on-miss seam
# ---------------------------------------------------------------------------


def _make_cwn_shock_pack():
    """Like _make_cwn_pack but the strike weapon carries a Shock rating.

    ``shock=12`` is both the chip amount and the AC ceiling (v1 models them as
    the same content number). The opponent's Melee AC is _OPPONENT_AC (12), so
    ``target_melee_ac (12) <= shock (12)`` and the chip applies on a MISS.
    No trauma_die — Shock is a fixed chip on a miss, independent of the Trauma
    seam (which only fires on a HIT that resolves damage).
    """
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        CwnConfig,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        SystemStrainConfig,
        TraumaConfig,
    )

    strike_beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Open Fire",
            "kind": "strike",
            "base": 2,
            "stat_check": "Reflex",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Muzzle flash strobes the alley.",
            "damage_override": DamageSpec(
                dice="1d6",
                shock=12,  # chip amount AND AC ceiling; opponent AC (12) <= shock
            ),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
    )

    cwn_cfg = CwnConfig(
        attribute_map=dict(_ATTRIBUTE_MAP),
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        trauma=TraumaConfig(default_trauma_target=6),
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        cwn=cwn_cfg,
    )
    pack.inventory = None
    return pack


def test_shock_chips_hp_on_miss(otel_capture):
    """A CWN melee weapon chips fixed Shock damage on a MISS vs a low-AC target.

    face=[1] forces the d20 to miss the opponent's AC, so the HIT path's
    damage block is skipped. The new miss branch resolves the weapon spec,
    sees shock=12 >= the opponent's Melee AC (12), and chips 12 HP. The
    cwn.shock.applied span fires and the target's HP drops despite the miss.
    """
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    pack = _make_cwn_shock_pack()
    snap, enc = _make_snapshot_and_encounter("Razor", "Mr. Vex")
    target_core = snap.find_creature_core("Mr. Vex")
    assert target_core is not None
    assert target_core.hp.current == target_core.hp.max  # precondition: full HP

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="shock-miss",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[1],  # MISS
            beat_id="shoot",
        ),
        rolling_player_id="player-razor",
        character_name="Razor",
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="neon_dystopia",
        session_id="cwn-shock-dispatch",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.shock.applied" in span_names, (
        f"cwn.shock.applied span must fire when a CWN strike MISSES vs a "
        f"low-Melee-AC target with a shock weapon; got spans: {span_names}"
    )
    assert target_core.hp.current < target_core.hp.max, (
        f"Shock must chip the target's HP despite the miss; "
        f"hp={target_core.hp.current}/{target_core.hp.max}"
    )


# ---------------------------------------------------------------------------
# Task 11 — 0-HP downed seam (Mortal Injury always; Major Injury after Trauma)
# ---------------------------------------------------------------------------


def _make_cwn_downed_pack():
    """Like _make_cwn_pack but the cdef carries opponent_default_stats.

    The downed seam computes the Physical-save target via
    ``ruleset.save_params(stats=<downed actor stats>, ...)``. CreatureCore/Npc
    carry no ability scores, so the opponent's scores must come from the
    confrontation's ``opponent_default_stats`` (the same source dispatch uses
    for the d20/initiative paths). All six flavor stats are present so the
    Physical save (STRENGTH/CONSTITUTION → Brawn/Grit) resolves.

    win_condition stays the default ``dial_threshold`` so no reserved-key
    (hp/armor_class/dexterity) validation fires — the opponent's 0-HP drop is
    driven by the strike ablating its seeded Npc.core.hp pool, not by a
    content HP key.
    """
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        CwnConfig,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        SystemStrainConfig,
        TraumaConfig,
    )

    strike_beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Open Fire",
            "kind": "strike",
            "base": 2,
            "stat_check": "Reflex",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Muzzle flash strobes the alley.",
            "damage_override": DamageSpec(
                dice="1d6",
                trauma_die="1d6",
                trauma_rating=2,
                trauma_target=2,  # validator minimum; a forced-high trauma roll meets it
            ),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Firefight",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
        # Ability scores for the downed-actor save (Physical → Brawn/Grit).
        opponent_default_stats={name: 12 for name in _ABILITY_SCORE_NAMES},
    )

    cwn_cfg = CwnConfig(
        attribute_map=dict(_ATTRIBUTE_MAP),
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        trauma=TraumaConfig(default_trauma_target=6),
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        cwn=cwn_cfg,
    )
    pack.inventory = None
    return pack


def _make_downed_snapshot_and_encounter(attacker: str, opponent: str, *, opponent_hp: int):
    """Like _make_snapshot_and_encounter but seats the opponent with low HP.

    ``opponent_hp=1`` means a single 1d6 strike drops the opponent to 0, firing
    the Task-11 downed seam on the same strike that lands.
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
        description="Street runner.",
        personality="reckless",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Enforcer",
        race="Human",
        backstory="Ran with the franchise gangs.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="Corp security.",
        personality="cold",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug="neon_dystopia",
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


def test_downed_target_gets_mortal_injury(otel_capture, monkeypatch):
    """A strike that drops a CWN target to 0 HP always declares a Mortal Injury.

    Determinism: opponent seeded at hp=1 so the 1d6 strike (face=20 hits) drops
    it to 0. The trauma die is forced LOW (randint -> a) so the hit is NOT
    traumatic (1 < trauma_target=2) — no scene tag, no Major Injury save. The
    Mortal Injury must still fire (it's unconditional at 0 HP).
    """
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _make_cwn_downed_pack()
    snap, enc = _make_downed_snapshot_and_encounter("Razor", "Mr. Vex", opponent_hp=1)
    target_core = snap.find_creature_core("Mr. Vex")

    _drive_strike(
        snap=snap, enc=enc, pack=pack, attacker="Razor",
        request_id="downed-mortal", round_number=1,
    )

    assert target_core.hp.current == 0, (
        f"precondition for the seam: the strike must drop the target to 0 HP; "
        f"hp={target_core.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.mortal_injury.declared" in span_names, (
        f"a CWN target dropped to 0 HP must declare a Mortal Injury; "
        f"got spans: {span_names}"
    )
    assert any("Mortal Injury" in s.text for s in target_core.statuses), (
        f"the downed target must carry a Mortal Injury Scar status; "
        f"statuses={[s.text for s in target_core.statuses]}"
    )
    # Non-traumatic hit: no Major Injury save should have rolled.
    assert "cwn.major_injury.roll" not in span_names, (
        "a non-traumatic 0-HP drop must NOT roll Major Injury (no scene tag)"
    )


def test_downed_after_traumatic_hit_rolls_major(otel_capture, monkeypatch):
    """A 0-HP drop on a Traumatic Hit rolls the Major Injury table on a failed save.

    Determinism: the SAME strike is both traumatic AND lethal.
      - trauma die (1d6): forced HIGH (randint(1,6) -> 6 >= trauma_target=2) so
        the hit is traumatic and the 'Traumatic Hit Landed' scene tag is set
        before the downed seam reads it.
      - Physical save (1d20): forced LOW (randint(1,20) -> 1) so the save FAILS
        and the Major Injury 1d12 roll fires.
    A single lambda keyed on the upper bound returns 6 for the 1d6 trauma roll
    and 1 for the 1d20 save (and 1 for the 1d12 major roll). The 1d6 damage
    rolls via the OTHER module's rng (damage_roll.random), untouched, so hp=1
    still drops to 0.
    """
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint",
        lambda a, b: b if b <= 6 else a,
    )

    pack = _make_cwn_downed_pack()
    snap, enc = _make_downed_snapshot_and_encounter("Razor", "Mr. Vex", opponent_hp=1)
    target_core = snap.find_creature_core("Mr. Vex")

    _drive_strike(
        snap=snap, enc=enc, pack=pack, attacker="Razor",
        request_id="downed-major", round_number=1,
    )

    assert target_core.hp.current == 0, (
        f"precondition: the traumatic strike must drop the target to 0 HP; "
        f"hp={target_core.hp.current}"
    )
    assert any(t.text == "Traumatic Hit Landed" for t in enc.tags), (
        "precondition: a Traumatic Hit must have landed this scene so the "
        "downed seam gates Major Injury on it"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.mortal_injury.declared" in span_names, (
        f"Mortal Injury still fires on every 0-HP drop; got spans: {span_names}"
    )
    assert "cwn.major_injury.roll" in span_names, (
        f"a 0-HP drop after a Traumatic Hit with a FAILED Physical save must "
        f"roll the Major Injury table; got spans: {span_names}"
    )
    assert any("Major Injury" in s.text for s in target_core.statuses), (
        f"the downed target must carry a Major Injury Scar status; "
        f"statuses={[s.text for s in target_core.statuses]}"
    )
