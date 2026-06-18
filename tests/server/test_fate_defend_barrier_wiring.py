"""WIRING (spec 2026-06-18 §11, story 126-8) — the mandatory end-to-end net.

NPC-attacks-PC → FATE_DEFEND_REQUEST → player defend FATE_THROW → resolve →
narrate, all through the REAL FateThrowHandler, the runtime registry, and the
exchange walker. The NPC path still server-rolls; the player defense never does;
an absent defender holds the barrier; a suspended exchange survives a reload
without re-rolling the locked NPC dice.

Behavioral fixtures + registry reflection only — never a source grep (server
CLAUDE.md). The registry tripwire passes today; the rest is RED until the barrier
production lands (plan Tasks 1,4,5,6,7).
"""

from __future__ import annotations

import asyncio

import sidequest.game.ruleset.fate_resolution as fate_resolution
from sidequest.game.session import GameSnapshot
from sidequest.handlers.fate_throw import HANDLER as FATE_THROW_HANDLER
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import FateRollMessage
from sidequest.server.websocket_session_handler import WebSocketSessionHandler
from tests._helpers.fate_session import _make_defend_throw, playing_session_with_fate_conflict


def _drain(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_fate_throw_is_registered_to_its_handler():
    # Legitimate reflection wiring net (passes today): FATE_THROW resolves to the
    # real handler singleton — the defend path rides this same channel.
    assert WebSocketSessionHandler._message_handler_for("FATE_THROW") is FATE_THROW_HANDLER


def test_full_defend_round_through_real_handlers(monkeypatch):
    from sidequest.protocol.messages import FateDefendRequestMessage

    session, proactive_throw, q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", attack_targets="Rux", action="attack", target="Bandit"
    )
    sd = session._session_data

    calls = {"server": 0}
    real = fate_resolution.roll_4df
    monkeypatch.setattr(
        fate_resolution,
        "roll_4df",
        lambda rng: calls.__setitem__("server", calls["server"] + 1) or real(rng),
    )

    # 1) Proactive throw closes the barrier → REVEAL → park → defend request.
    asyncio.run(FATE_THROW_HANDLER.handle(session, proactive_throw))
    sent = _drain(q)
    req = next(m for m in sent if isinstance(m, FateDefendRequestMessage))
    enc = sd.snapshot.encounter
    assert enc.resolved is False
    assert len(enc.pending_defenses) == 1
    assert sd.orchestrator.calls == 0  # no narration at the park

    # 2) The defender answers with FATE_THROW(action="defend"), echoing request_id,
    #    free-picking a defense skill and reporting the four settled faces.
    defend_throw = _make_defend_throw(
        session, request_id=req.payload.request_id, skill="Athletics", faces=(1, 1, 0, 0)
    )
    asyncio.run(FATE_THROW_HANDLER.handle(session, defend_throw))
    after = _drain(q)

    # 3) Ledger cleared (resumed), narrator invoked EXACTLY once at RESOLVE.
    assert enc.pending_defenses == []
    assert sd.orchestrator.calls == 1

    # 4) Source split held: the player's defense roll IS the thrown faces (never a
    #    server roll), while the NPC path used the server RNG in the same round.
    defense_rolls = [m for m in after if isinstance(m, FateRollMessage)]
    assert any(tuple(m.payload.dice) == (1, 1, 0, 0) for m in defense_rolls)
    assert calls["server"] >= 1  # NPC seating / NPC defense still server-rolls


def test_absent_defender_holds_the_barrier():
    session, proactive_throw, q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", attack_targets="Rux", action="attack", target="Bandit"
    )
    sd = session._session_data

    asyncio.run(FATE_THROW_HANDLER.handle(session, proactive_throw))
    sent = _drain(q)

    enc = sd.snapshot.encounter
    # No defend throw arrives. The barrier stays open; nothing auto-resolves and no
    # server-side auto-roll fills it (No Silent Fallbacks: block-and-wait AFK).
    assert enc.resolved is False
    assert any(p.defense_total is None and not p.conceded for p in enc.pending_defenses)
    assert sd.orchestrator.calls == 0
    assert not [m for m in sent if getattr(m, "type", None) == MessageType.NARRATION]


def test_parked_exchange_survives_reload_without_rerolling_npc():
    session, proactive_throw, _q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", attack_targets="Rux", action="attack", target="Bandit"
    )
    sd = session._session_data

    # Drive to the parked state, capture the locked NPC dice, round-trip the snapshot.
    asyncio.run(FATE_THROW_HANDLER.handle(session, proactive_throw))
    enc = sd.snapshot.encounter
    npc_commit = next(c for c in enc.fate_commits if c.actor == "Bandit")
    locked_dice = npc_commit.dice

    reloaded = GameSnapshot.model_validate_json(sd.snapshot.model_dump_json())
    rc = next(c for c in reloaded.encounter.fate_commits if c.actor == "Bandit")
    assert rc.dice == locked_dice  # NPC dice are LOCKED across the suspend
    assert reloaded.encounter.pending_defenses[0].defense_total is None
