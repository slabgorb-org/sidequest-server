"""Story 108-5 (epic 108, ADR-143) — the WN combat RP-flavor rider is flavor-only.

Under a Without-Number binding combat is declared through a closed set of WN
action buttons, and each button fires a REAL WN roll (108-1 cut the native
scaffolding behind them). The player may ATTACH freeform text to a button press
— the "chandelier swing" — but that text is RP intent ONLY: it rides alongside
the roll as a narrator hook and NEVER becomes its own action, gets its own roll,
grants advantage, sets a DC, or changes the resolved outcome. The d20 and the
weapon dice are byte-for-byte identical whether the player typed nothing or
typed a flourish (spec 2026-06-14; Keith: "you ACCOMPANY the attack with the
chandelier swing, not replace it … this is just an RP affordance").

The lie-detector for THIS feature is a new OTEL span:
``wwn.action.flavor_rider{attached=true, affected_mechanics=false}``. Without it
the GM panel cannot tell a flavor rider from a covert freeform-adjudication
regression — exactly the El Dorado failure the mechanical-scaffold architecture
exists to escape (SOUL: "escape El Dorado through structural support"). It pairs
with 108-1's ``wwn.native_scaffolding_suppressed`` (no native rider) — together
they prove the WN round resolved on the button and only on the button.

RED today (the span does not exist yet):
  * ``…attached_rider_emits_flavor_rider_span``
  * ``…flavor_rider_span_marks_mechanics_unaffected`` (the attr contract)
  * ``…flavor_rider_span_wired_end_to_end`` (wiring — the span at the wire)

Green guards (must STAY green — they pin the inertness the span CLAIMS):
  * ``…rider_removes_identical_opponent_hp`` — same d20+damage with/without text.
  * ``…rider_reaches_narrator_replay_text`` — the text IS attached as context.
  * ``…absent_rider_emits_no_flavor_rider_span`` — no false-positive span.

Lives in tests/integration/ because it needs the REAL packs (heavy_metal binds
``ruleset: wwn``); tests/server repoints genre resolution at frozen fixtures.
Skips cleanly when sidequest-content is not on disk. Mirrors
tests/integration/test_108_1_wn_native_scaffolding_cut.py.
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
_FLAVOR_SPAN = "wwn.action.flavor_rider"
_RIDER = "I swing from the chandelier"
_HIT_FACE = 20  # d20 face 20 → CritSuccess, hits regardless of AC (game/dice.py)


def _seat_pc_first():
    """Seat a solo heavy_metal (wwn) combat with the PC ahead in initiative.

    PC-first means the player's committed strike resolves at its own slot while
    the encounter is still live, so the full WN resolution path — the one the
    flavor rider rides — is actually reached. A solo table is a 1-participant
    barrier: the single commit closes it and walks the round in the same
    dispatch (mirrors test_108_1's ``_seat_pc_first``).
    """
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP], genre_slug="heavy_metal")
    force_initiative(enc, [(_PC, 9), (_OPP, 3)])
    return pack, snap, enc


def test_attached_rider_emits_flavor_rider_span(otel_capture):
    """RED: a WN combat throw that carries an RP-flavor rider must emit
    ``wwn.action.flavor_rider`` — the GM-panel lie-detector proving the typed
    "chandelier swing" was attached as narrator context, NOT mechanized into a
    second action. The span does not exist yet, so this fails."""
    pack, snap, enc = _seat_pc_first()

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_HIT_FACE,
        player_action=_RIDER,
    )

    rider = spans_named(otel_capture, _FLAVOR_SPAN)
    assert rider, (
        f"a WN combat throw carrying a flavor rider did not emit {_FLAVOR_SPAN!r} "
        "— without it the GM panel cannot distinguish an inert RP affordance from "
        "a covert freeform-adjudication regression (the El Dorado failure ADR-143 "
        "exists to prevent; CLAUDE.md OTEL Observability Principle). finished "
        f"spans: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    # Slug honesty: the rider span carries the honest binding slug, never a
    # mislabelled native./cwn. namespace (the family invariant the rest of the
    # WN round spans hold — wwn.round.resolved, not native.round.resolved).
    dishonest = [
        s.name
        for s in otel_capture.get_finished_spans()
        if s.name.endswith(".action.flavor_rider") and not s.name.startswith("wwn.")
    ]
    assert not dishonest, (
        f"flavor_rider span emitted under a non-wwn slug {dishonest!r} for a "
        "heavy_metal (ruleset: wwn) pack — breaks the slug-honesty invariant"
    )


def test_flavor_rider_span_marks_mechanics_unaffected(otel_capture):
    """RED: the rider span must record ``attached=true`` and, crucially,
    ``affected_mechanics=false`` — the explicit attestation that the flavor text
    colored the prose without touching the dice. The span (and these attrs) do
    not exist yet, so this fails."""
    pack, snap, enc = _seat_pc_first()

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_HIT_FACE,
        player_action=_RIDER,
    )

    rider = spans_named(otel_capture, _FLAVOR_SPAN)
    assert rider, (
        f"precondition: {_FLAVOR_SPAN!r} must fire when a flavor rider is attached"
    )
    attrs = dict(rider[0].attributes or {})
    assert attrs.get("attached") is True, (
        f"flavor_rider span must record attached=True when text rides the throw; "
        f"got attributes {attrs!r}"
    )
    assert attrs.get("affected_mechanics") is False, (
        "flavor_rider span must record affected_mechanics=False — it is the "
        "lie-detector that the chandelier text colored prose without touching the "
        f"dice (AC5). got attributes {attrs!r}"
    )


def test_rider_removes_identical_opponent_hp(otel_capture, monkeypatch):
    """GREEN GUARD: the flavor rider is mechanically inert. With the damage dice
    pinned to a fixed roll, a hitting strike removes the EXACT same opponent HP
    whether the player attached a flourish or typed nothing. This is the
    behavioral truth the ``affected_mechanics=false`` span asserts — if the rider
    ever fed a bonus/advantage into resolution, the two HP deltas would diverge
    and this guard would fail."""
    # Pin every dice roll to its minimum so both runs draw an identical weapon
    # roll — any HP divergence is then attributable to the rider, nothing else
    # (mirrors test_108_1's wire pin ``random.randint -> a``).
    monkeypatch.setattr("random.randint", lambda a, b: a)

    pack_a, snap_a, enc_a = _seat_pc_first()
    before_a = snap_a.find_creature_core(_OPP).hp.current
    assert before_a == HM_OPPONENT_HP, "precondition: opponent seeded at full HP"
    dispatch_throw(
        pack=pack_a,
        snap=snap_a,
        enc=enc_a,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_HIT_FACE,
    )
    removed_plain = before_a - snap_a.find_creature_core(_OPP).hp.current

    pack_b, snap_b, enc_b = _seat_pc_first()
    before_b = snap_b.find_creature_core(_OPP).hp.current
    dispatch_throw(
        pack=pack_b,
        snap=snap_b,
        enc=enc_b,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_HIT_FACE,
        player_action=_RIDER,
    )
    removed_rider = before_b - snap_b.find_creature_core(_OPP).hp.current

    assert removed_plain > 0, (
        "precondition: a hitting WN strike must remove opponent HP for the "
        "comparison to mean anything"
    )
    assert removed_rider == removed_plain, (
        "the RP-flavor rider changed the WN damage "
        f"(no-rider removed {removed_plain} HP, rider removed {removed_rider} HP) "
        "— the rider is supposed to be mechanically inert: same d20, same weapon "
        "dice, with or without the chandelier swing (spec 108-5, AC3)"
    )


def test_rider_reaches_narrator_replay_text(otel_capture):
    """GREEN GUARD: the rider IS attached as narrator context. The typed text
    reaches the narrator replay as a ``PLAYER_ACTION:`` hook so the narrator can
    color the resolved button outcome (AC3) — this is the "attached" half that
    the span's ``attached=true`` attribute attests, proven behaviorally."""
    pack, snap, enc = _seat_pc_first()

    outcome = dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_HIT_FACE,
        player_action=_RIDER,
    )

    assert f"PLAYER_ACTION: {_RIDER}" in outcome.replay_action_text, (
        "the flavor rider text must reach the narrator replay as a PLAYER_ACTION "
        "hook so the narrator colors the resolved button outcome with the player's "
        f"RP intent (AC3); got replay_action_text={outcome.replay_action_text!r}"
    )


