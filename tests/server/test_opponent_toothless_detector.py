"""BUG 1 (eh-opp-damage) + Story 153-20 — flag a TOOTHLESS opponent AT SEATING,
but DON'T false-flag a weaponless Without-Number opponent as invulnerable.

PLAYTEST (elemental_harmony/burning_peace, WWN): the seated Other dealt 0 HP on
every reprisal — ``dice.opponent_reprisal_damage_spec_missing`` fired turn after
turn and the player stayed 10/10. BUG 1 added the instantiation-time
lie-detector: when an hp_depletion combat seats an opponent with no resolvable
damage source, the seating seam (``_seed_combat_hp_depletion_to_npcs``) emits
``encounter.opponent_toothless`` so the GM panel flags it the moment it is seated.

Story 153-1 then wired the *runtime* reprisal to fall back to the Without Number
SRD **unarmed floor** (1d2) so a weaponless WN opponent's landed hit still ablates
HP. Story 153-20 closes the seating-detector sibling of that drift: the at-seat
detector ``_opponent_reprisal_damage_resolvable`` did NOT know about the WN SRD
unarmed floor, so it kept false-flagging a weaponless WN opponent as toothless /
invulnerable — when the WN binding actually gives it a real 1d2 reprisal.

The fix is WN-gated (SOUL "Bind the Ruleset, Don't Balance It", ADR-143): under a
``swn``/``wwn``/``cwn``/``awn`` binding a weaponless opponent resolves the SRD
unarmed floor (taken directly from the ruleset binding, never invented) and is NOT
toothless; under any non-WN ruleset (native ``dial``, ``fate``) the conservative
false-toothless bias is unchanged. The span still fires at seating to surface the
content gap, but now carries a ``ruleset`` field always and an ``unarmed_floor``
field when the WN floor rescues the opponent — so the GM panel can tell "floor
resolved" apart from "genuinely toothless".

Tests drive the seating helper and the detector directly with synthetic
cdefs/cores + repo-local WN/non-WN fixture packs (content-independent), and assert
on the OTEL span (per the repo's "no source-text wiring tests" rule).
"""

from __future__ import annotations

import pytest

from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.encounter import EncounterActor
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, ResolutionMode

SPAN_TOOTHLESS = "encounter.opponent_toothless"

#: WN-family ruleset slugs — the SRD gives a weaponless attack a real unarmed floor.
_WN_SLUGS = ["swn", "wwn", "cwn", "awn"]
#: Non-WN rulesets that must KEEP the conservative false-toothless bias (no SRD floor).
_NON_WN_SLUGS = ["dial", "fate"]


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


def _weaponless_core(name: str = "Approaching Riders") -> CreatureCore:
    """An opponent core with NO inventory weapon (no slot-3 damage source)."""
    return CreatureCore(
        name=name,
        description="x",
        personality="x",
        inventory=Inventory(items=[]),
        hp={"current": 8, "max": 8, "base_max": 8},
        armor_class=12,
    )


def _armed_core(name: str = "Armed Rider") -> CreatureCore:
    """An opponent core carrying an inventory weapon with a damage spec (slot 3)."""
    return CreatureCore(
        name=name,
        description="x",
        personality="x",
        inventory=Inventory(items=[{"id": "saber", "name": "Saber", "damage": {"dice": "1d8"}}]),
        hp={"current": 8, "max": 8, "base_max": 8},
        armor_class=12,
    )


def _snapshot_with(core: CreatureCore) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(),
    )
    snap.npcs.append(Npc(core=core))
    return snap


def _seed(snap, cdef, opponent_name, *, ruleset):
    """Drive the REAL production seating helper (called at encounter handshake).

    Story 153-20 threads the bound ``ruleset`` module into the seeder so it can
    consult the WN SRD unarmed floor at seat time and stamp the discriminating
    span fields. The production caller (``instantiate_encounter_from_trigger``)
    resolves this from ``get_ruleset_module(pack.rules.ruleset)``; the test passes
    the module directly — the same object the caller hands the seeder.
    """
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
        ruleset=ruleset,
    )


