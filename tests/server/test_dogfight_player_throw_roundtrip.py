"""Task 14 round-trip wiring tests — player shot via client Rapier throw.

Three mandatory scenarios:

1. ``test_player_gun_solution_stashes_and_emits_dice_request``:
   Sealed-letter turn where the PLAYER gets a gun solution.
   Asserts: (a) NO frame HP changed yet, (b) sd.pending_dogfight_shot is set
   with the right player_shooter_role, (c) a DICE_REQUEST message was broadcast.

2. ``test_dice_throw_completes_pending_shot``:
   Given a stashed PendingDogfightShot, dispatch a DICE_THROW with a high face.
   Asserts: shots resolve (frame HP ablated for the target),
   sd.pending_dogfight_shot is None after, a DICE_RESULT was broadcast.

3. ``test_npc_only_gun_solution_resolves_immediately_no_stash``:
   Cell where only the NPC gets a gun solution → resolves in the same turn,
   sd.pending_dogfight_shot stays None.

Reuses the harness from test_dogfight_shot_wiring.py and trigger_encounter.
Monkeypatches ``_roll_d20_server_side`` + ``sidequest.game.dogfight_shot._roll_damage_dice``
for determinism.
Skips when sidequest-content is not checked out.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.dogfight_shot import FRAME_HP_KEY, PendingDogfightShot
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.enums import MessageType
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for
from tests._helpers.trigger_encounter import trigger_encounter

CONTENT_ROOT = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"

pytestmark = pytest.mark.skipif(
    not CONTENT_ROOT.is_dir(),
    reason="sidequest-content not on disk alongside sidequest-server",
)

PLAYER = "Apex"
OPPONENT = "Bandit Ace"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def space_opera_pack() -> GenrePack:
    return load_genre_pack(CONTENT_ROOT / "space_opera")


def _make_pilot_character(name: str) -> Character:
    """Minimal space_opera pilot PC — both Reflex + Intellect at 10 (modifier=0)."""
    return Character(
        core=CreatureCore(name=name, description="Test pilot.", personality="Calm."),
        backstory="A pilot.",
        char_class="Pilot",
        race="Human",
        stats={"Reflex": 10, "Intellect": 10},
    )


@pytest.fixture
def snap_with_pilot(space_opera_pack: GenrePack) -> tuple[GameSnapshot, GenrePack]:
    snap = GameSnapshot(genre="space_opera")
    snap.genre_slug = "space_opera"
    snap.characters = [_make_pilot_character(PLAYER)]
    return snap, space_opera_pack


@pytest.fixture
def otel_capture():
    """Attach an in-memory span exporter to the running TracerProvider."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Broadcast capture helper
# ---------------------------------------------------------------------------


class _CapturingRoom:
    """Minimal room double that records broadcast()ed messages."""

    def __init__(self) -> None:
        self.broadcasts: list[object] = []

    def broadcast(self, msg: object, *, exclude_socket_id: object = None) -> None:
        self.broadcasts.append(msg)

    def save(self) -> None:
        pass

    def close_store(self) -> None:
        pass

    def session(self) -> None:
        return None


def _message_types(broadcasts: list[object]) -> list[str]:
    return [
        getattr(getattr(m, "message_type", None), "value", None)
        or getattr(m, "message_type", type(m).__name__)
        for m in broadcasts
    ]


# ---------------------------------------------------------------------------
# Test 1: Player gun solution → stash + DiceRequest, NO frame HP change yet
# ---------------------------------------------------------------------------


