"""road_warrior CWN combat — production-path wiring proof (Story 86-1 Plan 1, §6.4).

The mandatory OTEL/integration wiring test the design demands: prove the bound
``cwn`` ruleset is reachable on a REAL road_warrior turn, not just unit-tested in
isolation and not improvised by the narrator. We load the ACTUAL road_warrior
genre pack from sidequest-content, seat a combat encounter built from its own
``combat`` confrontation, and drive a real strike through the production
``dispatch_dice_throw`` — then assert on OTEL spans + HP state (NOT source text;
CLAUDE.md forbids source-grep wiring tests).

What this proves that the synthetic neon/awn module tests cannot:
  * road_warrior's OWN content + binding routes a strike through ``CwnRulesetModule``
    (``get_ruleset_module(pack.rules.ruleset)`` resolves to the CWN module in the
    production dispatcher).
  * Ablative HP (ADR-114) depletes on the strike channel — ``state_patch.hp`` fires.
  * The CWN 0-HP downed seam runs for road_warrior — ``cwn.mortal_injury.declared``
    fires when a strike drops a target to 0 HP. This is the cwn-specific
    lie-detector span on a real road_warrior turn; it needs no trauma/shock weapon
    authoring, only that the pack is cwn-bound and the combat strike beat carries
    ``damage_channel: strike``.

RED until Plan 1 lands: road_warrior currently runs the ``native`` engine and its
combat strike beats carry ``damage_channel: none`` (dial momentum only), so no HP
depletes, no downed seam runs, and the cwn spans never fire.

Determinism: the trauma/save dice roll via the ``random`` MODULE imported into
``sidequest.server.dispatch.dice``; we monkeypatch
``sidequest.server.dispatch.dice.random.randint`` LOW so the downed roll is
non-traumatic (clean Mortal Injury, no Major-Injury branch). Damage faces roll via
a DIFFERENT module (``damage_roll.random``), untouched. ``face=[20]`` guarantees
the d20 beats the (low) opponent AC so the strike-damage path fires.

``otel_capture`` is provided by tests/server/conftest.py.
"""

from __future__ import annotations

import pytest

from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_WORLD = "the_circuit"  # the live road_warrior world (personal weapons live here, 114-14)

_OPPONENT_AC = 8  # low so face=20 beats it under either native or cwn attack math

# Story 114-13: the personal-weapon guard widens to accept the verbatim CWN
# weapon categories (114-5) alongside the legacy bespoke "weapon" string. A
# personal weapon is any of these categories that is NOT a mounted/rig weapon.
_PERSONAL_WEAPON_CATEGORIES = frozenset({"weapon", "melee_weapon", "ranged_weapon"})


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_road_warrior() -> GenrePack:
    try:
        return load_genre_pack(find_pack_path("road_warrior"))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _combat_confrontation(pack: GenrePack):
    combat = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert combat is not None, "road_warrior must declare a 'combat' confrontation"
    return combat


def _strike_beat(combat):
    """The combat's HP-dealing strike beat.

    Prefer a beat whose ``damage_channel`` is ``strike`` (the ADR-114 HP channel
    Plan 1 must set on the attack beat); fall back to the first ``kind: strike``
    beat so the test still drives *something* (and stays RED) pre-migration.
    """

    def _dc(beat) -> str:
        dc = getattr(beat, "damage_channel", None)
        return dc.value if hasattr(dc, "value") else str(dc)

    def _kind(beat) -> str:
        k = beat.kind
        return k.value if hasattr(k, "value") else str(k)

    beat = next((b for b in combat.beats if _dc(b) == "strike"), None)
    if beat is None:
        beat = next((b for b in combat.beats if _kind(b) == "strike"), None)
    assert beat is not None, (
        "road_warrior combat must expose a strike beat for the player to attack with; "
        f"beat kinds: {[_kind(b) for b in combat.beats]}"
    )
    return beat


def _attacker_stats(pack: GenrePack) -> dict[str, int]:
    # Build a stat block keyed by whatever the sheet currently uses (flavor names
    # pre-migration, the standard six post-migration), high enough to land hits.
    return {name: 14 for name in pack.rules.ability_score_names}


