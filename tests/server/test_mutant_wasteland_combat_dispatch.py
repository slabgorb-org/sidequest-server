"""mutant_wasteland AWN combat — production-path wiring proof (Story 88-2, §6.5).

The mandatory OTEL/integration wiring test design §6.5 demands: prove the bound
`awn` ruleset is reachable on a REAL mutant_wasteland turn, fires the inherited
`cwn.*` lethality spans, and actually depletes ablative HP — not improvised by the
narrator with nothing mechanical underneath. This is the absence Sebastien and Jade
named after the broken-engine coyote_star session, applied to the wastes.

This mirrors `tests/server/test_road_warrior_combat_dispatch.py` verbatim in shape
(the road_warrior→CWN precedent): load the ACTUAL pack from sidequest-content, seat
a combat encounter from its own combat confrontation, drive a real strike through
the production `dispatch_dice_throw`, and assert on OTEL spans + HP state — NOT on
source text (server CLAUDE.md forbids source-grep wiring tests).

What this proves that the synthetic awn-module tests (88-1) cannot: mutant_wasteland's
OWN content + binding routes a strike through the AWN module (which IS a
CwnRulesetModule), ablative HP depletes (`state_patch.hp`), and the 0-HP downed seam
declares a Mortal Injury (`cwn.mortal_injury.declared`) on a real wasteland turn.

RED until Story 88-2 content lands: mutant_wasteland runs the `native` engine today,
its combat is momentum/`opposed_check`, and NO personal weapon authors a `damage`
spec — so a strike resolves no DamageSpec, no HP depletes, no downed seam runs, and
the cwn spans never fire. The weapon-damage guard below is the content half of the
hp_depletion combat: without it the strike lands in prose but moves no HP — the exact
"narrator improvising combat with nothing underneath" failure this story exists to kill.

Determinism: the trauma/save dice roll via the `random` MODULE imported into
`sidequest.server.dispatch.dice`; monkeypatch `dice.random.randint` LOW so the downed
roll is non-traumatic (clean Mortal Injury, no Major-Injury branch). Damage faces roll
via a DIFFERENT module (`damage_roll.random`), untouched. `face=[20]` guarantees the
d20 beats the (low) opponent AC so the strike-damage path fires.

`otel_capture` is provided by tests/server/conftest.py.
"""

from __future__ import annotations

import pytest

from sidequest.game.ruleset.awn import AwnRulesetModule
from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_GENRE = "mutant_wasteland"
_WORLD = "flickering_reach"
_OPPONENT_AC = 8  # low so face=20 beats it under either native or awn attack math


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_pack() -> GenrePack:
    try:
        return load_genre_pack(find_pack_path(_GENRE))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _combat_confrontation(pack: GenrePack):
    combat = next(
        (c for c in pack.rules.confrontations if c.category == "combat"),
        None,
    )
    assert combat is not None, "mutant_wasteland must declare a combat confrontation"
    return combat


