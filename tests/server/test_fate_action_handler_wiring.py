"""Wiring net for the FATE_ACTION channel (ADR-144 F1d; broadcast updated F3g).

Two assertions, neither a source grep (server CLAUDE.md):
  1. Registry membership — the FATE_ACTION message type resolves to the real
     FateActionHandler singleton (runtime registry check, the legitimate
     reflection exception).
  2. End-to-end — driving HANDLER.handle on a Fate-bound fake session reaches
     dispatch_fate_action → run_fate_exchange (encounter state changes).

Story 118-7 (F3g) changed the roll-delivery contract: the resolved 4dF roll is
now BROADCAST to the table via ``SessionRoom.broadcast`` (so peers see the
soloist's roll) rather than RETURNED for sender-only ``out_queue`` delivery, so
the end-to-end test now attaches a room and asserts the broadcast + an empty
return (no double-deliver). The broadcast fan-out + player_id attribution detail
lives in ``test_fate_roll_broadcast_wire_118_7.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.persistence import GameMode
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc
from sidequest.handlers.fate_action import HANDLER as FATE_HANDLER
from sidequest.protocol.fate import FateActionPayload
from sidequest.protocol.messages import FateActionMessage, FateRollMessage
from sidequest.server.session_handler import _State
from sidequest.server.session_room import SessionRoom
from sidequest.server.websocket_session_handler import WebSocketSessionHandler
from tests._helpers.fate_fixtures import resolve_parked_defenses


class _FixedRng:
    """Deterministic 4dF stand-in: ``.choice`` returns ``value`` for every die.
    ``_FixedRng(-1)`` rolls the surviving NPC defense to its floor (-4) at RESUME so
    the depleted foe is taken out regardless of the handler's random proactive roll."""

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _room_with_seat(player_id: str):
    """A live MP room with one connected seat + attached outbound queue, so a
    broadcast from the handler has somewhere to land."""
    import asyncio as _asyncio

    room = SessionRoom(slug="slug-fate-wire", mode=GameMode.MULTIPLAYER)
    q: _asyncio.Queue = _asyncio.Queue()
    room.connect(player_id, socket_id="sock-1")
    room.attach_outbound("sock-1", q)
    return room, q


def _drain_rolls(q) -> list[FateRollMessage]:
    out: list[FateRollMessage] = []
    while not q.empty():
        m = q.get_nowait()
        if isinstance(m, FateRollMessage):
            out.append(m)
    return out


def test_fate_action_is_registered_to_its_handler():
    # The registry is built lazily on first lookup; force it via the resolver.
    handler = WebSocketSessionHandler._message_handler_for("FATE_ACTION")
    assert handler is FATE_HANDLER


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _depleted_thug() -> Npc:
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def test_handler_drives_dispatch_end_to_end():
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 4})], encounter=enc
    )
    snap.npcs.append(_depleted_thug())

    # Minimal fake session: the handler reads _state, _session_data (snapshot,
    # genre_pack.rules.ruleset, player_id), snapshot.player_seats, and the room
    # (set on both session and session_data — production plumbs it onto both).
    room, q = _room_with_seat("p1")
    sd = SimpleNamespace(
        snapshot=snap,
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
        genre_slug="fate_test",
        world_slug="test_world",
        player_id="p1",
        _room=room,
    )
    session = SimpleNamespace(_state=_State.Playing, _session_data=sd, _room=room)

    msg = FateActionMessage(
        payload=FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug"),
        player_id="p1",
    )

    out = asyncio.run(FATE_HANDLER.handle(session, msg))

    # F3g (ADR-144 / Story 118-7): the handler BROADCASTS the acting PC's 4dF roll
    # to the table (SOUL Guitar Solo) rather than returning it for sender-only
    # delivery — so the return is empty and the roll lands on the seat's queue.
    assert out == [], "the roll is broadcast, not returned for sender-only delivery"
    rolls = _drain_rolls(q)
    assert len(rolls) == 1
    roll = rolls[0].payload
    assert len(roll.dice) == 4 and all(d in (-1, 0, 1) for d in roll.dice)
    assert roll.ladder_name  # the player reads the adjective, not just the number
    assert roll.tier in ("Fail", "Tie", "Succeed", "SucceedWithStyle")
    # Story 126-8: dispatch parks at the DEFEND barrier (the depleted foe's
    # counter-swing targets the PC). Drive the PC defense + RESUME to reach the
    # resolved end-to-end state. (NOTE: this FATE_ACTION handler does NOT itself emit
    # the FATE_DEFEND_REQUEST on the park — unlike FateThrowHandler — see the
    # session Delivery Findings; the dispatch+resume machinery it routes to works.)
    assert enc.pending_defenses  # parked at the DEFEND barrier
    resolve_parked_defenses(
        encounter=enc, snapshot=snap, ruleset=get_ruleset_module("fate"), rng=_FixedRng(-1)
    )
    assert enc.find_actor("Thug").withdrawn is True  # dispatch → exchange ran end-to-end
    assert enc.resolved is True


def test_handler_concede_emits_no_roll():
    """Concession is pre-roll (non-committing) — there is no 4dF roll to surface,
    so the handler broadcasts nothing (action_roll is None) and returns nothing."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 4})], encounter=enc
    )
    snap.npcs.append(_depleted_thug())
    room, q = _room_with_seat("p1")
    sd = SimpleNamespace(
        snapshot=snap,
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
        genre_slug="fate_test",
        world_slug="test_world",
        player_id="p1",
        _room=room,
    )
    session = SimpleNamespace(_state=_State.Playing, _session_data=sd, _room=room)
    msg = FateActionMessage(
        payload=FateActionPayload(request_id="r1", action="concede", skill="Fight"),
        player_id="p1",
    )

    out = asyncio.run(FATE_HANDLER.handle(session, msg))
    assert out == []  # no roll surfaced on a concession
    assert _drain_rolls(q) == []  # and nothing broadcast to the table