def _make_snapshot_and_encounter(
    pack: GenrePack, attacker: str, opponent: str, *, opponent_hp: int
):
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

    # The attacker carries the REAL road_warrior starting weapon (the Wheelman's
    # pistol) by id with NO inline damage spec, so the strike must resolve its
    # DamageSpec from the pack's own catalog (Priority 3 in
    # resolve_damage_spec_from_beat_and_actor — CatalogItem.damage). This drives
    # the real-content path the Reviewer flagged: if no personal weapon authors a
    # `damage:` spec, this resolves to None → damage_spec_missing → HP never
    # depletes → these tests go RED. The pistol's authored damage (1d6, trauma_die
    # 1d8) deals 1-6 — enough to drop a 1-HP opponent and to leave a 20-HP opponent
    # alive but dented. The trauma die rolls via the monkeypatched dice.random (=1)
    # vs the cfg default Trauma Target (6) → non-traumatic, clean Mortal Injury.
    weapon = {"id": "pistol", "name": "Pistol"}
    atk_core = CreatureCore(
        name=attacker,
        description="Wheelman of the Circuit.",
        personality="reckless",
        inventory=Inventory(items=[weapon]),
        hp={"current": 12, "max": 12, "base_max": 12},
        armor_class=12,
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Wheelman",
        race="Human",
        backstory="Raised on the ring road.",
        stats=_attacker_stats(pack),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="Chrome-masked raider.",
        personality="cruel",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug="road_warrior",
        world_slug="the_circuit",
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


def _drive_strike(*, snap, enc, pack, attacker, beat_id, face, request_id, round_number=1):
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
            beat_id=beat_id,
        ),
        rolling_player_id=f"player-{attacker.lower()}",
        character_name=attacker,
        character_stats=_attacker_stats(pack),
        encounter=enc,
        pack=pack,
        genre_slug="road_warrior",
        session_id=request_id,
        round_number=round_number,
        room_broadcast=[].append,
        snapshot=snap,
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_binds_cwn_module_in_production_registry() -> None:
    """The bound ruleset resolves to the CWN module through the same registry the
    production dispatcher uses — the binding is reachable, not just declared."""
    pack = _load_road_warrior()
    module = get_ruleset_module(pack.rules.ruleset)
    assert isinstance(module, CwnRulesetModule), (
        "road_warrior's bound ruleset must resolve to CwnRulesetModule in the "
        f"production registry; got {type(module).__name__} for slug {pack.rules.ruleset!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_strike_depletes_ablative_hp_on_real_turn(otel_capture, monkeypatch) -> None:
    """A real road_warrior combat strike, driven through dispatch_dice_throw,
    ablates the target's HP pool (ADR-114) and emits state_patch.hp — proving the
    cwn hp_depletion combat path is reachable from the production turn path with
    the real pack's own content."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _load_road_warrior()
    combat = _combat_confrontation(pack)
    beat = _strike_beat(combat)

    snap, enc = _make_snapshot_and_encounter(pack, "Vex", "Scrag", opponent_hp=20)
    target = snap.find_creature_core("Scrag")
    assert target is not None
    assert target.hp.current == target.hp.max  # precondition: full HP

    _drive_strike(
        snap=snap,
        enc=enc,
        pack=pack,
        attacker="Vex",
        beat_id=beat.id,
        face=[20],
        request_id="rw-ablative",
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "state_patch.hp" in span_names, (
        "a real road_warrior strike must emit the ADR-114 state_patch.hp span "
        f"(the combat strike beat must use damage_channel: strike); got spans: {span_names}"
    )
    assert target.hp.current < target.hp.max, (
        "the strike must deplete the target's ablative HP pool; "
        f"hp={target.hp.current}/{target.hp.max}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_downed_target_gets_mortal_injury(otel_capture, monkeypatch) -> None:
    """A real road_warrior strike that drops a target to 0 HP runs the CWN
    downed seam and declares a Mortal Injury — the cwn-specific lie-detector span
    firing on a real road_warrior turn. RED while the pack is native (the seam is
    gated on a cwn/wwn binding) or while the strike beat deals no HP.

    Opponent seeded at hp=1 so one 1d6 strike (faces forced low -> 1 damage) drops
    it to 0; trauma die forced low so the hit is non-traumatic (no Major-Injury
    branch)."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _load_road_warrior()
    combat = _combat_confrontation(pack)
    beat = _strike_beat(combat)

    snap, enc = _make_snapshot_and_encounter(pack, "Vex", "Scrag", opponent_hp=1)
    target = snap.find_creature_core("Scrag")
    assert target is not None

    _drive_strike(
        snap=snap,
        enc=enc,
        pack=pack,
        attacker="Vex",
        beat_id=beat.id,
        face=[20],
        request_id="rw-downed",
    )

    assert target.hp.current == 0, (
        "precondition for the downed seam: the strike must drop the target to 0 HP; "
        f"hp={target.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.mortal_injury.declared" in span_names, (
        "a road_warrior target dropped to 0 HP must declare a Mortal Injury — the "
        "CWN downed seam must run for the cwn-bound road_warrior pack on a real "
        f"turn; got spans: {span_names}"
    )
    assert any("Mortal Injury" in s.text for s in target.statuses), (
        "the downed target must carry a Mortal Injury status; "
        f"statuses={[s.text for s in target.statuses]}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_personal_weapons_carry_damage_specs() -> None:
    """Every PERSONAL weapon in the road_warrior catalog must author a ``damage``
    spec — the content half of the hp_depletion combat the engine seam serves.

    This is the regression lock for the Reviewer's HIGH finding (Story 86-1 Plan 1):
    the combat confrontation is ``win_condition: hp_depletion`` and its strike beats
    carry ``damage_channel: strike`` with no ``damage_override``, so damage resolves
    from the actor's weapon (resolve_damage_spec_from_beat_and_actor Priority 3 →
    CatalogItem.damage). A personal weapon with no ``damage`` spec resolves to None →
    ``damage_spec_missing`` → the strike lands in prose but moves no HP (the exact
    "narrator improvising combat with nothing underneath" failure the story exists to
    kill). Mounted/rig weapons (vehicular, Plan 2) are excluded — they are not the
    actor's personal strike weapon in Plan 1.

    This guard fails the suite if a future edit ships a personal weapon without
    damage, so the gap a seeded-weapon integration test once masked can never pass
    green again.
    """
    pack = _load_road_warrior()
    # Personal weapons moved to the world tier (ADR-145 D3 / 114-14) — bespoke gear
    # is world-tier. They resolve for the live world via the non-droppable merge;
    # check the RESOLVED catalog, the tier the player actually plays in.
    resolved = resolve_inventory(pack, _WORLD)
    assert resolved is not None
    catalog = resolved.item_catalog
    personal_weapons = [
        item
        for item in catalog
        if item.category in _PERSONAL_WEAPON_CATEGORIES and "mounted" not in item.tags
    ]
    assert personal_weapons, "road_warrior must declare personal weapons in its resolved catalog"

    missing = [item.id for item in personal_weapons if item.damage is None]
    assert not missing, (
        "every personal weapon must author a `damage:` spec so the hp_depletion "
        f"combat actually depletes HP; missing damage on: {missing}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_road_warrior_combat_classes_start_with_a_personal_weapon() -> None:
    """Every class whose combat beat menu includes a strike beat (``shoot`` /
    ``pistol_whip``) must start with a personal weapon that authors a ``damage``
    spec — otherwise it is unarmed in a lethal genre and deals 0 HP even after
    the catalog gains damage specs (Reviewer HIGH finding #2: Chrome Saint has
    shoot/pistol_whip beats but no gun).
    """
    pack = _load_road_warrior()
    # Personal weapons + kits resolve at the world tier (ADR-145 D3 / 114-14): the
    # bespoke weapons live in worlds/the_circuit and the kits are world-replaces.
    resolved = resolve_inventory(pack, _WORLD)
    assert resolved is not None
    catalog_by_id = {item.id: item for item in resolved.item_catalog}
    starting = resolved.starting_equipment

    strike_beats = {"shoot", "pistol_whip"}
    classes_needing_a_weapon = [
        cls.display_name
        for cls in pack.classes
        if strike_beats & set(getattr(cls, "encounter_beat_choices", []) or [])
    ]
    assert classes_needing_a_weapon, (
        "expected at least one road_warrior class to have a strike beat (shoot/pistol_whip)"
    )

    unarmed = []
    for class_name in classes_needing_a_weapon:
        loadout = starting.get(class_name, [])
        has_damaging_weapon = any(
            (item := catalog_by_id.get(item_id)) is not None
            and item.category in _PERSONAL_WEAPON_CATEGORIES
            and "mounted" not in item.tags
            and item.damage is not None
            for item_id in loadout
        )
        if not has_damaging_weapon:
            unarmed.append(class_name)
    assert not unarmed, (
        "every combat class with a strike beat must start with a damaging personal "
        f"weapon; these start unarmed: {unarmed}"
    )
