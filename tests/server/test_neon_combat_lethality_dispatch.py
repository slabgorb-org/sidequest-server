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
