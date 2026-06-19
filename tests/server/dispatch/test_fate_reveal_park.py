"""REVEAL + park (spec 2026-06-18 §5, story 126-8): when the COMMIT barrier closes
and an NPC attack targets a PC, the server seats + locks the NPC roll, writes
pending_defenses, returns defend requests, and does NOT walk the exchange yet. A
round with no PC targeted resolves immediately (today's single-call path).

RED: FateDispatchResult has no awaiting_defense / defend_requests fields and the
encounter has no pending_defenses ledger yet (plan Task 4).
"""

from __future__ import annotations

import random

from sidequest.game.ruleset import get_ruleset_module
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import dispatch_fate_action
from tests._helpers.fate_fixtures import conflict_with_pc_and_npc


def test_pc_targeted_round_parks_with_defend_request():
    snap, encounter = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    ruleset = get_ruleset_module("fate")

    # The PC commits a proactive attack; the barrier closes (solo-PC table).
    result = dispatch_fate_action(
        payload=FateActionPayload(request_id="a1", action="attack", skill="Fight", target="Bandit"),
        actor_name="Rux",
        encounter=encounter,
        ruleset=ruleset,
        snapshot=snap,
        rng=random.Random(7),
        thrown_faces=(0, 0, 0, 0),
    )

    # PARKED: defend requested, exchange NOT walked, conflict unresolved.
    assert result.awaiting_defense is True
    assert result.exchange is None
    assert encounter.resolved is False

    # Exactly one incoming attack targets the PC → one request + one ledger entry.
    assert len(result.defend_requests) == 1
    req = result.defend_requests[0]
    assert req.defender == "Rux"
    assert req.attacker == "Bandit"
    assert any(
        p.request_id == req.request_id and p.defense_total is None
        for p in encounter.pending_defenses
    )

    # The NPC's 4dF was rolled + sealed at REVEAL (locked, not re-rolled later).
    assert any(c.actor == "Bandit" and c.action == "attack" for c in encounter.fate_commits)


def test_no_pc_targeted_resolves_immediately():
    # PC overcomes a passive obstacle with no live opponent — nothing targets a
    # PC, so there is no DEFEND barrier and the exchange resolves in one call.
    snap, encounter = conflict_with_pc_and_npc(pc="Rux", npc="Bandit", npc_targets_pc=False)
    ruleset = get_ruleset_module("fate")

    result = dispatch_fate_action(
        payload=FateActionPayload(
            request_id="o1", action="overcome", skill="Athletics", difficulty=2
        ),
        actor_name="Rux",
        encounter=encounter,
        ruleset=ruleset,
        snapshot=snap,
        rng=random.Random(7),
        thrown_faces=(1, 1, 0, 0),
    )

    assert result.awaiting_defense is False
    assert result.exchange is not None  # walked now
    assert encounter.pending_defenses == []
