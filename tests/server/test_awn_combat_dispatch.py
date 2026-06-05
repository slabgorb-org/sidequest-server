"""AWN combat dispatch — production-path wiring proof (Story 88-1, §11.4 Test 4).

AWN combat IS CWN combat (faithful SRD port): ``AwnRulesetModule`` subclasses
``CwnRulesetModule`` with NO method overrides in Plan 1, so every CWN combat
subsystem — Shock, Trauma, Mortal Injury, ablative HP, opponent reprisal — must
fire IDENTICALLY for an ``awn``-bound pack. This proves it by driving REAL
strikes through the production ``dispatch_dice_throw`` with a synthetic
``ruleset="awn"`` pack and asserting on OTEL spans + HP state — NOT source text
(CLAUDE.md forbids source-grep wiring tests).

This is the integration test that proves the seams are wired:
  * ``get_ruleset_module("awn")`` resolves inside the production dispatcher
    (dice.py:297) — Items 1 & 2. (UnknownRulesetError until registered.)
  * ``pack.rules.ruleset_config()`` returns the awn block — Item 3.
  * The inherited ``cwn.*`` spans fire for AWN: ``cwn.trauma.roll``,
    ``cwn.shock.applied``, ``cwn.mortal_injury.declared``.
  * ``state_patch.hp`` fires and the target's ablative HP depletes (ADR-114).
  * The 0-HP downed seam (``run_cwn_wwn_downed_seam``) runs for AWN — Item 5
    (the slug-string ``ruleset in ("cwn","wwn")`` guard must be taught "awn").
  * Opponent reprisal fires (capability seam dice.py:776 — Item 8 FREE).

Fixture strategy mirrors test_neon_combat_lethality_dispatch.py: a ``MagicMock``
pack whose ``.rules`` is a real ``RulesConfig(ruleset="awn", ...)`` carrying a
``combat`` ConfrontationDef. No content dependency — the awn pack lands in 88-2.

Determinism: the trauma/save dice are rolled via the ``random`` MODULE imported
into ``sidequest.server.dispatch.dice``; we monkeypatch
``sidequest.server.dispatch.dice.random.randint``. The damage faces roll via a
DIFFERENT module (``damage_roll.random``), untouched. ``face=[20]`` guarantees a
hit; ``face=[1]`` guarantees a miss.

``otel_capture`` is provided by tests/server/conftest.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# AWN attribute_map: the six SWN/CWN attributes -> this fixture pack's flavor
# stats. RulesConfig._validate_awn requires all six keys, each flavor declared in
# ability_score_names, and system_strain.max_source a key of the map.
_ATTRIBUTE_MAP = {
    "STRENGTH": "Brawn",
    "CONSTITUTION": "Grit",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Nerve",
    "CHARISMA": "Presence",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())

# Strike beat's stat_check is "Reflex" (DEXTERITY flavor) so attack_params
# resolves; high enough that face=20 beats AC.
_STATS = {name: 12 for name in _ABILITY_SCORE_NAMES}

_OPPONENT_AC = 12


# ---------------------------------------------------------------------------
# Synthetic awn-bound pack builders
# ---------------------------------------------------------------------------


def _awn_cfg():
    from sidequest.genre.models.rules import AwnConfig, SystemStrainConfig, TraumaConfig

    return AwnConfig(
        attribute_map=dict(_ATTRIBUTE_MAP),
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        trauma=TraumaConfig(default_trauma_target=6),
    )


def _make_awn_pack():
    """A MagicMock pack whose ``.rules`` is a real awn-bound RulesConfig.

    One strike beat whose ``damage_override`` carries a ``trauma_die`` (so the
    inherited Trauma seam has a die) and ``trauma_target=2`` (validator minimum)
    so a forced-high trauma roll is unambiguously traumatic.
    """
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
            "id": "shoot",
            "label": "Open Fire",
            "kind": "strike",
            "base": 2,
            "stat_check": "Reflex",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Scrap-gun roars across the wastes.",
            "damage_override": DamageSpec(
                dice="1d6",
                trauma_die="1d6",
                trauma_rating=2,
                trauma_target=2,
            ),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Wasteland Firefight",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
        # Opponent ability scores for the downed-actor Physical save.
        opponent_default_stats={name: 12 for name in _ABILITY_SCORE_NAMES},
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="awn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        awn=_awn_cfg(),
    )
    pack.inventory = None
    return pack


def _make_awn_shock_pack():
    """Like _make_awn_pack but the strike weapon carries a Shock rating.

    CWN/AWN "Shock X/AC Y": ``shock=4`` chip amount X, ``shock_ac=15`` Melee-AC
    ceiling Y. Opponent AC (12) <= shock_ac (15) so the chip (4) applies on a MISS.
    """
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
            "id": "shoot",
            "label": "Open Fire",
            "kind": "strike",
            "base": 2,
            "stat_check": "Reflex",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Scrap-gun roars across the wastes.",
            "damage_override": DamageSpec(dice="1d6", shock=4, shock_ac=15),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Wasteland Firefight",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="awn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        awn=_awn_cfg(),
    )
    pack.inventory = None
    return pack


def _make_awn_reprisal_pack():
    """An awn pack whose combat is win_condition=hp_depletion so the seated
    opponent reprises (server-driven enemy turn, dice.py:776).

    hp_depletion combat requires reserved opponent keys (hp/armor_class/dexterity)
    at LOAD time. The opponent's reprisal strike is the first ``damage_channel:
    strike`` beat (``shoot``). The capability check keys on the
    ``resolve_opponent_attack`` OVERRIDE (inherited from SWN), so AWN gets the
    enemy turn FREE — no slug branch.
    """
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        WinCondition,
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
            "narrator_hint": "Scrap-gun roars across the wastes.",
            "damage_override": DamageSpec(dice="1d6"),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Wasteland Firefight",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        win_condition=WinCondition.hp_depletion,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
        opponent_default_stats={
            **{name: 12 for name in _ABILITY_SCORE_NAMES},
            # Reserved hp_depletion combat keys (required at load).
            "hp": 30,
            "armor_class": _OPPONENT_AC,
            "dexterity": 10,
        },
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="awn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        awn=_awn_cfg(),
    )
    pack.inventory = None
    return pack


# ---------------------------------------------------------------------------
# Snapshot / encounter builders
# ---------------------------------------------------------------------------


def _make_snapshot_and_encounter(attacker: str, opponent: str, *, opponent_hp: int):
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
        description="Wasteland runner.",
        personality="reckless",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        armor_class=12,
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Survivor",
        race="Human",
        backstory="Born under the fallout sky.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="Raider scav.",
        personality="cruel",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
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


def _drive_strike(*, snap, enc, pack, attacker, face, request_id, round_number=1):
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
            face=face,
            beat_id="shoot",
        ),
        rolling_player_id=f"player-{attacker.lower()}",
        character_name=attacker,
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="mutant_wasteland",
        session_id=request_id,
        round_number=round_number,
        room_broadcast=[].append,
        snapshot=snap,
    )


# ---------------------------------------------------------------------------
# Test 1 — registry resolves in production + trauma span + HP depletion
# ---------------------------------------------------------------------------


def test_awn_strike_fires_trauma_span_and_depletes_hp(otel_capture, monkeypatch):
    """A real AWN strike resolves through dispatch_dice_throw: get_ruleset_module
    ('awn') resolves (Items 1/2), ruleset_config() yields the awn block (Item 3),
    the inherited Trauma seam fires cwn.trauma.roll, and ablative HP depletes
    (state_patch.hp). Forces the trauma die LOW (deterministic; the span fires
    regardless of traumatic-ness).
    """
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _make_awn_pack()
    snap, enc = _make_snapshot_and_encounter("Vane", "Scrag", opponent_hp=20)
    target = snap.find_creature_core("Scrag")
    assert target is not None
    assert target.hp.current == target.hp.max  # precondition: full HP

    _drive_strike(snap=snap, enc=enc, pack=pack, attacker="Vane", face=[20], request_id="awn-trauma")

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.trauma.roll" in span_names, (
        f"the inherited cwn.trauma.roll span must fire for an AWN strike with a "
        f"trauma_die — get_ruleset_module('awn') must resolve and AwnRulesetModule "
        f"must inherit resolve_trauma; got spans: {span_names}"
    )
    assert "state_patch.hp" in span_names, (
        f"ADR-114 ablative HP: a state_patch.hp span must fire on the strike; "
        f"got spans: {span_names}"
    )
    assert target.hp.current < target.hp.max, (
        f"the AWN strike must deplete the target's ablative HP pool; "
        f"hp={target.hp.current}/{target.hp.max}"
    )


# ---------------------------------------------------------------------------
# Test 2 — inherited Shock-on-miss (dice.py:341 FREE site)
# ---------------------------------------------------------------------------


def test_awn_shock_chips_hp_on_miss(otel_capture):
    """An AWN melee weapon chips fixed Shock damage on a MISS vs a low-AC target
    (inherited CWN Shock). face=[1] forces a miss; the shock chip applies and
    cwn.shock.applied fires.
    """
    pack = _make_awn_shock_pack()
    snap, enc = _make_snapshot_and_encounter("Vane", "Scrag", opponent_hp=20)
    target = snap.find_creature_core("Scrag")
    assert target is not None
    assert target.hp.current == target.hp.max  # precondition: full HP

    _drive_strike(snap=snap, enc=enc, pack=pack, attacker="Vane", face=[1], request_id="awn-shock")

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.shock.applied" in span_names, (
        f"the inherited cwn.shock.applied span must fire when an AWN strike MISSES "
        f"a low-Melee-AC target with a shock weapon; got spans: {span_names}"
    )
    assert target.hp.current < target.hp.max, (
        f"Shock must chip the target's HP despite the miss; "
        f"hp={target.hp.current}/{target.hp.max}"
    )


# ---------------------------------------------------------------------------
# Test 3 — 0-HP downed seam runs for AWN (Item 5: slug-string fix)
# ---------------------------------------------------------------------------


def test_awn_downed_target_gets_mortal_injury(otel_capture, monkeypatch):
    """A strike that drops an AWN target to 0 HP declares a Mortal Injury.

    This exercises Item 5: ``run_cwn_wwn_downed_seam`` (dice.py:713) is gated by
    the slug-string ``pack.rules.ruleset in ("cwn","wwn")`` at downed_seam.py:128,
    which currently EXCLUDES "awn" → the seam returns early and NO Mortal Injury
    fires. After the fix (isinstance on ruleset_config()), AWN rides the seam and
    cwn.mortal_injury.declared fires.

    Determinism: opponent seeded at hp=1 so one 1d6 strike (face=20) drops it to
    0; trauma die forced LOW so the hit is non-traumatic (no Major Injury path).
    """
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _make_awn_pack()
    snap, enc = _make_snapshot_and_encounter("Vane", "Scrag", opponent_hp=1)
    target = snap.find_creature_core("Scrag")
    assert target is not None

    _drive_strike(snap=snap, enc=enc, pack=pack, attacker="Vane", face=[20], request_id="awn-downed")

    assert target.hp.current == 0, (
        f"precondition for the downed seam: the strike must drop the target to 0 HP; "
        f"hp={target.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.mortal_injury.declared" in span_names, (
        f"an AWN target dropped to 0 HP must declare a Mortal Injury — the downed "
        f"seam's slug-string guard (downed_seam.py:128) must accept 'awn'; "
        f"got spans: {span_names}"
    )
    assert any("Mortal Injury" in s.text for s in target.statuses), (
        f"the downed AWN target must carry a Mortal Injury Scar status; "
        f"statuses={[s.text for s in target.statuses]}"
    )


# ---------------------------------------------------------------------------
# Test 4 — opponent reprisal fires for AWN (dice.py:776 capability FREE site)
# ---------------------------------------------------------------------------


def test_awn_opponent_reprisal_fires(otel_capture):
    """In hp_depletion AWN combat, the seated opponent answers with a server-driven
    attack turn after the player acts (inherited from SWN via the capability check
    on ``resolve_opponent_attack``). The opponent survives the player's strike
    (hp=30) so the encounter is unresolved and the reprisal runs.
    """
    pack = _make_awn_reprisal_pack()
    snap, enc = _make_snapshot_and_encounter("Vane", "Scrag", opponent_hp=30)

    _drive_strike(
        snap=snap, enc=enc, pack=pack, attacker="Vane", face=[20], request_id="awn-reprisal"
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "encounter.opponent_attack_resolved" in span_names, (
        f"AWN must inherit the server-driven opponent reprisal (the capability "
        f"check keys on resolve_opponent_attack, which AwnRulesetModule inherits "
        f"from SWN); got spans: {span_names}"
    )
