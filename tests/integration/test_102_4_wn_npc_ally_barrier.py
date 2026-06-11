"""Story 102-4 regression — a player-side NPC ally must not deadlock the WN
commit barrier (coyote_star solo ship_combat, 2026-06-10 playtest).

The WN sealed-round barrier waits for every *human-controlled* player seat to
commit a Main Action, then walks the round in initiative order. Story 59-35
seats scene-present FRIENDLY NPCs on the player side (SOUL Guitar-Solo / ADR-116
friendly half) — but those allies carry no SWN ability scores, get NO initiative
slot, and act on narrator beats. They never seal a Main Action.

The deadlock: ``wn_waiting_actors`` counted EVERY ``side="player"`` actor,
including the engine-driven NPC ally. In solo play the human commits the one PC,
the ally never commits, so the barrier never closes — ``commitment_pending``
sticks True, ``structured_phase`` freezes at Setup, and space combat is
unplayable (the live coyote_star snapshot: ``initiative: [{dark contact},
{Chico}]`` with ``wn_commits: [{Chico}]`` and the Wainu crew ally dangling).

Fix: only PCs (names in ``snapshot.characters``) hold the barrier; player-side
NPC allies (in ``snapshot.npcs``) are exempt — recorded on the round-committed
span (``exempt_allies``) so the GM panel can see why the barrier closed without
every player-side actor committing.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests.integration._wn_round_102_4 import (
    GENRE_PACKS_DIR,
    dispatch_throw,
    force_initiative,
    load_pack,
    seat_npc_ally,
    seat_wn_combat,
    spans_named,
)

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

_OPP = "Hired Blade"
_PC = "Vesska"
_ALLY = "Wainu Moana-Teru"  # friendly NPC crew, seated player-side, no init slot

_SPAN_COMMITTED = "wwn.round.committed"
_SPAN_RESOLVED = "wwn.round.resolved"


def test_solo_pc_with_npc_ally_closes_the_barrier_and_fires_the_round(
    otel_capture, monkeypatch
):
    """The load-bearing deadlock regression: in solo play, a seated friendly
    NPC ally must NOT hold the WN commit barrier. The human commits the one PC
    (the last *human* commit), so the barrier closes and the round walks."""
    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: nobody drops
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP])
    seat_npc_ally(snap, enc, _ALLY)
    # 59-35: the ally carries no DEX and is NOT in initiative — PC + opp only,
    # exactly the live coyote_star ship_combat snapshot shape.
    force_initiative(enc, [(_OPP, 9), (_PC, 3)])

    outcome = dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1")

    assert outcome.commitment_pending is False, (
        "a seated NPC ally must not dangle the barrier — the solo PC's commit "
        "is the last human commit, so the round must fire (commitment_pending "
        "stuck True is the coyote_star deadlock)"
    )
    assert spans_named(otel_capture, _SPAN_RESOLVED), (
        "the barrier closed, so the round walk must reach resolution; an absent "
        "wwn.round.resolved span is the frozen-at-Setup deadlock"
    )


def test_wn_waiting_actors_excludes_the_npc_ally(monkeypatch):
    """Direct predicate pin: with the PC uncommitted the barrier waits on the
    PC ONLY — never on the engine-driven NPC ally."""
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP])
    seat_npc_ally(snap, enc, _ALLY)

    from sidequest.server.dispatch.wn_round import wn_waiting_actors

    waiting = wn_waiting_actors(encounter=enc, snapshot=snap)
    assert waiting == [_PC], (
        "the commit barrier waits only on human-controlled PCs; the friendly "
        f"NPC ally must be exempt. got {waiting!r}"
    )


def test_round_committed_span_surfaces_the_exempt_ally(otel_capture, monkeypatch):
    """OTEL observability (GM-panel lie-detector): when the barrier closes with
    a player-side ally that never committed, the round-committed span records
    the exempt ally so a GM can see WHY the barrier closed short of every
    player-side actor."""
    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: nobody drops
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP])
    seat_npc_ally(snap, enc, _ALLY)
    force_initiative(enc, [(_OPP, 9), (_PC, 3)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1")

    committed = spans_named(otel_capture, _SPAN_COMMITTED)
    assert committed, "the barrier closed — a wwn.round.committed span must fire"
    exempt = committed[0].attributes.get("exempt_allies")
    assert exempt == _ALLY, (
        "the round-committed span must name the engine-driven NPC ally exempt "
        f"from the barrier (the GM-panel explanation for the short barrier); got {exempt!r}"
    )