def _detector(cdef, core, *, ruleset):
    from sidequest.server.dispatch.encounter_lifecycle import (
        _opponent_reprisal_damage_resolvable,
    )

    return _opponent_reprisal_damage_resolvable(cdef, core, ruleset=ruleset)


# ──────────────────────────────────────────────────────────────────────────────
# AC-1 / AC-2 — detector-level: WN weaponless opponent resolves the SRD unarmed
# floor (True); non-WN weaponless opponent stays toothless (False).
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("wn_slug", _WN_SLUGS)
def test_detector_weaponless_wn_opponent_resolves_unarmed_floor(wn_slug):
    """AC-1: a weaponless opponent (no opponent_damage, no strike override, no
    inventory weapon) under a WN binding is NOT toothless — the detector resolves
    the SRD unarmed floor and returns True. The fourth check fires for all four WN
    siblings, not just the one that surfaced the playtest bug."""
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)
    result = _detector(cdef, _weaponless_core(), ruleset=get_ruleset_module(wn_slug))
    assert result is True, (
        f"a weaponless {wn_slug} opponent CAN deal damage via the WN SRD unarmed "
        "floor — the seating detector must return True, not flag it invulnerable"
    )


@pytest.mark.parametrize("non_wn_slug", _NON_WN_SLUGS)
def test_detector_weaponless_non_wn_opponent_is_toothless(non_wn_slug):
    """AC-2: under a non-WN ruleset (native dial / Fate) the SAME weaponless
    opponent stays toothless (False) — the WN unarmed floor is WN-gated, not
    universal. The conservative false-toothless bias is preserved off the WN path
    (there is no SRD unarmed guarantee to defer to)."""
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)
    result = _detector(cdef, _weaponless_core(), ruleset=get_ruleset_module(non_wn_slug))
    assert result is False, (
        f"a weaponless {non_wn_slug} opponent has no SRD unarmed guarantee — the "
        "detector must keep the conservative toothless flag (False)"
    )


@pytest.mark.parametrize("slug", _WN_SLUGS + _NON_WN_SLUGS)
def test_detector_authored_opponent_damage_resolves_for_any_ruleset(slug):
    """An authored ``cdef.opponent_damage`` (priority slot 1) gives the opponent
    teeth regardless of ruleset — the new WN floor branch is a LAST resort and must
    not change the existing slot-1 behavior for any binding."""
    cdef = _combat_cdef(opponent_damage=DamageSpec(dice="1d6", bonus=0), strike_override=None)
    assert _detector(cdef, _weaponless_core(), ruleset=get_ruleset_module(slug)) is True


@pytest.mark.parametrize("slug", _WN_SLUGS + _NON_WN_SLUGS)
def test_detector_strike_override_resolves_for_any_ruleset(slug):
    """A strike beat carrying a ``damage_override`` (priority slot 2) is a
    resolvable reprisal source under any ruleset — not toothless, and reached
    before the WN floor."""
    cdef = _combat_cdef(opponent_damage=None, strike_override=DamageSpec(dice="2d6", bonus=0))
    assert _detector(cdef, _weaponless_core(), ruleset=get_ruleset_module(slug)) is True


@pytest.mark.parametrize("slug", _WN_SLUGS + _NON_WN_SLUGS)
def test_detector_inventory_weapon_resolves_for_any_ruleset(slug):
    """An opponent carrying an inventory weapon with a damage spec (priority slot 3)
    is not toothless under any ruleset, even with no authored ``opponent_damage``."""
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)
    assert _detector(cdef, _armed_core(), ruleset=get_ruleset_module(slug)) is True


# ──────────────────────────────────────────────────────────────────────────────
# AC-3 / AC-4 — integration: drive the REAL seating helper and assert the OTEL
# span discriminates "WN floor resolved" from "genuinely toothless".
# ──────────────────────────────────────────────────────────────────────────────