def _strike_beat(combat):
    """The combat's HP-dealing strike beat.

    Prefer a beat whose ``damage_channel`` is ``strike`` (the ADR-114 HP channel the
    migration must set on the attack beat); fall back to the first ``kind: strike``
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
        "mutant_wasteland combat must expose a strike beat to attack with; "
        f"beat kinds: {[_kind(b) for b in combat.beats]}"
    )
    return beat


def _personal_weapon_id(pack: GenrePack) -> str:
    """A non-mounted weapon id from the pack's own catalog. The strike resolves its
    DamageSpec from this item (resolve_damage_spec_from_beat_and_actor Priority 3 →
    CatalogItem.damage); a weapon with no `damage:` spec resolves to None → HP never
    depletes → these tests go RED, which is the content gap the story closes."""
    weapon = next(
        (
            item
            for item in pack.inventory.item_catalog
            if item.category == "weapon" and "mounted" not in item.tags
        ),
        None,
    )
    assert weapon is not None, "mutant_wasteland must declare a personal weapon in its catalog"
    return weapon.id


def _attacker_stats(pack: GenrePack) -> dict[str, int]:
    # Keyed by whatever the sheet currently uses (flavor names pre-migration, the
    # standard six post-migration), high enough to land hits.
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

    # The attacker carries the pack's OWN starting weapon by id with NO inline damage
    # spec, so the strike must resolve its DamageSpec from the pack's catalog — the
    # real-content path. If no personal weapon authors a `damage:` spec, this resolves
    # to None → damage_spec_missing → HP never depletes → RED.
    weapon = {"id": _personal_weapon_id(pack), "name": "Wasteland Weapon"}
    atk_core = CreatureCore(
        name=attacker,
        description="Scavenger of the wastes.",
        personality="hardbitten",
        inventory=Inventory(items=[weapon]),
        hp={"current": 12, "max": 12, "base_max": 12},
        armor_class=12,
    )
    attacker_char = Character(
        core=atk_core,
        char_class="Scavenger",
        race="Mutant Human",
        backstory="Born under a green sky.",
        stats=_attacker_stats(pack),
    )
    opp_core = CreatureCore(
        name=opponent,
        description="Raider in scrap plate.",
        personality="cruel",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug=_WORLD,
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
        genre_slug=_GENRE,
        session_id=request_id,
        round_number=round_number,
        room_broadcast=[].append,
        snapshot=snap,
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_mutant_wasteland_binds_awn_module_in_production_registry() -> None:
    """The bound ruleset resolves to the AWN module through the same registry the
    production dispatcher uses — the binding is reachable, not just declared. AWN is
    a clean WithoutNumberRulesetModule sibling (ADR-142, no longer Awn(Cwn)), so the
    WN-core capability gates cover it; it is NOT a CwnRulesetModule."""
    pack = _load_pack()
    module = get_ruleset_module(pack.rules.ruleset)
    assert isinstance(module, AwnRulesetModule), (
        "mutant_wasteland's bound ruleset must resolve to AwnRulesetModule in the "
        f"production registry; got {type(module).__name__} for slug {pack.rules.ruleset!r}"
    )
    assert isinstance(module, WithoutNumberRulesetModule), (
        "AwnRulesetModule must inherit WithoutNumberRulesetModule so the WN-core "
        "capability gates (lethality, Effort) fire for awn-bound combat"
    )
    assert not isinstance(module, CwnRulesetModule), (
        "ADR-142: AWN is a clean WN sibling, NOT a CwnRulesetModule subclass"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_mutant_wasteland_strike_depletes_ablative_hp_on_real_turn(
    otel_capture, monkeypatch
) -> None:
    """A real mutant_wasteland combat strike, driven through dispatch_dice_throw,
    ablates the target's HP pool (ADR-114) and emits state_patch.hp — proving the
    awn hp_depletion combat path is reachable from the production turn path with the
    pack's own content."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _load_pack()
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
        request_id="mw-ablative",
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "state_patch.hp" in span_names, (
        "a real mutant_wasteland strike must emit the ADR-114 state_patch.hp span "
        "(the combat strike beat must use damage_channel: strike and the weapon must "
        f"author a damage spec); got spans: {span_names}"
    )
    assert target.hp.current < target.hp.max, (
        "the strike must deplete the target's ablative HP pool; "
        f"hp={target.hp.current}/{target.hp.max}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_mutant_wasteland_downed_target_routes_through_cwn_seam(otel_capture, monkeypatch) -> None:
    """A real mutant_wasteland strike that drops a target to 0 HP runs the inherited
    WN-core downed seam and declares a Mortal Injury — the lethality lie-detector span
    (awn.mortal_injury.declared) firing on a real awn turn (the player can actually
    lose). RED while the pack is native (the seam is capability-gated on a WN binding)
    or while the strike beat deals no HP.

    Opponent seeded at hp=1 so one strike (faces forced low → 1 damage) drops it to 0;
    trauma die forced low so the hit is non-traumatic (no Major-Injury branch)."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _load_pack()
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
        request_id="mw-downed",
    )

    assert target.hp.current == 0, (
        "precondition for the downed seam: the strike must drop the target to 0 HP; "
        f"hp={target.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "awn.mortal_injury.declared" in span_names, (
        "a mutant_wasteland target dropped to 0 HP must declare a Mortal Injury — the "
        "inherited WN-core downed seam must run for the awn-bound pack on a real turn; "
        f"got spans: {span_names}"
    )
    assert any("Mortal Injury" in s.text for s in target.statuses), (
        "the downed target must carry a Mortal Injury status; "
        f"statuses={[s.text for s in target.statuses]}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_mutant_wasteland_personal_weapons_carry_damage_specs() -> None:
    """Every PERSONAL weapon in the mutant_wasteland catalog must author a ``damage``
    spec — the content half of the hp_depletion combat the engine seam serves.

    The combat confrontation becomes ``win_condition: hp_depletion`` with strike beats
    on ``damage_channel: strike`` and no ``damage_override``, so damage resolves from
    the actor's weapon (resolve_damage_spec_from_beat_and_actor Priority 3 →
    CatalogItem.damage). A personal weapon with no ``damage`` spec resolves to None →
    ``damage_spec_missing`` → the strike lands in prose but moves no HP (the exact
    "narrator improvising combat with nothing underneath" failure this story kills).
    RED today: NO mutant_wasteland weapon authors a damage spec."""
    pack = _load_pack()
    catalog = pack.inventory.item_catalog
    personal_weapons = [
        item for item in catalog if item.category == "weapon" and "mounted" not in item.tags
    ]
    assert personal_weapons, "mutant_wasteland must declare personal weapons in its catalog"

    missing = [item.id for item in personal_weapons if item.damage is None]
    assert not missing, (
        "every personal weapon must author a `damage:` spec so the hp_depletion "
        f"combat actually depletes HP; missing damage on: {missing}"
    )
