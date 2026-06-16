"""RED — FATE_ROLL broadcast + attribution (Story 118-7 / ADR-144 F3g).

Story 118-7 carries the deferred 118-3 (F3c) findings: the acting PC's 4dF roll
must reach the *whole table*, not just the socket that sent the FATE_ACTION.

Today ``FateActionHandler.handle`` *returns* ``[FateRollMessage(...)]``. Returned
messages ride the per-socket ``out_queue`` (websocket.py:133-135) — sender-only
delivery — and the message carries no ``player_id`` (defaults to ``""``). Two
gaps, both observable, neither a source grep (server CLAUDE.md "No Source-Text
Wiring Tests"):

  AC2 (broadcast)   — the roll must fan out via ``SessionRoom.broadcast`` (like
                      DICE_RESULT, websocket_session_handler.py:1286) so peers
                      see the soloist's roll (SOUL "The Guitar Solo": the
                      soloist's roll is visible to the table). A broadcast with
                      ``exclude_socket_id=None`` reaches every seat *including*
                      the actor, so the handler must NOT also return the message
                      (that would double-deliver to the sender).
  AC3 (attribution) — the broadcast ``FateRollMessage`` must be stamped with the
                      acting (server-authenticated) ``player_id`` so a consumer
                      can attribute whose roll it is.

These tests drive the REAL handler against a REAL ``SessionRoom`` with two
attached outbound queues and inspect what each queue actually received — the
fan-out is the behavior under test, refactor-stable, agnostic to whether the
broadcast call lives in the handler or the dispatch layer.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot, Npc
from sidequest.handlers.fate_action import HANDLER as FATE_HANDLER
from sidequest.protocol.fate import FateActionPayload
from sidequest.protocol.messages import FateActionMessage, FateRollMessage
from sidequest.server.session_handler import _State
from sidequest.server.session_room import SessionRoom

ACTOR_PID = "p1"
PEER_PID = "p2"


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _depleted_thug() -> Npc:
    """A foe with every stress box checked and consequences taken — the next hit
    resolves the exchange, so ``run_fate_exchange`` produces a real action roll."""
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def _fate_encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )


def _room_with_two_seats() -> tuple[SessionRoom, asyncio.Queue, asyncio.Queue]:
    """A live MP room with the actor and a peer connected, each with an attached
    outbound queue — ``broadcast`` delivers into exactly these queues."""
    room = SessionRoom(slug="slug-118-7", mode=GameMode.MULTIPLAYER)
    actor_q: asyncio.Queue = asyncio.Queue()
    peer_q: asyncio.Queue = asyncio.Queue()
    room.connect(ACTOR_PID, socket_id="sock-actor")
    room.connect(PEER_PID, socket_id="sock-peer")
    room.attach_outbound("sock-actor", actor_q)
    room.attach_outbound("sock-peer", peer_q)
    return room, actor_q, peer_q


def _session_with_room(room: SessionRoom) -> SimpleNamespace:
    """Minimal fake session the handler reads: ``_state``, ``_session_data``
    (snapshot, genre_pack.rules.ruleset, player_id), and the room. ``_room`` is
    set on both the session and its session_data because production plumbs the
    SessionRoom onto both (websocket_session_handler.py:284 + session_state.py:207)
    and we must not couple the test to which attribute the handler reads."""
    enc = _fate_encounter()
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 4})], encounter=enc
    )
    snap.npcs.append(_depleted_thug())
    sd = SimpleNamespace(
        snapshot=snap,
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
        genre_slug="fate_test",
        world_slug="test_world",
        player_id=ACTOR_PID,
        _room=room,
    )
    return SimpleNamespace(_state=_State.Playing, _session_data=sd, _room=room)


def _attack_msg() -> FateActionMessage:
    return FateActionMessage(
        payload=FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug"),
        player_id=ACTOR_PID,
    )


def _drain(q: asyncio.Queue) -> list[object]:
    out: list[object] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# --- AC2: broadcast fan-out -------------------------------------------------


def test_roll_is_broadcast_to_the_peer_seat():
    """The peer (who did NOT send the action) must receive the acting PC's roll —
    otherwise the table can't see the soloist's roll (SOUL Guitar Solo)."""
    room, _actor_q, peer_q = _room_with_two_seats()
    session = _session_with_room(room)

    out = asyncio.run(FATE_HANDLER.handle(session, _attack_msg()))

    peer_msgs = [m for m in _drain(peer_q) if isinstance(m, FateRollMessage)]
    assert len(peer_msgs) == 1, "peer seat must receive exactly one FATE_ROLL broadcast"
    # And the handler must NOT also return it (broadcast + return == double-deliver
    # to the sender, since exclude_socket_id=None already reaches the actor).
    assert out == [], "handler must broadcast, not return the roll for sender-only delivery"


def test_roll_reaches_the_acting_seat_too():
    """``broadcast(exclude_socket_id=None)`` reaches EVERY seat including the
    actor — the soloist sees their own roll (Guitar Solo). The actor must get the
    roll exactly once (via the broadcast, not also via a returned message)."""
    room, actor_q, _peer_q = _room_with_two_seats()
    session = _session_with_room(room)

    asyncio.run(FATE_HANDLER.handle(session, _attack_msg()))

    actor_msgs = [m for m in _drain(actor_q) if isinstance(m, FateRollMessage)]
    assert len(actor_msgs) == 1, "actor seat must receive the roll exactly once (no double-deliver)"


def test_broadcast_roll_is_a_real_resolved_4df_roll():
    """The fan-out carries the actual resolved roll, not an empty shell: four
    Fudge faces in {-1,0,1}, a ladder adjective, and a known tier."""
    room, _actor_q, peer_q = _room_with_two_seats()
    session = _session_with_room(room)

    asyncio.run(FATE_HANDLER.handle(session, _attack_msg()))

    roll = next(m for m in _drain(peer_q) if isinstance(m, FateRollMessage)).payload
    assert len(roll.dice) == 4 and all(d in (-1, 0, 1) for d in roll.dice)
    assert roll.ladder_name  # the player reads the adjective, not just the number
    assert roll.tier in ("Fail", "Tie", "Succeed", "SucceedWithStyle")


# --- AC3: attribution -------------------------------------------------------


def test_broadcast_roll_is_stamped_with_the_acting_player_id():
    """The broadcast FATE_ROLL must carry the acting (server-authenticated)
    player_id so a consumer can attribute whose roll it is — today it defaults to
    the empty string."""
    room, _actor_q, peer_q = _room_with_two_seats()
    session = _session_with_room(room)

    asyncio.run(FATE_HANDLER.handle(session, _attack_msg()))

    msg = next(m for m in _drain(peer_q) if isinstance(m, FateRollMessage))
    assert msg.player_id == ACTOR_PID, "FATE_ROLL must be attributed to the acting PC"


# --- Concession: no roll, so no broadcast -----------------------------------


def test_concession_broadcasts_no_roll():
    """A concession is pre-roll (``action_roll is None``) — there is nothing to
    surface, so the handler broadcasts nothing and returns nothing."""
    room, actor_q, peer_q = _room_with_two_seats()
    session = _session_with_room(room)
    concede = FateActionMessage(
        payload=FateActionPayload(request_id="r1", action="concede", skill="Fight"),
        player_id=ACTOR_PID,
    )

    out = asyncio.run(FATE_HANDLER.handle(session, concede))

    assert out == []
    assert [m for m in _drain(actor_q) if isinstance(m, FateRollMessage)] == []
    assert [m for m in _drain(peer_q) if isinstance(m, FateRollMessage)] == []
