"""BUG 1 (eh-opp-damage) — flag a TOOTHLESS opponent AT INSTANTIATION.

PLAYTEST (elemental_harmony/burning_peace, WWN): the seated Other dealt 0 HP on
every reprisal — ``dice.opponent_reprisal_damage_spec_missing`` fired turn after
turn and the player stayed 10/10, even on a failed beat. Root cause: the seated
opponent had NO resolvable reprisal damage source — the confrontation authors no
``opponent_damage``, its strike beat carries no ``damage_override``, and the
seeded/materialized opponent NPC has an empty inventory. The gap only surfaced
once per reprisal, deep in combat; the GM panel had no signal at SEATING time.

This adds the instantiation-time lie-detector the playtest asked for: when an
hp_depletion combat seats an opponent with no resolvable damage source, the
seating seam (``_seed_combat_hp_depletion_to_npcs``) emits
``encounter.opponent_toothless`` so the GM panel flags the toothless Other the
moment it is seated — not 6 rounds later. (Authoring the missing
``opponent_damage`` is a content fix; this is the engine's detector for it.)

Tests drive the seating helper directly with synthetic cdefs/cores and assert on
the OTEL span (per the repo's "no source-text wiring tests" rule).
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.encounter import EncounterActor
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, ResolutionMode

SPAN_TOOTHLESS = "encounter.opponent_toothless"


def _combat_cdef(*, opponent_damage: DamageSpec | None, strike_override: DamageSpec | None):
    """A minimal hp_depletion combat ConfrontationDef with one strike beat."""
    strike = BeatDef.model_validate(
        {
            "id": "strike",
            "label": "Strike",
            "kind": "strike",
            "base": 2,
            "stat_check": "Strength",
            "damage_channel": "strike",
            "effect": "A blow.",
            "narrator_hint": "Hit.",
            **({"damage_override": strike_override.model_dump()} if strike_override else {}),
        }
    )
    return ConfrontationDef(
        type="combat",
        label="Skirmish",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        win_condition="hp_depletion",
        player_metric=None,
        opponent_metric=None,
        opponent_default_stats={
            "Strength": 10,
            "hp": 8,
            "armor_class": 12,
            "dexterity": 11,
        },
        opponent_damage=opponent_damage,
        beats=[strike],
    )


def _snapshot_with_opponent(name: str, *, armed: bool) -> GameSnapshot:
    items = [{"id": "saber", "name": "Saber", "damage": {"dice": "1d8"}}] if armed else []
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(),
    )
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name=name,
                description="x",
                personality="x",
                inventory=Inventory(items=items),
                hp={"current": 8, "max": 8, "base_max": 8},
                armor_class=12,
            )
        )
    )
    return snap


def _seed(snap, cdef, opponent_name):
    from sidequest.server.dispatch.encounter_lifecycle import (
        _seed_combat_hp_depletion_to_npcs,
    )

    _seed_combat_hp_depletion_to_npcs(
        snapshot=snap,
        actors=[EncounterActor(name=opponent_name, role="combatant", side="opponent")],
        cdef=cdef,
        turn=1,
        source="encounter_handshake",
        acting_character_name="Hero",
    )


def test_toothless_opponent_emits_span_at_instantiation(otel_capture):
    """No opponent_damage, no strike damage_override, weaponless opponent → the
    seating seam flags the toothless Other. RED before the fix (no such span)."""
    snap = _snapshot_with_opponent("Approaching Riders", armed=False)
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)

    _seed(snap, cdef, "Approaching Riders")

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert spans, (
        "a hp_depletion combat that seats an opponent with no resolvable reprisal "
        "damage source must emit encounter.opponent_toothless AT INSTANTIATION; "
        f"got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes)
    assert attrs.get("opponent") == "Approaching Riders"
    assert attrs.get("confrontation_type") == "combat"


def test_opponent_damage_authored_is_not_toothless(otel_capture):
    """With cdef.opponent_damage authored (the space_opera-style fix), the
    opponent has a reprisal source — no toothless span fires."""
    snap = _snapshot_with_opponent("Statted Rider", armed=False)
    cdef = _combat_cdef(opponent_damage=DamageSpec(dice="1d6", bonus=0), strike_override=None)

    _seed(snap, cdef, "Statted Rider")

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert not spans, (
        "an opponent whose cdef authors opponent_damage is NOT toothless; "
        "the detector must not false-positive"
    )


def test_strike_damage_override_is_not_toothless(otel_capture):
    """A strike beat carrying a damage_override is a resolvable reprisal source —
    not toothless."""
    snap = _snapshot_with_opponent("Override Rider", armed=False)
    cdef = _combat_cdef(opponent_damage=None, strike_override=DamageSpec(dice="2d6", bonus=0))

    _seed(snap, cdef, "Override Rider")

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert not spans, "a strike beat with damage_override gives the opponent teeth"


def test_armed_opponent_is_not_toothless(otel_capture):
    """An opponent carrying an inventory weapon with a damage spec is not
    toothless even when the cdef authors no opponent_damage."""
    snap = _snapshot_with_opponent("Armed Rider", armed=True)
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)

    _seed(snap, cdef, "Armed Rider")

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert not spans, "an armed opponent (inventory weapon) has a reprisal source"
