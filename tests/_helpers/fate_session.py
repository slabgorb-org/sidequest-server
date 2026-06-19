"""Playing-session harness for the Fate DEFEND barrier (story 126-8).

Mirrors ``tests/server/test_fate_throw_handler_wiring.py``'s ``_fate_session()``:
a ``SimpleNamespace`` session double in ``_State.Playing`` with a real
``SessionRoom`` whose outbound queue we drain to see broadcasts. Tests drive the
REAL ``FateThrowHandler`` (``HANDLER.handle(session, msg)``) so the wiring runs
through production code, and assert the registry resolves FATE_THROW to that same
handler (the legitimate reflection exception, not a source grep).

Differs from the 126-7 harness in two ways the DEFEND barrier needs:
  1. The opponent is LIVE (fresh sheet) so it is seated attacking the PC at
     REVEAL and the round PARKS instead of taking the foe out immediately.
  2. ``sd.orchestrator`` is a counting fake — the resume→narrate step must invoke
     the narrator exactly once at RESOLVE; the spy lets a test assert that floor
     without driving the full (heavy) narration-emit pipeline.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.dice import ThrowParams
from sidequest.protocol.fate import FateThrowPayload
from sidequest.protocol.messages import FateThrowMessage
from sidequest.server.session_handler import _State
from sidequest.server.session_room import SessionRoom


class FakeOrchestrator:
    """Counts narration invocations so a test can assert the RESOLVE floor of
    exactly-one narrator call per resumed round (spec 2026-06-18 §3 step 5)."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_narration_turn(self, action, context, **kwargs):  # noqa: ANN001
        self.calls += 1
        return SimpleNamespace(narration="(woven exchange)", is_degraded=False, agent_duration_ms=0)


async def _fake_narrate_resolved_fate_exchange(sd, action):  # noqa: ANN001, ARG001
    """Stand-in for ``WebSocketSessionHandler._narrate_resolved_fate_exchange`` on
    the SimpleNamespace session double.

    The real method runs ``_build_turn_context`` + ``_execute_narration_turn``, which
    invokes the orchestrator exactly once. The double drives the SAME counting
    ``FakeOrchestrator`` so the RESOLVE-floor assertion (exactly-one narrator call per
    resumed round) stays faithful — without standing up the heavy narration-emit
    pipeline that a lightweight session double can't drive."""
    await sd.orchestrator.run_narration_turn(action, None)
    return []


def _throw_params() -> ThrowParams:
    return ThrowParams(velocity=(0.0, 4.0, -1.0), angular=(0.5, 0.5, 0.5), position=(0.5, 0.5))


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _healthy_npc(name: str, skills: dict[str, int]) -> Npc:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Npc(core=core)


def _room_with_seat(player_id: str):
    # Unbound room (no bind_world): the narrate-at-RESOLVE step is delegated to the
    # session's _narrate_resolved_fate_exchange (faked on the double), so
    # _build_turn_context never runs here and room.save() is a safe no-op while
    # unbound. Mirrors the canonical 126-7 harness _room_with_seat.
    room = SessionRoom(slug="slug-fate-defend-wire", mode=GameMode.MULTIPLAYER)
    q: asyncio.Queue = asyncio.Queue()
    room.connect(player_id, socket_id="sock-1")
    room.attach_outbound("sock-1", q)
    return room, q


def playing_session_with_fate_conflict(
    *,
    actor: str = "Rux",
    attacker_npc: str = "Bandit",
    attack_targets: str = "Rux",  # noqa: ARG001 — documents intent; targeting is deterministic
    action: str = "attack",
    target: str = "Bandit",
    player_id: str = "p1",
):
    """Build (session, proactive_throw_msg, outbound_queue) for a PC-vs-live-NPC
    Fate conflict. The proactive throw closes the barrier → REVEAL seats the
    opponent attacking the PC → the round parks at DEFEND."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name=actor, role="lead", side="player"),
            EncounterActor(name=attacker_npc, role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc(actor, {"Fight": 2, "Athletics": 2, "Notice": 1})],
        encounter=enc,
    )
    snap.npcs.append(_healthy_npc(attacker_npc, {"Fight": 2, "Athletics": 1, "Notice": 1}))
    snap.player_seats[player_id] = actor

    room, q = _room_with_seat(player_id)
    # Minimal sd — mirrors the canonical 126-7 Fate handler-wiring harness. The
    # narrate-at-RESOLVE step is delegated to the session double's faked
    # _narrate_resolved_fate_exchange, so the heavy _build_turn_context session state
    # isn't needed here; only the mechanical Fate path reads these fields.
    sd = SimpleNamespace(
        snapshot=snap,
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
        genre_slug="fate_test",
        world_slug="test_world",
        player_id=player_id,
        orchestrator=FakeOrchestrator(),
        _room=room,
    )
    session = SimpleNamespace(
        _state=_State.Playing,
        _session_data=sd,
        _room=room,
        _narrate_resolved_fate_exchange=_fake_narrate_resolved_fate_exchange,
    )

    throw_msg = FateThrowMessage(
        payload=FateThrowPayload(
            request_id="a1",
            action=action,
            skill="Fight",
            target=target,
            throw_params=_throw_params(),
            face=(0, 0, 0, 0),
        ),
        player_id=player_id,
    )
    return session, throw_msg, q


def _make_defend_throw(session, *, request_id: str, skill: str, faces: tuple[int, int, int, int]):
    """A FATE_THROW answering a defend request: action="defend", the echoed
    request_id, a free-picked defense skill, and the four settled faces.

    During RED this RAISES at construction — ``action="defend"`` is not yet in the
    ``FateThrowPayload.action`` Literal — which is the intended missing-production
    failure for the defend-path tests."""
    pid = session._session_data.player_id
    return FateThrowMessage(
        payload=FateThrowPayload(
            request_id=request_id,
            action="defend",
            skill=skill,
            throw_params=_throw_params(),
            face=faces,
        ),
        player_id=pid,
    )
