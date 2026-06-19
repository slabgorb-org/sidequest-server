"""When a proactive FATE_THROW closes the barrier and the round PARKS at DEFEND,
the real handler broadcasts a FATE_DEFEND_REQUEST per incoming attack on a PC and
leaves the exchange unresolved — and triggers NO narration (spec 2026-06-18 §3,§5,
story 126-8).

Drives the REAL FateThrowHandler on a Playing-state session double and drains the
room's outbound queue (behavioral wiring, never a source grep — server CLAUDE.md).

RED: FateDefendRequestMessage does not exist and the handler has no park branch
yet (plan Tasks 1 + 5).
"""

from __future__ import annotations

import asyncio

from sidequest.handlers.fate_throw import HANDLER as FATE_THROW_HANDLER
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import FateDefendRequestMessage
from tests._helpers.fate_session import playing_session_with_fate_conflict


def _drain(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_parked_throw_broadcasts_defend_request():
    session, throw_msg, q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", attack_targets="Rux", action="attack", target="Bandit"
    )

    out = asyncio.run(FATE_THROW_HANDLER.handle(session, throw_msg))
    assert out == []  # broadcasts, never returns

    sent = _drain(q)
    requests = [m for m in sent if isinstance(m, FateDefendRequestMessage)]
    assert len(requests) == 1
    req = requests[0]
    assert req.payload.defender == "Rux"
    assert req.payload.attacker == "Bandit"

    # Parked: unresolved, ledger written, and NO narration was emitted by the park.
    enc = session._session_data.snapshot.encounter
    assert enc.resolved is False
    assert len(enc.pending_defenses) == 1
    assert not [m for m in sent if getattr(m, "type", None) == MessageType.NARRATION]
    # The narrator must not fire at the park — only at RESOLVE.
    assert session._session_data.orchestrator.calls == 0
