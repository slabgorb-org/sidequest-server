"""Wiring net for the FATE_ACTION channel (ADR-144 F1d).

Two assertions, neither a source grep (server CLAUDE.md):
  1. Registry membership — the FATE_ACTION message type resolves to the real
     FateActionHandler singleton (runtime registry check, the legitimate
     reflection exception).
  2. End-to-end — driving HANDLER.handle on a Fate-bound fake session reaches
     dispatch_fate_action → run_fate_exchange (encounter state changes).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot, Npc
from sidequest.handlers.fate_action import HANDLER as FATE_HANDLER
from sidequest.protocol.fate import FateActionPayload
from sidequest.protocol.messages import FateActionMessage
from sidequest.server.session_handler import _State
from sidequest.server.websocket_session_handler import WebSocketSessionHandler


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
    # genre_pack.rules.ruleset, player_id), and snapshot.player_seats.
    sd = SimpleNamespace(
        snapshot=snap,
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
        genre_slug="fate_test",
        world_slug="test_world",
        player_id="p1",
    )
    session = SimpleNamespace(_state=_State.Playing, _session_data=sd)

    msg = FateActionMessage(
        payload=FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug"),
        player_id="p1",
    )

    out = asyncio.run(FATE_HANDLER.handle(session, msg))

    assert out == []  # broadcast/narration is F2/F3; the handler routes + mutates state
    assert enc.find_actor("Thug").withdrawn is True  # dispatch → exchange ran end-to-end
    assert enc.resolved is True
