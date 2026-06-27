"""PLAYTEST (caverns_and_claudes/beneath_sunden, WWN, 2026-06-27): WWN combat
soft-locked the player. A fresh confrontation seated cleanly and the opponent
resolved mechanically (to-hit vs AC, ablative HP), but the player's beat menu
offered exactly ONE beat — "Drink Potion of Mending" (an inventory consumable) —
and after committing it the menu was EMPTY. No attack/weapon/disengage beat ever
appeared, so the player could not touch the opponent's HP: combat seated and could
not be played to a win or loss.

ROOT CAUSE: 108-3 strips ``cdef.beats`` to ``[]`` for a WWN ``hp_depletion`` combat
(the WN engine owns the round, ADR-143). The RESOLUTION path (wn_round.py / dice.py,
stories 108-8 / 152-1) synthesizes the WN action beats on commit, but the SELECTION
MENU (``beats_available_for``) only ever synthesized ``cast_spell`` (the caster
twin) — so a non-caster Warrior saw only inventory beats. This is the missing
selection-menu twin: ``build_confrontation_payload`` must offer the core WWN SRD
§2.4.4 action set (Attack + Total Defense + Fighting Withdrawal + Run) under a WN
binding.

WIRING test: drives the real ``build_confrontation_payload`` (the surface that emits
the CONFRONTATION beat menu to the client), not ``beats_available_for`` in isolation.
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
from sidequest.genre.models.rules import ConfrontationDef, MetricDef, RulesConfig, WwnConfig
from sidequest.server.dispatch.confrontation import build_confrontation_payload

_WN_MENU = {"attack", "total_defense", "fighting_withdrawal", "run"}
_WN_ATTRS = {a: a for a in ("STRENGTH", "DEXTERITY", "CONSTITUTION", "INTELLIGENCE", "WISDOM", "CHARISMA")}


def _wwn_rules() -> RulesConfig:
    """A minimal but VALID WWN RulesConfig (the validator requires an authored
    ``wwn.attribute_map``)."""
    return RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_WN_ATTRS.values()),
        wwn=WwnConfig(attribute_map=_WN_ATTRS),
    )


def _warrior() -> ClassDef:
    # A non-caster: no wwn_magic block, so the cast-menu twin never fires — the
    # ONLY way this PC gets an offensive beat is the WN action synthesis.
    return ClassDef(
        id="warrior",
        display_name="Warrior",
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table="warrior_kit",
        flavor="-",
        encounter_beat_choices=["killing_blow"],  # non-empty; not a WN action id
    )


def _wwn_empty_cdef() -> ConfrontationDef:
    """A WWN hp_depletion combat after 108-3 strips the native beats: ``beats=[]``."""
    return ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        win_condition="hp_depletion",
        player_metric=MetricDef(name="m", starting=0, threshold=10),
        opponent_metric=MetricDef(name="m", starting=0, threshold=10),
        beats=[],
        opponent_default_stats={
            "STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10,
            "hp": 10, "armor_class": 10, "dexterity": 10,
        },
    )


def _encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="m", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="m", current=0, starting=0, threshold=10),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(name="Chico", role="combatant", side="player"),
            EncounterActor(name="The Dwarf Still At Its Work", role="combatant", side="opponent"),
        ],
    )


def _core() -> SimpleNamespace:
    # Non-caster core, no consumables: the payload's ONLY beats must be synthesized.
    return SimpleNamespace(spellcasting=None, inventory=SimpleNamespace(items=[]))


def test_wwn_combat_offers_player_attack_menu_in_payload() -> None:
    payload = build_confrontation_payload(
        encounter=_encounter(),
        cdef=_wwn_empty_cdef(),
        genre_slug="caverns_and_claudes",
        recipient_pc=(_warrior(), 0.0, None),
        recipient_actor_name="Chico",
        core_resolver=lambda name: _core() if name == "Chico" else None,
        rules=_wwn_rules(),
    )
    ids = [b["id"] for b in payload["beats"]]
    assert ids, "WWN combat must not present an empty beat menu (the soft-lock)"
    assert ids[0] == "attack", "Attack leads the WWN action menu"
    assert _WN_MENU.issubset(ids), f"full WWN SRD 2.4.4 action set offered; got {ids}"


def test_native_binding_keeps_empty_cdef_menu_empty() -> None:
    # A native (dial) binding does NOT synthesize the WN action menu — an
    # empty-beats cdef stays an empty player menu (back-compat; the gate is the
    # WN binding, not merely hp_depletion).
    payload = build_confrontation_payload(
        encounter=_encounter(),
        cdef=_wwn_empty_cdef(),
        genre_slug="caverns_and_claudes",
        recipient_pc=(_warrior(), 0.0, None),
        recipient_actor_name="Chico",
        core_resolver=lambda name: _core() if name == "Chico" else None,
        rules=RulesConfig(ruleset="dial"),
    )
    ids = [b["id"] for b in payload["beats"]]
    assert not (_WN_MENU & set(ids)), f"native binding must not synthesize WN actions; got {ids}"
