"""Story 108-1 (epic 108, ADR-143) — the engine core cut.

Under a Without-Number binding we BIND the ruleset so we never balance combat
(SOUL: "Bind the Ruleset, Don't Balance It"). ``run_wn_round`` already resolves
the player's committed action with WN math (d20+hit vs AC, weapon dice, Shock,
saves), but it still reaches the NATIVE beat engine (``ruleset.apply_beat`` →
``beat_kinds.apply_beat``) to apply that action — and the native engine grants
fleeting tags ("Opening" / "Counter Stance"), advances dials, offers Brace, and
carries the composure rider. Those native riders are the residual bleed this
story REMOVES from the WN path (it does NOT tune or gate them — removal is the
whole point).

The observable, deterministic native rider is the fleeting tag: a CritSuccess
strike (d20 face 20 → ``RollOutcome.CritSuccess``, regardless of AC — see
``game/dice.py``) makes the native ``strike`` rule grant the **"Opening"**
fleeting tag (``beat_kinds.DEFAULT_DELTAS[BeatKind.strike][CritSuccess]``). After
the cut, a WN round must resolve the strike's WN damage WITHOUT minting that tag,
and must emit ``wwn.native_scaffolding_suppressed`` (slug-honest per the WN family
span invariant — the GM-panel lie-detector that the native engine is OFF).

RED today:
  * ``…grants_no_native_fleeting_tag`` — native ``apply_beat`` mints "Opening".
  * ``…emits_native_scaffolding_suppressed_span`` — the span does not exist yet.
  * ``…suppresses_native_scaffolding_end_to_end`` (wiring) — both, at the wire.

Green guards (must STAY green through the cut):
  * ``…strike_still_removes_opponent_hp`` — WN damage math is untouched.
  * ``…native_engine_apply_beat_still_grants_opening`` — the native engine itself
    is not edited; the cut is at the CALL SITE, not in ``beat_kinds``.
  * ``…native_module_is_not_a_without_number_module`` — the isinstance gate Dev
    must use correctly excludes native packs, so native dispatch never suppresses.

Lives in tests/integration/ because it needs the REAL packs (heavy_metal binds
``ruleset: wwn``); tests/server repoints genre resolution at frozen fixtures.
Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR
from tests.integration._wn_round_102_4 import (
    HM_OPPONENT_HP,
    HM_STRIKE_BEAT,
    dispatch_throw,
    force_initiative,
    load_pack,
    seat_wn_combat,
    spans_named,
)

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

_PC = "Rux"
_OPP = "Furnace Thrall"
_NATIVE_RIDER_TAGS = {"Opening", "Counter Stance"}
_SUPPRESSED_SPAN = "wwn.native_scaffolding_suppressed"
_CRIT_FACE = 20  # d20 face 20 → RollOutcome.CritSuccess regardless of AC (game/dice.py)


def _seat_pc_first():
    """Seat a solo heavy_metal (wwn) combat with the PC ahead in initiative.

    PC-first means the player's committed strike resolves at its own slot while
    the encounter is still live (the opponent has not yet acted), so the native
    ``apply_beat`` path — the one under test — is actually reached. A solo table
    is a 1-participant barrier: the single commit closes it and walks the round
    inside the same dispatch.
    """
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP], genre_slug="heavy_metal")
    force_initiative(enc, [(_PC, 9), (_OPP, 3)])
    return pack, snap, enc


def test_wn_round_critsuccess_strike_grants_no_native_fleeting_tag(otel_capture):
    """RED: a CritSuccess strike under a WN binding must NOT mint the native
    "Opening" fleeting tag — the native scaffolding is removed from the WN
    path, not balanced. Today ``run_wn_round`` calls ``ruleset.apply_beat`` and
    the native ``strike`` rule appends "Opening" on CritSuccess, so this fails."""
    pack, snap, enc = _seat_pc_first()

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_CRIT_FACE,
    )

    minted = [t.text for t in enc.tags if t.text in _NATIVE_RIDER_TAGS]
    assert not minted, (
        "a WN-bound CritSuccess strike minted native fleeting tag(s) "
        f"{minted!r} — the native beat engine is still resolving the player's "
        "action under a Without-Number binding (ADR-143: the native scaffolding "
        f"is REMOVED from the WN path, not tuned). encounter.tags={enc.tags!r}"
    )


def test_wn_round_emits_native_scaffolding_suppressed_span(otel_capture):
    """RED: the cut must emit ``wwn.native_scaffolding_suppressed`` — the
    GM-panel lie-detector proving the native engine did NOT resolve the WN
    action. Slug-honest (``wwn.*``, never ``native.*``/``cwn.*``) per the WN
    family span invariant. The span does not exist yet, so this fails."""
    pack, snap, enc = _seat_pc_first()

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_CRIT_FACE,
    )

    suppressed = spans_named(otel_capture, _SUPPRESSED_SPAN)
    assert suppressed, (
        f"the WN round did not emit {_SUPPRESSED_SPAN!r} — without it the GM "
        "panel cannot tell the native engine is OFF from Claude improvising a "
        "suppressed-scaffolding claim (CLAUDE.md OTEL Observability Principle). "
        f"finished spans: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    # Slug honesty: the suppression span carries the honest binding slug, never
    # a mislabelled native./cwn. namespace (the family invariant the rest of the
    # WN round spans hold — wwn.round.resolved, not native.round.resolved).
    dishonest = [
        s.name
        for s in otel_capture.get_finished_spans()
        if s.name.endswith(".native_scaffolding_suppressed") and not s.name.startswith("wwn.")
    ]
    assert not dishonest, (
        f"native-scaffolding-suppressed span emitted under a non-wwn slug {dishonest!r} "
        "for a heavy_metal (ruleset: wwn) pack — breaks the slug-honesty invariant"
    )


def test_wn_round_strike_still_removes_opponent_hp(otel_capture):
    """GREEN GUARD: removing the native scaffolding must NOT remove the WN
    damage. A hitting CritSuccess strike still drops the opponent's ablative HP
    via WN weapon dice. This stays green through the cut — it fails only if Dev
    throws out the baby (the WN math) with the bathwater (the native riders)."""
    pack, snap, enc = _seat_pc_first()
    before = snap.find_creature_core(_OPP).hp.current
    assert before == HM_OPPONENT_HP, "precondition: opponent seeded at full HP"

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_CRIT_FACE,
    )

    after = snap.find_creature_core(_OPP).hp.current
    assert after < before, (
        f"a hitting WN strike removed no opponent HP ({before} → {after}); the "
        "engine-core cut must keep WN weapon-damage resolution, only drop the "
        "native beat riders"
    )


def test_native_engine_apply_beat_still_grants_opening(otel_capture):
    """GREEN GUARD: the native beat engine itself is NOT edited. Calling
    ``beat_kinds.apply_beat`` directly with a CritSuccess strike still mints the
    "Opening" fleeting tag. The cut belongs at the CALL SITE in ``run_wn_round``
    (don't call the native engine under a WN binding), never inside
    ``beat_kinds`` — editing the engine would regress every NATIVE pack."""
    from sidequest.game.beat_kinds import BeatKind, apply_beat
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.genre.models.rules import BeatDef, DamageChannel
    from sidequest.protocol.dice import RollOutcome

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
    beat = BeatDef(
        id="committed_blow",
        label="Committed Blow",
        kind=BeatKind.strike,
        base=1,
        stat_check="STR",
        damage_channel=DamageChannel.none,  # pure dial strike — no HP/damage resolver needed
    )

    apply_beat(
        enc,
        enc.actors[0],
        beat,
        RollOutcome.CritSuccess,
        turn=1,
        edge_resolver=lambda _name: None,
        damage_resolver=lambda: 0,
    )

    assert any(t.text == "Opening" for t in enc.tags), (
        "the native beat engine stopped minting 'Opening' on a CritSuccess "
        "strike — the engine-core cut must live at run_wn_round's call site, "
        "NOT inside beat_kinds; editing the engine regresses native packs. "
        f"tags={enc.tags!r}"
    )


def test_native_module_is_not_a_without_number_module():
    """GREEN GUARD: the isinstance gate Dev must use to scope the cut. A WN
    binding IS a ``WithoutNumberRulesetModule``; the native module is NOT — so
    an ``isinstance(ruleset, WithoutNumberRulesetModule)`` gate suppresses the
    scaffolding only under WN bindings and leaves native packs byte-for-byte
    untouched ("Do NOT regress the native module for native packs")."""
    from sidequest.game.ruleset.native import NativeRulesetModule
    from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
    from sidequest.game.ruleset.wwn import WwnRulesetModule

    assert isinstance(WwnRulesetModule(), WithoutNumberRulesetModule), (
        "WWN must be a WithoutNumberRulesetModule so the isinstance gate fires"
    )
    assert not isinstance(NativeRulesetModule(), WithoutNumberRulesetModule), (
        "the native module must NOT be a WithoutNumberRulesetModule — otherwise "
        "an isinstance gate would wrongly suppress native scaffolding for native "
        "packs (the regression this story forbids)"
    )


# ---------------------------------------------------------------------------
# WIRING — the cut engages through the full production chain (CLAUDE.md: every
# test suite needs a wiring test). Drive WebSocketSessionHandler.handle_message
# with a real DICE_THROW (the shape the UI sends) and assert the suppression
# fires end-to-end: handler → dispatch → sealed round → WN resolution.
# ---------------------------------------------------------------------------


def _install_wire_wn_combat(sd) -> None:
    """Seat Rux vs a resolvable 10-HP opponent, opponent first in initiative.

    Mirrors test_102_4_wn_round_wire_wiring._install_wn_combat. Opponent-first is
    fine: Rux (12 HP) survives a single min-rolled hit, then resolves his own
    CritSuccess strike — reaching the native scaffolding the cut must suppress.
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


def _strike_message(player_id: str = "player-1", request_id: str = "wire-108-1"):
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
            beat_id=HM_STRIKE_BEAT,
        ),
        player_id=player_id,
    )