def test_absent_rider_emits_no_flavor_rider_span(otel_capture):
    """GUARD: the rider span fires ONLY when text is actually attached. A WN
    combat throw with no flavor text must NOT emit ``wwn.action.flavor_rider`` —
    a span on an empty rider would be a false positive that pollutes the GM
    panel's signal (and would let a fabricated rider hide)."""
    pack, snap, enc = _seat_pc_first()

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="player-1",
        beat_id=HM_STRIKE_BEAT,
        face=_HIT_FACE,
    )

    rider = spans_named(otel_capture, _FLAVOR_SPAN)
    assert not rider, (
        "a WN combat throw with NO attached flavor text emitted a "
        f"{_FLAVOR_SPAN!r} span — the rider span must fire only when text is "
        "actually attached, else it is a false positive. spans: "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )


# ---------------------------------------------------------------------------
# WIRING — the rider span engages through the full production chain (CLAUDE.md:
# every test suite needs a wiring test). Drive WebSocketSessionHandler with a
# real DICE_THROW carrying ``player_action`` (the shape the UI sends when the
# player types a flourish then clicks a button) and assert the rider span fires
# end-to-end: handler → dispatch → sealed round. Mirrors test_108_1's wire test.
# ---------------------------------------------------------------------------


def _install_wire_wn_combat(sd) -> None:
    """Seat Rux vs a resolvable 10-HP opponent, opponent first in initiative.

    Mirrors test_108_1's ``_install_wire_wn_combat``. Opponent-first is fine: Rux
    (12 HP) survives a single min-rolled hit, then resolves his own CritSuccess
    strike — reaching the WN resolution path the rider rides.
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


def _strike_message(
    *,
    player_action: str | None = None,
    player_id: str = "player-1",
    request_id: str = "wire-108-5",
):
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
            face=[_HIT_FACE],
            beat_id=HM_STRIKE_BEAT,
            player_action=player_action,
        ),
        player_id=player_id,
    )


@pytest.mark.asyncio
async def test_flavor_rider_span_wired_end_to_end(
    session_handler_factory, otel_capture, monkeypatch
):
    """RED (wiring): a wire-level DICE_THROW carrying a flavor rider must run the
    round AND emit ``wwn.action.flavor_rider`` with affected_mechanics=False
    through the full production chain. If the span is emitted only inside a
    dispatch helper and not reached from the handler, this catches it."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _State

    # Min server rolls: the opponent's reprisal chips low, so Rux survives to
    # resolve his own CritSuccess strike (client face=20).
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

    await handler.handle_message(_strike_message(player_action=_RIDER))

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.round.resolved" in names, (
        f"precondition: the wire commit must run the sealed round; got {names}"
    )
    rider = [s for s in otel_capture.get_finished_spans() if s.name == _FLAVOR_SPAN]
    assert rider, (
        f"a wire-level WN throw carrying a flavor rider did not emit {_FLAVOR_SPAN!r} "
        "— the rider lie-detector is not wired into the handler→dispatch→round "
        f"chain. spans: {names}"
    )
    attrs = dict(rider[0].attributes or {})
    assert attrs.get("affected_mechanics") is False, (
        "the wire-level flavor_rider span must prove the rider was mechanically "
        f"inert (affected_mechanics=False); got attributes {attrs!r}"
    )