def test_wn_weaponless_seat_span_carries_ruleset_and_unarmed_floor(otel_capture):
    """AC-3 / AC-4: seating a weaponless opponent under a WN-bound pack drives the
    real ``_seed_combat_hp_depletion_to_npcs`` → ``_opponent_reprisal_damage_resolvable``
    path. The span fires carrying:
      * ``ruleset`` == the bound ruleset (AC-3), and
      * ``unarmed_floor`` == the SRD value taken DIRECTLY from the ruleset binding
        (AC-1: not authored/invented by the server; AC-4b).
    so the GM panel can confirm the floor resolved rather than a false-toothless flag.
    """
    snap = _snapshot_with(_weaponless_core("Approaching Riders"))
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)

    _seed(snap, cdef, "Approaching Riders", ruleset=get_ruleset_module("wwn"))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert spans, (
        "a weaponless WN opponent must still emit the seating diagnostic span "
        f"(now carrying the unarmed-floor discriminator); got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes)
    assert attrs.get("confrontation_type") == "combat"
    assert attrs.get("opponent") == "Approaching Riders"
    assert attrs.get("ruleset") == "wwn", "AC-3: the span carries the bound ruleset"
    assert attrs.get("unarmed_floor") == get_ruleset_module("wwn").SRD_UNARMED_DICE, (
        "AC-1/AC-4b: unarmed_floor must be the WN SRD value pulled from the ruleset "
        "binding (SRD_UNARMED_DICE), never an authored/invented literal"
    )


def test_non_wn_weaponless_seat_span_is_toothless_without_floor(otel_capture):
    """AC-2 / AC-3: under a non-WN (native dial) pack the weaponless opponent stays
    genuinely toothless — the span fires carrying the ``ruleset`` field but NO
    ``unarmed_floor`` (there is no SRD floor to defer to off the WN path)."""
    snap = _snapshot_with(_weaponless_core("Native Mook"))
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)

    _seed(snap, cdef, "Native Mook", ruleset=get_ruleset_module("dial"))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert spans, "a genuinely toothless (non-WN weaponless) opponent must still flag at seating"
    attrs = dict(spans[0].attributes)
    assert attrs.get("ruleset") == "dial", (
        "AC-3: the span carries the bound ruleset even when toothless"
    )
    assert not attrs.get("unarmed_floor"), (
        "a non-WN toothless opponent has NO unarmed floor — the unarmed_floor field "
        "must be absent/empty so the GM panel reads it as a genuine toothless flag"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Regression guard — an AUTHORED damage source (slots 1-3) closes the content gap,
# so NO toothless span fires. The 153-20 WN branch must not start firing spurious
# diagnostics for opponents that already have teeth.
# ──────────────────────────────────────────────────────────────────────────────


def test_opponent_damage_authored_emits_no_span_under_wn(otel_capture):
    """A cdef authoring ``opponent_damage`` (slot 1) under a WN pack has a real
    reprisal source — no content gap, so no toothless span at all."""
    snap = _snapshot_with(_weaponless_core("Statted Rider"))
    cdef = _combat_cdef(opponent_damage=DamageSpec(dice="1d6", bonus=0), strike_override=None)

    _seed(snap, cdef, "Statted Rider", ruleset=get_ruleset_module("wwn"))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert not spans, "an authored opponent_damage source means no content gap → no span"


def test_strike_override_emits_no_span_under_wn(otel_capture):
    """A strike beat with ``damage_override`` (slot 2) under a WN pack gives teeth —
    no toothless span."""
    snap = _snapshot_with(_weaponless_core("Override Rider"))
    cdef = _combat_cdef(opponent_damage=None, strike_override=DamageSpec(dice="2d6", bonus=0))

    _seed(snap, cdef, "Override Rider", ruleset=get_ruleset_module("wwn"))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert not spans, "a strike beat with damage_override gives the opponent teeth → no span"


def test_armed_opponent_emits_no_span_under_wn(otel_capture):
    """An opponent carrying an inventory weapon (slot 3) under a WN pack is not
    toothless even with no authored ``opponent_damage`` — no span."""
    snap = _snapshot_with(_armed_core("Armed Rider"))
    cdef = _combat_cdef(opponent_damage=None, strike_override=None)

    _seed(snap, cdef, "Armed Rider", ruleset=get_ruleset_module("wwn"))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_TOOTHLESS]
    assert not spans, "an armed opponent (inventory weapon) has a reprisal source → no span"