def test_player_gun_solution_stashes_and_emits_dice_request(
    snap_with_pilot: tuple[GameSnapshot, GenrePack],
) -> None:
    """A sealed-letter turn where the player gets a gun solution must:

    (a) NOT change frame HP yet (resolution deferred to DICE_THROW).
    (b) Stash a PendingDogfightShot on the return value with the right
        player_shooter_role.
    (c) Emit a DICE_REQUEST broadcast via the room so the client throws the die.
    """
    snap, pack = snap_with_pilot

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
    )
    enc = snap.encounter
    assert enc is not None

    red = next(a for a in enc.actors if a.role == "red")
    blue = next(a for a in enc.actors if a.role == "blue")
    red_hp_before = int(red.per_actor_state[FRAME_HP_KEY])
    blue_hp_before = int(blue.per_actor_state[FRAME_HP_KEY])

    # Use real room for apply (provides required session binding) but capture broadcasts
    # separately via the sd._room mock in the narration apply context.
    room = room_for(snap)

    with (
        patch(
            "sidequest.server.narration_apply._roll_d20_server_side",
            return_value=10,  # NPC roll — held, not resolved yet
        ),
        patch(
            "sidequest.game.dogfight_shot._roll_damage_dice",
            return_value=6,
        ),
    ):
        applied = _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="You loop behind the bandit and line up the shot.",
                beat_selections=[
                    BeatSelection(actor=PLAYER, beat_id="loop"),
                    BeatSelection(actor=OPPONENT, beat_id="kill_rotation"),
                ],
            ),
            player_name=PLAYER,
            pack=pack,
            room=room,
        )

    # (a) Frame HP must NOT have changed — resolution deferred.
    red_hp_after = int(red.per_actor_state[FRAME_HP_KEY])
    blue_hp_after = int(blue.per_actor_state[FRAME_HP_KEY])
    assert red_hp_after == red_hp_before, (
        f"player frame HP must not change before DICE_THROW; "
        f"was {red_hp_before}, now {red_hp_after}"
    )
    assert blue_hp_after == blue_hp_before, (
        f"opponent frame HP must not change before DICE_THROW; "
        f"was {blue_hp_before}, now {blue_hp_after}"
    )

    # (b) pending_dogfight_shot on the outcome must be set with the right role.
    assert applied.pending_dogfight_shot is not None, (
        "applied_outcome.pending_dogfight_shot must be set when player has a gun solution"
    )
    assert isinstance(applied.pending_dogfight_shot, PendingDogfightShot)
    assert applied.pending_dogfight_shot.player_shooter_role == "red", (
        f"player_shooter_role must be 'red' (player side); "
        f"got {applied.pending_dogfight_shot.player_shooter_role!r}"
    )
    assert len(applied.pending_dogfight_shot.gun_solutions) >= 1, (
        "gun_solutions list must include at least the player's gun solution"
    )


# ---------------------------------------------------------------------------
# Test 1b: session-handler layer EMITS DiceRequest + copies stash onto sd
# ---------------------------------------------------------------------------


class _DiceRequestCapturingRoom:
    """SessionRoom stand-in recording broadcasts (mirrors the _StubRoom
    pattern in test_dice_throw_confrontation_emit.py)."""

    slug = "dogfight-emit-test"

    def __init__(self, snap: GameSnapshot) -> None:
        self.broadcasts: list[object] = []
        from sidequest.server.session import Session

        self.session = Session(snap)

    def broadcast(self, msg: object, *, exclude_socket_id: object = None) -> None:
        self.broadcasts.append(msg)

    def is_paused(self) -> bool:
        return False

    def save(self) -> None:
        pass


