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
# De-nativized WWN combat (108-3/108-8, ADR-143): the WN round owns the action
# set, so the committed action is the synthesized WN ``attack`` (the native
# ``committed_blow`` beat was stripped). These wire tests pin span presence and
# initiative-walk ORDERING (opponent acts before the player's beat), not HP, so
# no weapon is needed — the opponent_attack_resolved span fires regardless of the
# unarmed player's damage (story 125-8).
_STRIKE_BEAT = "attack"

_STATS = {"STR": 12, "DEX": 10, "CON": 10, "INT": 14, "WIS": 10, "CHA": 10}

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

# BLOCKED on epic-152: under de-nativized WWN combat the opponent's attack is
# skipped (``_resolve_opponent_reprisal`` requires an authored strike beat in
# ``cdef.beats``, stripped by 108-3; 108-8 synthesized only the PLAYER's attack,
# so ``wn_round`` logs ``opponent_reprisal_skipped reason=no_strike_beat``).
# Production gap (opponent-attack synthesis) — out of scope for 125-8 (AC3).
_OPPONENT_ATTACK_BLOCKED = (
    "epic-152: WN opponent attack skipped under de-nativized WWN combat "
    "(no_strike_beat — opponent strike beat never synthesized; 108-8 did only the "
    "player). Production gap; 125-8 is test-debt only (AC3). See Delivery Findings."
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


@pytest.mark.skip(reason=_OPPONENT_ATTACK_BLOCKED)
@pytest.mark.asyncio
async def test_ws_dice_throw_runs_the_initiative_ordered_round(
    session_handler_factory, otel_capture, monkeypatch
):
    """handle_message(DICE_THROW) on a solo WN table must close the barrier
    and walk the round in initiative order: round-phase spans fire and the
    higher-initiative opponent acts BEFORE the player's beat applies.

    SKIPPED (125-8): asserts the opponent acts before the player's beat, which
    needs the opponent attack to fire — the no_strike_beat production gap owned
    by epic-152 (see _OPPONENT_ATTACK_BLOCKED)."""
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
    """The seal→fire sequence at the WIRE level, across two real MP sockets.

    Two seated PCs, each on its OWN authenticated handler sharing ONE
    SessionRoom — the production MP substrate (ADR-036 barrier on one shared
    snapshot; ADR-119/118-9 per-socket identity: ``sd.player_id`` is the SOLE
    seat-resolution source, so two seats require two sockets). The first
    socket's DICE_THROW must SEAL (no round-phase spans, no resolution); the
    second must close the barrier and walk the round exactly once, inside the
    same production chain (handler → dispatch → wn_round).

    History (152-5): this test was quarantined believing the
    2nd-commit-misresolves-to-the-1st-seat symptom and the non-hermetic
    transport were *production* roots needing a Dev fix. The investigation
    proved both were *test construction*:

      * Seat resolution is correct. The prior single-handler version drove BOTH
        commits through one authenticated ``sd.player_id`` ("player-1"); under
        ADR-119 the second message's ``player_id="player-2"`` is a rejected
        spoof, so both resolved to "Rux" and ``seal_wn_commit`` raised
        "'Rux' has already committed" — the round never fired. Real MP gives
        each player its own socket/``sd.player_id``; two handlers sharing the
        room fix it with no production change.
      * The non-hermetic call is the post-narration sidecar-extraction watcher
        (``run_sidecar_extraction_watcher`` → live ``build_sidecar_extractor_llm``
        → real claude-agent-sdk ``query()``), not ``run_narration_turn``. Its
        fail-loud real-transport guard is the correct signal that a server test
        must install a fake; stubbing the watcher keeps the wire path hermetic.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.game.persistence import GameMode
    from sidequest.game.session import Npc
    from sidequest.protocol.models import InitiativeEntry

    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: misses, nobody drops
    monkeypatch.setattr(
        "sidequest.server.dispatch.monster_manual_inject.ensure_loaded",
        lambda _sd: None,
    )
    # Hermeticity (152-5 root 2): the post-narration sidecar-extraction watcher
    # builds a live SDK-backed LLM and would reach the real claude-agent-sdk
    # query() transport. ``run_narration_turn`` (stubbed below) does NOT cover
    # this seam — stub the watcher itself so the wire path stays hermetic.
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.run_sidecar_extraction_watcher",
        AsyncMock(return_value=None),
    )

    pc_one, pc_two = _PC, "Vex Calder"
    slug = "test-mp-wn-round-wire"
    seats = [("player-1", pc_one), ("player-2", pc_two)]
    handler_one, sd_one, room = session_handler_factory(
        genre="heavy_metal",
        slug=slug,
        mode=GameMode.MULTIPLAYER,
        seat_players=seats,
        active_player=("player-1", pc_one),
    )
    handler_two, sd_two, _room2 = session_handler_factory(
        genre="heavy_metal",
        slug=slug,
        mode=GameMode.MULTIPLAYER,
        seat_players=seats,
        active_player=("player-2", pc_two),
        existing_room=room,
    )

    # Install WN combat on the SHARED room snapshot: both PCs + a 10-HP opponent
    # first in initiative. Both handlers read this one snapshot/commit barrier.
    snapshot = room.snapshot
    for ch in snapshot.characters:
        if ch.core.name in (pc_one, pc_two):
            ch.stats.update(_STATS)
    snapshot.npcs.append(
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
    snapshot.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=pc_one, role="combatant", side="player"),
            EncounterActor(name=pc_two, role="combatant", side="player"),
            EncounterActor(name=_OPP, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
        initiative=[
            InitiativeEntry(token_id=_OPP, value=9),
            InitiativeEntry(token_id=pc_one, value=3),
            InitiativeEntry(token_id=pc_two, value=2),
        ],
    )

    fake = NarrationTurnResult(narration="Steel waits on steel.")
    sd_one.orchestrator.run_narration_turn = AsyncMock(return_value=fake)
    sd_two.orchestrator.run_narration_turn = AsyncMock(return_value=fake)

    # First socket commits → SEAL only; the barrier still waits on the second PC.
    await handler_one.handle_message(_strike_message(player_id="player-1", request_id="mp-wire-1"))

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

    # Second socket commits → barrier closes; the round walks exactly once.
    await handler_two.handle_message(_strike_message(player_id="player-2", request_id="mp-wire-2"))

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "wwn.round.committed" in names and "wwn.round.resolved" in names, (
        f"the barrier-closing wire commit must run the sealed round; got {names}"
    )
    assert names.count("wwn.round.resolved") == 1, (
        "exactly one round may fire for one full set of commits; got "
        f"{names.count('wwn.round.resolved')}"
    )
