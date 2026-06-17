"""Wiring net for the FATE_THROW channel (Story 126-7 / ADR-148).

Two kinds of assertion, neither a source grep (server CLAUDE.md):
  1. Registry membership — FATE_THROW resolves to the real FateThrowHandler
     singleton via the runtime registry (the legitimate reflection exception).
  2. End-to-end — driving HANDLER.handle on a Fate-bound fake session reaches
     dispatch_fate_action with the thrown faces and BROADCASTS a FATE_ROLL whose
     dice ARE the player's thrown faces (authoritative) and whose throw_params
     echo the thrower's gesture (so every seat replays the same tumble).

The NPC opponent in the same exchange still server-rolls (``roll_4df``): the
player path is determinative, the NPC path is not. Modeled on
``tests/server/test_fate_action_handler_wiring.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import sidequest.game.ruleset.fate_resolution as fr
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot, Npc
from sidequest.handlers.fate_throw import HANDLER as FATE_THROW_HANDLER
from sidequest.protocol.dice import ThrowParams
from sidequest.protocol.fate import FateThrowPayload
from sidequest.protocol.messages import FateRollMessage, FateThrowMessage
from sidequest.server.session_handler import _State
from sidequest.server.session_room import SessionRoom
from sidequest.server.websocket_session_handler import WebSocketSessionHandler


def _room_with_seat(player_id: str):
    room = SessionRoom(slug="slug-fate-throw-wire", mode=GameMode.MULTIPLAYER)
    q: asyncio.Queue = asyncio.Queue()
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


def _fate_session():
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
    return session, enc, q


def _throw_msg() -> FateThrowMessage:
    return FateThrowMessage(
        payload=FateThrowPayload(
            request_id="r1",
            action="attack",
            skill="Fight",
            target="Thug",
            throw_params=ThrowParams(
                velocity=(0.0, 4.0, -1.0), angular=(0.5, 0.5, 0.5), position=(0.5, 0.5)
            ),
            face=(1, 1, 0, -1),
        ),
        player_id="p1",
    )


def test_fate_throw_is_registered_to_its_handler():
    # Registry built lazily on first lookup; force it via the resolver.
    handler = WebSocketSessionHandler._message_handler_for("FATE_THROW")
    assert handler is FATE_THROW_HANDLER


def test_fate_throw_roundtrips_to_fate_roll():
    session, enc, q = _fate_session()

    out = asyncio.run(FATE_THROW_HANDLER.handle(session, _throw_msg()))

    # The roll is BROADCAST to the table (118-7 contract), not returned.
    assert out == []
    rolls = _drain_rolls(q)
    assert len(rolls) == 1
    roll = rolls[0].payload
    # The player's thrown faces ARE the authoritative roll (physics-is-the-roll).
    assert tuple(roll.dice) == (1, 1, 0, -1)
    assert roll.roll_total == 1
    # The thrower's gesture is echoed so every seat replays the same tumble.
    assert roll.throw_params.velocity == (0.0, 4.0, -1.0)
    # Dispatch ran end-to-end: the depleted foe was taken out and the conflict ended.
    assert enc.find_actor("Thug").withdrawn is True
    assert enc.resolved is True


def test_npc_path_still_server_rolls_in_same_exchange(monkeypatch):
    # In the SAME exchange the player throws determinatively, the NPC opponent is
    # seated and defends via the server RNG (roll_4df). The player's broadcast
    # dice equal the thrown faces (not a roll_4df product), while roll_4df fires
    # for the NPC — proving the source split holds end-to-end.
    calls = {"n": 0}
    real = fr.roll_4df

    def spy(rng):
        calls["n"] += 1
        return real(rng)

    monkeypatch.setattr(fr, "roll_4df", spy)

    session, _enc, q = _fate_session()
    out = asyncio.run(FATE_THROW_HANDLER.handle(session, _throw_msg()))

    assert out == []
    rolls = _drain_rolls(q)
    assert len(rolls) == 1
    assert tuple(rolls[0].payload.dice) == (1, 1, 0, -1)  # player faces, not server-rolled
    assert calls["n"] >= 1, "the NPC opponent still server-rolls in the same exchange"