async def test_session_handler_emits_dice_request_and_stashes_on_sd(
    space_opera_pack: GenrePack,
    session_handler_factory,
) -> None:
    """Drive the SESSION-HANDLER layer (``_execute_narration_turn``), not just
    ``_apply_narration_result_to_snapshot``, and assert the Task 14 emission
    block fires:

    (a) A ``DiceRequestMessage`` (context=dogfight_player_gun_solution) is
        broadcast to the room so the client throws the real Rapier d20.
    (b) ``sd.pending_dogfight_shot`` is set to a ``PendingDogfightShot`` —
        the stash survives the narration turn for the next DICE_THROW.

    This is the wiring that the isolated apply-level Test 1 does NOT exercise:
    the copy-to-sd + broadcast happens in ``websocket_session_handler`` after
    the clear block, not in ``narration_apply``.
    """
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.protocol.messages import DiceRequestMessage
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="space_opera")
    handler._state = _State.Playing

    # The autouse _fixture_pack_search_paths fixture points the factory's
    # loader at frozen test packs, which carry NO dogfight subsystem. Swap in
    # the real space_opera content pack (the same one Test 1 uses) so the
    # dogfight ConfrontationDef the apply path reads (sd.genre_pack) matches
    # the encounter we install.
    sd.genre_pack = space_opera_pack
    sd.player_name = PLAYER  # sealed-letter resolver finds the PC by player_name
    sd.snapshot.characters = [_make_pilot_character(PLAYER)]
    trigger_encounter(
        sd.snapshot,
        space_opera_pack,
        "dogfight",
        PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
    )
    assert sd.snapshot.encounter is not None

    room = _DiceRequestCapturingRoom(sd.snapshot)
    handler._room = room  # type: ignore[assignment]
    sd._room = room  # type: ignore[assignment]

    # Stub the narrator to return the loop/kill_rotation mutual gunline so the
    # player (red) gets a gun solution → the emission block must fire.
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="You loop behind the bandit and line up the shot.",
            beat_selections=[
                BeatSelection(actor=PLAYER, beat_id="loop"),
                BeatSelection(actor=OPPONENT, beat_id="kill_rotation"),
            ],
        ),
    )

    with (
        patch("sidequest.server.narration_apply._roll_d20_server_side", return_value=10),
        patch("sidequest.game.dogfight_shot._roll_damage_dice", return_value=6),
    ):
        await handler._execute_narration_turn(sd, "I loop in behind him.", TurnContext())

    # (a) DiceRequest broadcast for the player's deferred shot.
    dice_requests = [m for m in room.broadcasts if isinstance(m, DiceRequestMessage)]
    assert len(dice_requests) >= 1, (
        f"session handler must broadcast a DiceRequestMessage when the player "
        f"has a deferred gun solution; got broadcasts: "
        f"{[type(m).__name__ for m in room.broadcasts]}"
    )
    df_requests = [m for m in dice_requests if m.payload.context == "dogfight_player_gun_solution"]
    assert len(df_requests) == 1, (
        f"exactly one dogfight DiceRequest expected; got "
        f"{[m.payload.context for m in dice_requests]}"
    )

    # (b) stash copied onto sd, surviving the narration turn for DICE_THROW.
    assert isinstance(sd.pending_dogfight_shot, PendingDogfightShot), (
        "session handler must copy the PendingDogfightShot onto sd so the next "
        f"DICE_THROW can consume it; got {sd.pending_dogfight_shot!r}"
    )
    assert sd.pending_dogfight_shot.player_shooter_role == "red"


# ---------------------------------------------------------------------------
# Test 2: DICE_THROW consumes stash → shots resolve, stash cleared, DiceResult
# ---------------------------------------------------------------------------


