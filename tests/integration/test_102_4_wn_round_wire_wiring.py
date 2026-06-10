"""Story 102-4 AC5 — WIRING: a WebSocket-level DICE_THROW runs the WN round.

Every test suite needs a wiring test (CLAUDE.md): the dispatch-level proofs
in test_102_4_wn_sealed_round.py call ``dispatch_dice_throw`` directly —
necessary but not sufficient. This suite drives
``WebSocketSessionHandler.handle_message`` with a real ``DiceThrowMessage``
(the wire shape the UI sends) and asserts the round walk engaged through the
full production chain: handler -> dispatch -> sealed round -> initiative-
ordered resolution. Solo table: one seated PC means the commit IS the
barrier close, so the round must fire inside the same wire dispatch.

Pattern: tests/integration/test_dice_throw_spell_cast_wiring_102_2.py
(session_handler_factory + AsyncMock narrator + Monster Manual pregen
stubbed to "not loaded" — pregen runs downstream of the seam under test and
fail-louds on the factory's empty world bestiary, correct production
behavior but out of scope here).

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR

_OPP = "Furnace Thrall"
_PC = "Rux"
_STRIKE_BEAT = "committed_blow"

_STATS = {"STR": 12, "DEX": 10, "CON": 10, "INT": 14, "WIS": 10, "CHA": 10}

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


def _install_wn_combat(sd) -> None:
    """Seat Rux vs a resolvable 10-HP opponent core, opponent first in
    initiative — the wire-level proof that the order is walked, not prose."""
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.session import Npc
    from sidequest.protocol.models import InitiativeEntry

    sd.snapshot.characters[0].stats.update(_STATS)
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


def _strike_message(player_id: str = "player-1", request_id: str = "wire-round-102-4"):
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
            face=[13],
            beat_id=_STRIKE_BEAT,
        ),
        player_id=player_id,
    )


@pytest.mark.asyncio
async def test_ws_dice_throw_runs_the_initiative_ordered_round(
    session_handler_factory, otel_capture, monkeypatch
):
    """handle_message(DICE_THROW) on a solo WN table must close the barrier
    and walk the round in initiative order: round-phase spans fire and the
    higher-initiative opponent acts BEFORE the player's beat applies. If any
    link in handler -> dispatch -> round walk is missing, this catches it
    while the unit suite stays green."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _State

    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: misses, nobody drops
    monkeypatch.setattr(
        "sidequest.server.dispatch.monster_manual_inject.ensure_loaded",
        lambda _sd: None,
    )

    sd, handler = session_handler_factory(genre="heavy_metal")
    handler._state = _State.Playing
    _install_wn_combat(sd)
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="Steel rings on steel."),
    )

    await handler.handle_message(_strike_message())

    spans = otel_capture.get_finished_spans()
    names = [s.name for s in spans]
    assert "wwn.round.committed" in names and "wwn.round.resolved" in names, (
        "a wire-level commit on a solo WN table must run the sealed round "
        f"through the full production chain; got spans: {names}"
    )
    walk = sorted(
        (
            s
            for s in spans
            if s.name in ("encounter.opponent_attack_resolved", "encounter.beat_applied")
        ),
        key=lambda s: s.start_time,
    )
    assert walk and walk[0].name == "encounter.opponent_attack_resolved", (
        "with the opponent first in persisted initiative, its attack must "
        "START before the player's beat applies — order walked at the wire "
        f"level, not narrated; walk: {[s.name for s in walk]}"
    )


@pytest.mark.asyncio
async def test_mp_wire_first_commit_seals_second_commit_fires_the_round(
    session_handler_factory, otel_capture, monkeypatch
):
    """Review rework r1 [MEDIUM]: the seal→fire sequence at the WIRE level.

    Two seated PCs (snapshot.player_seats maps each player_id to its PC —
    the production MP seat resolution in handlers/dice_throw.py). The first
    DICE_THROW must seal (no round-phase spans, no resolution); the second
    must close the barrier and walk the round exactly once, inside the same
    production chain (handler → dispatch → wn_round). Before this test the
    two-player sequence was proven at dispatch level only."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.encounter import EncounterActor
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.session_handler import _State
    from tests.integration._wn_round_102_4 import make_pc

    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: misses, nobody drops
    monkeypatch.setattr(
        "sidequest.server.dispatch.monster_manual_inject.ensure_loaded",
        lambda _sd: None,
    )

    sd, handler = session_handler_factory(genre="heavy_metal")
    handler._state = _State.Playing
    _install_wn_combat(sd)
    pc_one = sd.snapshot.characters[0].core.name
    pc_two = "Vex Calder"
    sd.snapshot.characters.append(make_pc(pc_two, stats=_STATS))
    enc = sd.snapshot.encounter
    enc.actors.append(EncounterActor(name=pc_two, role="combatant", side="player"))
    enc.initiative.append(InitiativeEntry(token_id=pc_two, value=1))
    sd.snapshot.player_seats = {"player-1": pc_one, "player-2": pc_two}
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="Steel waits on steel."),
    )

    await handler.handle_message(_strike_message(player_id="player-1", request_id="mp-wire-1"))

    names_after_first = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.round.committed" not in names_after_first, (
        "the barrier is still open after one of two commits — no round phase "
        f"may start; got spans {names_after_first}"
    )
    assert "wwn.round.resolved" not in names_after_first, (
        "resolution output leaked before the barrier closed (AC1 at the wire)"
    )
    assert "encounter.opponent_attack_resolved" not in names_after_first, (
        "the opponent acts at its initiative slot once the round fires — a "
        "reprisal on the FIRST sealed commit is the retired rider behavior"
    )

    await handler.handle_message(_strike_message(player_id="player-2", request_id="mp-wire-2"))

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.round.committed" in names and "wwn.round.resolved" in names, (
        f"the barrier-closing wire commit must run the sealed round; got {names}"
    )
    assert names.count("wwn.round.resolved") == 1, (
        "exactly one round may fire for one full set of commits; got "
        f"{names.count('wwn.round.resolved')}"
    )