@pytest.mark.asyncio
async def test_ws_dice_throw_suppresses_native_scaffolding_end_to_end(
    session_handler_factory, otel_capture, monkeypatch
):
    """RED (wiring): a wire-level DICE_THROW on a solo WN table must run the
    round AND suppress the native scaffolding through the full production chain
    — the ``wwn.native_scaffolding_suppressed`` span fires and no native
    "Opening" tag is minted. If the cut is implemented only at the dispatch
    helper and not reached from the handler, this catches it."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _State

    # Min server rolls: the opponent's reprisal misses / chips low, so Rux
    # survives to resolve his own CritSuccess strike (client face=20).
    monkeypatch.setattr("random.randint", lambda a, b: a)
    monkeypatch.setattr(
        "sidequest.server.dispatch.monster_manual_inject.ensure_loaded",
        lambda _sd: None,
    )

    sd, handler = session_handler_factory(genre="heavy_metal")
    handler._state = _State.Playing
    _install_wire_wn_combat(sd)
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="Steel rings on steel."),
    )

    await handler.handle_message(_strike_message())

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.round.resolved" in names, (
        f"precondition: the wire commit must run the sealed round; got {names}"
    )
    assert _SUPPRESSED_SPAN in names, (
        f"the WN round resolved at the wire without emitting {_SUPPRESSED_SPAN!r} "
        f"— native scaffolding suppression is not wired into the handler→dispatch"
        f"→round chain. spans: {names}"
    )
    minted = [t.text for t in sd.snapshot.encounter.tags if t.text in _NATIVE_RIDER_TAGS]
    assert not minted, (
        f"a wire-level WN CritSuccess strike minted native fleeting tag(s) "
        f"{minted!r} — the native engine still resolved the action end-to-end"
    )