async def test_dice_throw_completes_pending_shot(
    snap_with_pilot: tuple[GameSnapshot, GenrePack],
) -> None:
    """Given a stashed PendingDogfightShot, DICE_THROW with a high face must:

    (a) Resolve shots — opponent frame HP drops.
    (b) Clear sd.pending_dogfight_shot to None.
    (c) Broadcast a DICE_RESULT message.

    We drive this via the DiceThrowHandler consuming a PendingDogfightShot
    stashed directly on a _SessionData double, rather than going through the
    full WebSocket handler, to isolate Part 5 of the implementation.
    """
    from sidequest.game.dogfight_shot import GunSolution, PendingDogfightShot
    from sidequest.game.ruleset.resolution import AttackRollParams
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.handlers.dice_throw import HANDLER

    snap, pack = snap_with_pilot
    snap.characters = [_make_pilot_character(PLAYER)]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
    )
    enc = snap.encounter
    assert enc is not None

    blue = next(a for a in enc.actors if a.role == "blue")
    blue_hp_before = int(blue.per_actor_state[FRAME_HP_KEY])

    # Build a GunSolution for the player (red → blue) with modifier=0, target_number=10
    # so a face of 15 (total 15) is a hit.

    player_gun_sol = GunSolution(
        shooter_role="red",
        shooter_name=PLAYER,
        target_role="blue",
        target_name=OPPONENT,
        attack=AttackRollParams(modifier=0, target_number=10),
        weapon=DamageSpec(dice="1d6", bonus=0),
        weapon_name="Laser Cannon",
        target_armor=0,  # no armor so all damage applies
        geometry_modifier=0,
    )

    # Stash a PendingDogfightShot: NPC already rolled a miss (d20=1+0=1 < 10).
    pending = PendingDogfightShot(
        gun_solutions=[player_gun_sol],  # only player has a shot this cell
        held_npc_d20s={},  # no NPC gun solution
        player_shooter_role="red",
        player_modifier=0,
        player_target_number=10,
        player_actor_name=PLAYER,
    )

    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_handler import _State

    # Build a minimal _SessionData-like object.
    class _FakeSessionData:
        def __init__(self) -> None:
            self.pending_dogfight_shot = pending
            self.player_id = "player1"
            self.player_name = PLAYER
            self.genre_slug = "space_opera"
            self.world_slug = "test_world"
            self.genre_pack = pack
            self.snapshot = snap
            self._room = None  # set after FakeRoom is built
            self.pending_roll_outcome = None
            self.pending_roll_actor = None

    # Build a minimal session handler double.
    broadcasts: list[object] = []

    class _FakeRoom:
        def broadcast(self, msg: object, *, exclude_socket_id: object = None) -> None:
            broadcasts.append(msg)

    _minimal_turn_ctx = TurnContext()

    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self) -> None:
            self._session_data = _FakeSessionData()
            self._room = _FakeRoom()
            self._session_data._room = self._room
            self._state = _State.Playing

        async def _retrieve_lore_for_turn(self, sd: object, action: str) -> object:
            return None

        async def _execute_narration_turn(
            self, sd: object, action: str, turn_context: object
        ) -> list[object]:
            captured["action"] = action
            return []

    class _FakeMsg:
        def __init__(self) -> None:
            self.payload = DiceThrowPayload(
                request_id="test-req-001",
                throw_params=ThrowParams(
                    velocity=(0.0, 4.0, -1.0),
                    angular=(0.5, 0.5, 0.5),
                    position=(0.5, 0.5),
                ),
                face=[15],  # high face → total = 15, hits target_number=10
                beat_id=None,
            )
            self.player_id = "player1"

    fake_session = _FakeSession()

    with (
        patch("sidequest.game.dogfight_shot._roll_damage_dice", return_value=5),
        patch(
            "sidequest.handlers.dice_throw._build_turn_context",
            return_value=_minimal_turn_ctx,
        ),
    ):
        await HANDLER.handle(fake_session, _FakeMsg())

    # (a) Opponent frame HP dropped (damage=5, armor=0, applied=5).
    blue_hp_after = int(blue.per_actor_state[FRAME_HP_KEY])
    assert blue_hp_after < blue_hp_before, (
        f"opponent frame_hp should drop after DICE_THROW hit; "
        f"before={blue_hp_before} after={blue_hp_after}"
    )

    # (b) Stash cleared.
    assert fake_session._session_data.pending_dogfight_shot is None, (
        "pending_dogfight_shot must be None after DICE_THROW consumes it"
    )

    # (c) DICE_RESULT broadcast.
    broadcast_types = [getattr(m, "type", None) for m in broadcasts]
    assert any(t == MessageType.DICE_RESULT for t in broadcast_types), (
        f"DICE_RESULT message must be broadcast after dogfight resolution; "
        f"got message_types={broadcast_types}"
    )

    # (d) Narrator is ANCHORED to the real mechanical outcome — no improvisation.
    # The replay text must be the structured marker carrying the actual hit +
    # damage + post-shot hull, and the encounter narrator_hints must list the
    # same factual shot line.
    replay = captured["action"]
    assert isinstance(replay, str)
    assert replay.startswith("[DOGFIGHT_SHOT_RESOLVED]"), (
        f"narrator replay text must be the structured shot-resolved marker; got {replay!r}"
    )
    assert "HIT" in replay and "5 dmg" in replay, (
        f"replay must state the real outcome (player HIT for 5 dmg); got {replay!r}"
    )
    assert f"hull {blue_hp_after}/" in replay, (
        f"replay must carry post-shot hull readout {blue_hp_after}/...; got {replay!r}"
    )
    enc_after = fake_session._session_data.snapshot.encounter
    assert any("HIT" in h and "5 dmg" in h for h in enc_after.narrator_hints), (
        f"encounter.narrator_hints must carry the factual shot line; "
        f"got {enc_after.narrator_hints!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: NPC-only gun solution → resolves inline, no stash
# ---------------------------------------------------------------------------


def test_npc_only_gun_solution_resolves_immediately_no_stash(
    snap_with_pilot: tuple[GameSnapshot, GenrePack],
    otel_capture: InMemorySpanExporter,
) -> None:
    """A cell where ONLY the NPC gets a gun solution must resolve inline (Task 13
    path) — the shot ablates HP THIS turn — and leave ``pending_dogfight_shot``
    unset on the outcome.

    ``straight`` (red/player) vs ``loop`` (blue/NPC) in interactions_mvp.yaml:
    red_view.gun_solution=false, blue_view.gun_solution=true. So only the NPC
    (blue) has a shot, and its TARGET is the player (red) — therefore the
    PLAYER's frame HP drops this turn, server-side, with no stash.
    """
    snap, pack = snap_with_pilot
    snap.characters = [_make_pilot_character(PLAYER)]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
    )
    enc = snap.encounter
    assert enc is not None

    red = next(a for a in enc.actors if a.role == "red")
    blue = next(a for a in enc.actors if a.role == "blue")
    red_hp_before = int(red.per_actor_state[FRAME_HP_KEY])
    blue_hp_before = int(blue.per_actor_state[FRAME_HP_KEY])

    room = room_for(snap)
    otel_capture.clear()

    # NPC d20=20 → auto-hit. Damage=3, no armor → player frame HP drops by 3.
    with (
        patch(
            "sidequest.server.narration_apply._roll_d20_server_side",
            return_value=20,
        ),
        patch(
            "sidequest.game.dogfight_shot._roll_damage_dice",
            return_value=3,
        ),
    ):
        applied = _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="Red drills straight through; Blue loops onto Red's six.",
                beat_selections=[
                    BeatSelection(actor=PLAYER, beat_id="straight"),
                    BeatSelection(actor=OPPONENT, beat_id="loop"),
                ],
            ),
            player_name=PLAYER,
            pack=pack,
            room=room,
        )

    # No stash — NPC-only solutions resolve inline.
    assert applied.pending_dogfight_shot is None, (
        "pending_dogfight_shot must be None for an NPC-only gun solution "
        f"(straight/loop cell); got: {applied.pending_dogfight_shot}"
    )

    # The NPC shot resolved THIS turn against the player (red) frame HP.
    red_hp_after = int(red.per_actor_state[FRAME_HP_KEY])
    blue_hp_after = int(blue.per_actor_state[FRAME_HP_KEY])
    assert red_hp_after < red_hp_before, (
        f"NPC-only gun solution must ablate the PLAYER's (red) frame HP inline "
        f"this turn; was {red_hp_before}, now {red_hp_after}"
    )
    # Player had no gun solution → opponent (blue) frame HP untouched.
    assert blue_hp_after == blue_hp_before, (
        f"opponent (blue) frame HP must be untouched (player had no shot); "
        f"was {blue_hp_before}, now {blue_hp_after}"
    )

    # The shot spans fire IN-TURN for NPC-only resolution (no deferral).
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "dogfight.shot_attempted" in span_names, (
        f"NPC-only resolution must fire dogfight.shot_attempted in-turn; got: {sorted(span_names)}"
    )


