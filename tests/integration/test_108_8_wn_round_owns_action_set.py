"""Story 108-8 (epic 108, ADR-143) — run_wn_round() must OWN the WN action set.

108-1 removed the native riders from the WN path and 108-3 strips the native
beat list off every WWN *combat* def (``win_condition: hp_depletion``), leaving
``cdef.beats == []``. But the runtime still resolves a committed action by
looking the commit's ``beat_id`` up in ``cdef.beats``:

    * ``dispatch/dice.py:407`` — ``beat = next(b for b in cdef.beats ...)`` →
      ``DiceDispatchError: unknown beat_id ... available: []``
    * ``dispatch/wn_round.py:374`` — the sealed-round re-resolution, same shape.

So with the beats stripped, EVERY WWN combat commit raises before any WN math
runs — a total combat outage (the 78 e2e failures this story unblocks for
108-3). The runtime half of the engine-core cut was never implemented.

THE FIX (Dev): under a ``WithoutNumberRulesetModule`` binding, ``dice.py`` +
``run_wn_round`` must SYNTHESIZE a transient beat for each WN action
(attack / move / item-use / cast) — independent of ``cdef.beats`` — modelled on
the existing ``is_item_use_beat`` transient-beat synthesis (the "Drink potion"
beat that resolves at ``dice.py:390`` BEFORE the cdef lookup and is NOT on the
cdef). The WN damage math is untouched (d20 + hit vs AC, weapon dice from the
actor's inventory, Shock, saves, ablative HP) and the cut keeps emitting
``wwn.native_scaffolding_suppressed`` (the GM-panel lie-detector that the native
engine is OFF — CLAUDE.md OTEL Observability Principle).

THE GATE (the regression this story forbids): the synthesis is scoped to the WN
binding via ``isinstance(ruleset, WithoutNumberRulesetModule)``. A *native* pack
keeps requiring an authored beat — and note that ``attack`` is ALSO a real
authored beat id on native packs (tests/fixtures/packs/test_genre's "Wasteland
Brawl"). An UNCONDITIONAL intercept would hijack that native beat; the gate is
what prevents it.

RED today (these FAIL — the dispatch raises ``unknown beat_id`` before resolving):
  * ``…wn_attack_resolves_without_dispatch_error``      (AC1)
  * ``…wn_attack_removes_opponent_hp``                  (AC1 / AC3)
  * ``…wn_attack_emits_native_scaffolding_suppressed``  (AC4)
  * ``…wn_attack_mints_no_native_fleeting_tag``         (AC3 — native engine OFF)
  * ``…ws_dice_throw_zero_beat_attack_resolves_end_to_end`` (wiring, AC1 / AC5)

GREEN GUARDS (these PASS today and must STAY green through the fix):
  * ``…unknown_action_id_under_zero_beats_still_raises`` (AC2 — the synthesis is a
    CLOSED allowlist, not "accept any id once beats are empty"; No Silent Fallbacks)
  * ``…native_attack_does_not_engage_wn_synthesis`` (AC2 — the isinstance gate;
    a native "attack" resolves on the native engine, never the WN suppression path)

Lives in tests/integration/ because it needs the REAL packs (heavy_metal binds
``ruleset: wwn``); tests/server repoints genre resolution at frozen fixtures.
Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.server.dispatch.dice import DiceDispatchError
from tests._helpers.genre_paths import GENRE_PACKS_DIR
from tests.integration._wn_round_102_4 import (
    HM_OPPONENT_HP,
    dispatch_throw,
    force_initiative,
    load_pack,
    seat_wn_combat,
)

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

_PC = "Rux"
_OPP = "Furnace Thrall"

# The canonical WN *attack* action id. The WN action surface (the UI buttons that
# replace the native beat menu) sends this: src/components/CavernActionPanel.tsx
# and EncounterTab.test.tsx both use "attack", and the story's action-set is spelled
# "(attack / move / item-use / cast)". item-use already rides "use_item:<slug>" and
# cast rides "cast_spell"; this is the new one the runtime must synthesize.
# Centralised so a contract change is a one-line edit, not a sweep.
WN_ATTACK_BEAT = "attack"

_SUPPRESSED_SPAN = "wwn.native_scaffolding_suppressed"
_NATIVE_RIDER_TAGS = {"Opening", "Counter Stance"}
_CRIT_FACE = 20  # d20 face 20 → RollOutcome.CritSuccess regardless of AC (game/dice.py)

# A plain melee weapon whose item dict carries a serialised ``damage`` spec
# (priority-2 in damage_roll.resolve_damage_spec_from_beat_and_actor). heavy_metal
# ships NO genre ``unarmed_damage`` floor, so a synthesized attack needs the actor
# to carry a real weapon for its WN weapon dice to resolve.
_WEAPON = {
    "id": "iron_blade",
    "name": "Iron Blade",
    "category": "weapon",
    "damage": {"dice": "1d8", "bonus": 0},
}


def _strip_combat_beats(pack) -> int:
    """Empty ``cdef.beats`` on every hp_depletion COMBAT def — the post-108-3 state.

    Mirrors story 108-3's strip (native beats removed from WWN combat defs, dial
    chase/negotiation defs left intact). Mutation is legal: ConfrontationDef is
    not a frozen model, and ``find_confrontation_def`` hands the dispatch the very
    object we mutate here (same list in ``pack.rules.confrontations``).
    """
    stripped = 0
    for cdef in pack.rules.confrontations:
        if getattr(cdef, "win_condition", None) == "hp_depletion":
            cdef.beats = []
            stripped += 1
    assert stripped, (
        "precondition: the real pack must ship at least one hp_depletion combat "
        "def to strip — the zero-beat state under test never occurred otherwise"
    )
    return stripped


def _arm_pc(snap, name: str = _PC) -> None:
    """Give the PC a weapon so the synthesized attack has WN weapon dice to roll."""
    core = snap.find_creature_core(name)
    assert core is not None, f"PC {name!r} must be seated before arming"
    core.inventory.items.append(dict(_WEAPON))


def _seat_zero_beat_combat(*, pc_first: bool = True):
    """Seat a solo heavy_metal (wwn) combat, arm the PC, force initiative, strip beats.

    PC-first means the player's committed attack resolves at its own slot while the
    encounter is still live (the opponent has not yet acted), so the synthesized-beat
    path under test is actually reached. A solo table is a 1-participant barrier: the
    single commit closes it and walks the round inside the same dispatch.
    """
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP], genre_slug="heavy_metal")
    _arm_pc(snap)
    order = [(_PC, 9), (_OPP, 3)] if pc_first else [(_OPP, 9), (_PC, 3)]
    force_initiative(enc, order)
    _strip_combat_beats(pack)
    return pack, snap, enc


def _attack(pack, snap, enc, *, face: int = _CRIT_FACE):
    return dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=WN_ATTACK_BEAT,
        face=face,
    )


# ---------------------------------------------------------------------------
# RED — the zero-beat WN attack must resolve through the WN engine.
# ---------------------------------------------------------------------------


def test_zero_beat_wn_attack_resolves_without_dispatch_error(otel_capture):
    """RED (AC1): a WN attack on a zero-beat combat def must resolve, not raise.

    Today ``dice.py:407`` looks ``"attack"`` up in the empty ``cdef.beats`` and
    raises ``DiceDispatchError: unknown beat_id ... available: []`` — the total
    combat outage. After the fix the WN engine synthesizes the attack beat and
    resolves it without ever consulting ``cdef.beats``."""
    pack, snap, enc = _seat_zero_beat_combat()
    try:
        _attack(pack, snap, enc)
    except DiceDispatchError as exc:
        pytest.fail(
            "a WN attack on a zero-beat (post-108-3) combat def raised "
            f"DiceDispatchError instead of resolving: {exc}. The WN engine must "
            "OWN the action set — synthesize a transient attack beat under the "
            "WithoutNumberRulesetModule binding, independent of cdef.beats "
            "(model: is_item_use_beat at dice.py:390)."
        )


def test_zero_beat_wn_attack_removes_opponent_hp(otel_capture):
    """RED (AC1/AC3): the synthesized attack still runs the WN damage math.

    A hitting CritSuccess attack drops the opponent's ablative HP via the actor's
    WN weapon dice — proving Dev kept the math (d20+hit vs AC, weapon dice) and did
    not ship a no-op stub that merely silences the lookup error."""
    pack, snap, enc = _seat_zero_beat_combat()
    before = snap.find_creature_core(_OPP).hp.current
    assert before == HM_OPPONENT_HP, "precondition: opponent seeded at full HP"

    _attack(pack, snap, enc)

    after = snap.find_creature_core(_OPP).hp.current
    assert after < before, (
        f"a hitting zero-beat WN attack removed no opponent HP ({before} → {after}); "
        "the synthesized attack must resolve real WN weapon damage, not a no-op that "
        "only suppresses the unknown-beat error"
    )


def test_zero_beat_wn_attack_emits_native_scaffolding_suppressed(otel_capture):
    """RED (AC4): the zero-beat resolution still emits the GM-panel lie-detector.

    ``wwn.native_scaffolding_suppressed`` proves the native beat engine did NOT
    resolve the synthesized action. Slug-honest (``wwn.*``, never ``native.*``)
    per the WN-family span invariant."""
    pack, snap, enc = _seat_zero_beat_combat()

    _attack(pack, snap, enc)

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert _SUPPRESSED_SPAN in names, (
        f"the zero-beat WN attack did not emit {_SUPPRESSED_SPAN!r} — without it the "
        "GM panel cannot tell the native engine is OFF from Claude improvising the "
        f"claim (CLAUDE.md OTEL Observability Principle). finished spans: {names}"
    )
    dishonest = [
        n
        for n in names
        if n.endswith(".native_scaffolding_suppressed") and not n.startswith("wwn.")
    ]
    assert not dishonest, (
        f"native-scaffolding-suppressed span under a non-wwn slug {dishonest!r} for a "
        "heavy_metal (ruleset: wwn) pack — breaks the slug-honesty invariant"
    )


def test_zero_beat_wn_attack_mints_no_native_fleeting_tag(otel_capture):
    """RED (AC3): the synthesized attack resolves WITHOUT the native engine.

    A CritSuccess strike through the native ``beat_kinds`` engine mints the
    "Opening" fleeting tag; the WN path must not. (Fails today because the dispatch
    raises before any resolution — and must stay clean once the synthesized path
    routes around ``beat_kinds``.)"""
    pack, snap, enc = _seat_zero_beat_combat()

    _attack(pack, snap, enc)

    minted = [t.text for t in enc.tags if t.text in _NATIVE_RIDER_TAGS]
    assert not minted, (
        f"a zero-beat WN CritSuccess attack minted native fleeting tag(s) {minted!r} — "
        "the synthesized attack reached the native beat engine instead of the WN "
        f"damage math (ADR-143: native scaffolding is REMOVED). encounter.tags={enc.tags!r}"
    )


# ---------------------------------------------------------------------------
# GREEN GUARDS — the synthesis is a closed, WN-gated allowlist.
# ---------------------------------------------------------------------------


def test_unknown_action_id_under_zero_beats_still_raises(otel_capture):
    """GREEN GUARD (AC2 / No Silent Fallbacks): synthesis is a CLOSED allowlist.

    The fix must synthesize the four WN actions ONLY. A bogus id under a zero-beat
    def must still raise ``DiceDispatchError`` — the failure mode to forbid is
    "the beats are empty, so accept whatever id arrives", which would let a
    malformed/stale commit silently resolve as an attack."""
    pack, snap, enc = _seat_zero_beat_combat()
    with pytest.raises(DiceDispatchError):
        dispatch_throw(
            pack=pack,
            snap=snap,
            enc=enc,
            character_name=_PC,
            player_id="player-1",
            beat_id="frobnicate_the_widget",
            face=_CRIT_FACE,
        )


def test_native_attack_does_not_engage_wn_synthesis(otel_capture):
    """GREEN GUARD (AC2 — the isinstance gate): native "attack" is NOT WN-synthesized.

    ``attack`` is a real authored beat on the native test_genre "Wasteland Brawl"
    cdef. Under a native binding the dispatch must resolve it on the NATIVE engine —
    it must NOT route through the WN synthesis (no ``*.native_scaffolding_suppressed``
    span). An unconditional intercept (synthesis not gated on
    ``isinstance(ruleset, WithoutNumberRulesetModule)``) would hijack the native beat;
    this catches it. Passes today (no WN path for a native pack) and must stay green."""
    from pathlib import Path

    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager
    from sidequest.genre.loader import load_genre_pack

    fixture_pack = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"
    pack = load_genre_pack(fixture_pack)
    assert pack.rules.ruleset == "dial", "precondition: test_genre binds the dial ruleset"

    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    from sidequest.game.character import Character

    snap.characters.append(
        Character(
            core=CreatureCore(
                name=_PC,
                description="a brawler",
                personality="gritty",
                inventory=Inventory(),
                hp={"current": 12, "max": 12, "base_max": 12},
                level=1,
            ),
            char_class="Survivor",
            race="Human",
            backstory="—",
            stats={"Brawn": 12, "Toughness": 10, "Reflexes": 10, "Instinct": 10},
        )
    )
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name=_OPP,
                description="a raider",
                personality="feral",
                inventory=Inventory(),
                hp={"current": 10, "max": 10, "base_max": 10},
                armor_class=12,
            )
        )
    )
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=_PC, role="combatant", side="player"),
            EncounterActor(name=_OPP, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    snap.encounter = enc

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=WN_ATTACK_BEAT,
        face=13,
        genre_slug="test_genre",
        stats={"Brawn": 12, "Toughness": 10, "Reflexes": 10, "Instinct": 10},
    )

    suppressed = [
        s.name
        for s in otel_capture.get_finished_spans()
        if s.name.endswith(".native_scaffolding_suppressed")
    ]
    assert not suppressed, (
        f"a NATIVE 'attack' commit emitted WN suppression span(s) {suppressed!r} — the "
        "WN action synthesis fired for a native pack. The intercept must be gated on "
        "isinstance(ruleset, WithoutNumberRulesetModule); a native 'attack' is the "
        "pack's authored beat and resolves on the native engine."
    )


# ---------------------------------------------------------------------------
# WIRING — the synthesized attack resolves through the full production chain
# (CLAUDE.md: every test suite needs a wiring test). Drive
# WebSocketSessionHandler.handle_message with a real DICE_THROW (the shape the UI
# sends) against a zero-beat pack and assert the round resolves end-to-end.
# ---------------------------------------------------------------------------


def _install_wire_zero_beat_combat(sd) -> None:
    """Seat Rux vs a 10-HP opponent (opponent first), arm Rux, strip the pack beats.

    Opponent-first is fine: Rux (12 HP) survives a single min-rolled reprisal, then
    resolves his own CritSuccess attack — reaching the synthesized-beat path the cut
    must own. Mirrors test_108_1_wn_native_scaffolding_cut._install_wire_wn_combat,
    plus the post-108-3 beat strip and a carried weapon.
    """
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import Npc
    from sidequest.protocol.models import InitiativeEntry

    sd.snapshot.characters[0].stats.update(
        {"STR": 12, "DEX": 10, "CON": 10, "INT": 14, "WIS": 10, "CHA": 10}
    )
    sd.snapshot.characters[0].core.inventory.items.append(dict(_WEAPON))
    sd.snapshot.npcs.append(
        Npc(
            core=CreatureCore(
                name=_OPP,
                description="A furnace-fed revenant.",
                personality="relentless",
                inventory=Inventory(),
                hp={"current": 10, "max": 10, "base_max": 10},
                armor_class=12,
            )
        )
    )
    sd.snapshot.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=_PC, role="combatant", side="player"),
            EncounterActor(name=_OPP, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
        initiative=[
            InitiativeEntry(token_id=_OPP, value=9),
            InitiativeEntry(token_id=_PC, value=3),
        ],
    )
    _strip_combat_beats(sd.genre_pack)


def _attack_message(player_id: str = "player-1", request_id: str = "wire-108-8"):
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.messages import DiceThrowMessage

    return DiceThrowMessage(
        payload=DiceThrowPayload(
            request_id=request_id,
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[_CRIT_FACE],
            beat_id=WN_ATTACK_BEAT,
        ),
        player_id=player_id,
    )


@pytest.mark.asyncio
async def test_ws_dice_throw_zero_beat_attack_resolves_end_to_end(
    session_handler_factory, otel_capture, monkeypatch
):
    """RED (wiring, AC1/AC5): a wire-level DICE_THROW with ``beat_id="attack"`` on a
    zero-beat WN table must run the sealed round AND resolve through the synthesized
    action set — ``wwn.round.resolved`` + ``wwn.native_scaffolding_suppressed`` fire
    and the opponent's HP drops. If the cut is implemented only at the dispatch helper
    and not reached from the handler→dispatch→round chain, this catches it."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _State

    # Min server rolls: the opponent's reprisal chips low, so Rux survives to resolve
    # his own CritSuccess attack (client face=20).
    monkeypatch.setattr("random.randint", lambda a, b: a)
    monkeypatch.setattr(
        "sidequest.server.dispatch.monster_manual_inject.ensure_loaded",
        lambda _sd: None,
    )

    sd, handler = session_handler_factory(genre="heavy_metal")
    handler._state = _State.Playing
    _install_wire_zero_beat_combat(sd)
    before = sd.snapshot.find_creature_core(_OPP).hp.current
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="Steel rings on steel."),
    )

    await handler.handle_message(_attack_message())

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.round.resolved" in names, (
        f"the wire commit must run the sealed round on a zero-beat def; got {names}"
    )
    assert _SUPPRESSED_SPAN in names, (
        f"the zero-beat WN round resolved at the wire without {_SUPPRESSED_SPAN!r} — "
        f"the synthesized-action cut is not wired into the handler→dispatch→round "
        f"chain. spans: {names}"
    )
    after = sd.snapshot.find_creature_core(_OPP).hp.current
    assert after < before, (
        f"a wire-level zero-beat WN attack removed no opponent HP ({before} → {after}); "
        "the synthesized attack did not resolve WN weapon damage end-to-end"
    )