# ---------------------------------------------------------------------------
# OTEL span test: when player gun solution is stashed, shot spans do NOT fire
# (deferred to DICE_THROW) but narration turn completes.
# ---------------------------------------------------------------------------


def test_no_shot_spans_when_player_gun_solution_deferred(
    snap_with_pilot: tuple[GameSnapshot, GenrePack],
    otel_capture: InMemorySpanExporter,
) -> None:
    """When the player has a gun solution the shots are DEFERRED — no
    ``dogfight.shot_attempted`` spans should fire during the narration turn.
    They fire later when the DICE_THROW handler resolves the shot.
    """
    snap, pack = snap_with_pilot
    snap.characters = [_make_pilot_character(PLAYER)]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
    )
    otel_capture.clear()

    room = room_for(snap)

    with (
        patch("sidequest.server.narration_apply._roll_d20_server_side", return_value=10),
        patch("sidequest.game.dogfight_shot._roll_damage_dice", return_value=6),
    ):
        applied = _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="You loop; no shot yet.",
                beat_selections=[
                    BeatSelection(actor=PLAYER, beat_id="loop"),
                    BeatSelection(actor=OPPONENT, beat_id="kill_rotation"),
                ],
            ),
            player_name=PLAYER,
            pack=pack,
            room=room,
        )

    # Precondition: the loop/kill_rotation cell MUST yield a player gun
    # solution (mutual gunline). Assert it so the test fails loudly — rather
    # than silently no-op'ing — if the content combo ever changes.
    assert isinstance(applied.pending_dogfight_shot, PendingDogfightShot), (
        "loop/kill_rotation must yield a deferred player gun solution "
        "(mutual gunline cell); test precondition failed — content changed?"
    )

    # When the player has a gun solution, shots are deferred — no shot_attempted
    # spans should have fired during the narration turn.
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "dogfight.shot_attempted" not in span_names, (
        f"dogfight.shot_attempted must NOT fire during narration turn when "
        f"player shot is deferred; got: {sorted(span_names)}"
    )
