"""Story 102-4 AC1 + AC2 — sealed-letter commitment and initiative-ordered resolution.

The WN turn model (SWN module design §6-7, P4 follow-on): in a WN
(`SwnRulesetModule`-family) confrontation, a committed Main Action does NOT
resolve at submission. It seals. When the LAST seated player-side participant
commits, the engine resolves the whole round actor-by-actor in the persisted
1d8+DEX initiative order — including the opponent, which acts at its OWN
initiative slot instead of as an immediate reprisal rider on the player's
dispatch.

Contract pinned here (the RED-phase API for Dev):

- ``DiceThrowOutcome.commitment_pending: bool`` — True when the throw was
  sealed (peers uncommitted), False when this commit fired the round. Mirrors
  the existing ``opposed_pending`` defer-the-beat idiom on the same dataclass.
- Round-phase OTEL spans, per the epic's ``{ruleset}.{surface}`` invariant:
  ``wwn.round.committed`` (barrier closed — all seated player actions in),
  ``wwn.round.initiative`` (the persisted order this round resolves in),
  ``wwn.round.resolved`` (round walk done) carrying a ``resolution_order``
  attribute — the comma-joined token_id sequence actually walked.
- ``build_confrontation_payload()["committed_actors"]`` — the player-side
  actor names whose Main Action is sealed this round (the UI
  committed-vs-waiting seam; sidequest-ui mirrors it on ConfrontationData).

Keith directive (2026-06-10, story context): SideQuest turn semantics are
kept — this rides the ADR-036 submit-and-wait barrier, never replaces it.
Sealed *resolution*, not hidden submission: nothing here pins away peer
action-text visibility.

Initiative is forced post-instantiation for determinism — rolling it is the
P4 spine's proven job; this suite owns what the round DOES with the order.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests.integration._wn_round_102_4 import (
    GENRE_PACKS_DIR,
    arm_pc,
    dispatch_throw,
    force_initiative,
    load_pack,
    seat_wn_combat,
    span_start_order,
    spans_named,
)

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

_OPP = "Hired Blade"
_PC_A = "Vesska"
_PC_B = "Brakka"

_SPAN_COMMITTED = "wwn.round.committed"
_SPAN_INITIATIVE = "wwn.round.initiative"
_SPAN_RESOLVED = "wwn.round.resolved"
_SPAN_BEAT_APPLIED = "encounter.beat_applied"
_SPAN_OPP_ATTACK = "encounter.opponent_attack_resolved"

# BLOCKED on epic-152 (opponent-attack synthesis). Under de-nativized WWN combat
# the opponent's attack is SKIPPED: ``_resolve_opponent_reprisal`` (dice.py:2069)
# requires an authored strike beat in ``cdef.beats``, which 108-3 stripped empty;
# 108-8 synthesized only the PLAYER's ``attack``, never the opponent's, so
# ``wn_round`` logs ``opponent_reprisal_skipped reason=no_strike_beat`` and the
# Other never swings (a live combat outage). Fixing it needs production engine
# code (synthesize the opponent strike beat, parallel to 108-8) — out of scope
# for 125-8 (test-debt only, AC3). Loud-skip + linked story per AC1; see the
# session Delivery Findings. Unskip when epic-152 lands the opponent-attack synthesis.
_OPPONENT_ATTACK_BLOCKED = (
    "epic-152: WN opponent attack skipped under de-nativized WWN combat "
    "(no_strike_beat — opponent strike beat never synthesized; 108-8 did only the "
    "player). Production gap; 125-8 is test-debt only (AC3). See Delivery Findings."
)


@pytest.fixture
def two_pc_combat():
    """Real heavy_metal Blade-work combat: two PCs vs one 10-HP blade.

    Both PCs are armed (arm_pc → 2d6) so a committed WN ``attack`` resolves real
    weapon dice: heavy_metal ships no unarmed_damage floor, and 108-3 removed the
    native committed_blow damage_override the kill choreography once rode (125-8)."""
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC_A, _PC_B], [_OPP])
    arm_pc(snap, _PC_A)
    arm_pc(snap, _PC_B)
    return pack, snap, enc


# ─── AC1: sealed commitment ──────────────────────────────────────────────────


def test_first_commit_is_sealed_not_resolved(two_pc_combat, otel_capture, monkeypatch):
    """PC A commits while PC B is uncommitted: NOTHING mechanical resolves.

    Today dispatch_dice_throw applies the beat + opponent reprisal
    immediately — this is the load-bearing behavioral flip of the story.
    """
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])
    opp_hp_before = snap.find_creature_core(_OPP).hp.current

    outcome = dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")

    assert outcome.commitment_pending is True, (
        "with a seated peer uncommitted, the throw must seal (commit, not "
        "resolve) — mirroring the opposed_pending defer idiom"
    )
    assert snap.find_creature_core(_OPP).hp.current == opp_hp_before, (
        "a sealed commit must not ablate the opponent — resolution belongs "
        "to the round walk after the barrier closes"
    )
    assert not spans_named(otel_capture, _SPAN_RESOLVED), (
        "no wwn.round.resolved may fire while a seated participant is "
        "uncommitted (no resolution output leaks before the barrier)"
    )


def test_first_commit_runs_no_opponent_reprisal(two_pc_combat, otel_capture, monkeypatch):
    """The opponent acts at its initiative slot, not as a reprisal rider on
    the first commit: sealed commit -> both PCs' HP untouched.

    rng pinned MIN (not max): a max pin lets A's 2d6=12 kill the 10-HP blade
    in today's immediate-resolution flow, which skips reprisal and turns this
    test vacuously green. Min pin = A misses, today's reprisal still records
    its opponent_attack_resolved span (it carries miss math too) — so the
    span assertion is the genuine RED driver."""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_OPP, 9), (_PC_A, 7), (_PC_B, 2)])
    pc_a_hp = snap.characters[0].core.hp.current
    pc_b_hp = snap.characters[1].core.hp.current

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")

    assert snap.characters[0].core.hp.current == pc_a_hp
    assert snap.characters[1].core.hp.current == pc_b_hp, (
        "no opponent attack may resolve while the round is uncommitted — even "
        "with the opponent first in the initiative order"
    )
    assert not spans_named(otel_capture, _SPAN_OPP_ATTACK), (
        "opponent_attack_resolved before the barrier closes is exactly the "
        "reprisal-rider behavior the WN turn model retires"
    )


def test_committed_actors_surface_on_confrontation_payload(two_pc_combat, monkeypatch):
    """After A commits (B waiting), the CONFRONTATION payload names the
    committed actor — the UI committed-vs-waiting seam (cross-repo AC)."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])
    cdef = next(c for c in pack.rules.confrontations if c.win_condition == "hp_depletion")

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")

    from sidequest.server.dispatch.confrontation import build_confrontation_payload

    payload = build_confrontation_payload(encounter=enc, cdef=cdef, genre_slug="heavy_metal")
    assert payload.get("committed_actors") == [_PC_A], (
        "the payload must surface exactly the sealed player-side actors so "
        "the overlay can render committed-vs-waiting (Alex sees who the "
        "table is waiting on without anyone being rushed); got "
        f"{payload.get('committed_actors')!r}"
    )


# ─── AC1→AC2: the last commit fires the ordered round ────────────────────────


def test_last_commit_fires_round_phase_spans_in_order(two_pc_combat, otel_capture, monkeypatch):
    """B's commit closes the barrier: committed -> initiative -> resolved
    spans fire, in that phase order (the GM-panel polygraph for the round)."""
    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: misses, nobody drops
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")
    outcome_b = dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_B, player_id="p2")

    assert outcome_b.commitment_pending is False, (
        "the barrier-closing commit resolves the round in the same dispatch"
    )
    for name in (_SPAN_COMMITTED, _SPAN_INITIATIVE, _SPAN_RESOLVED):
        assert spans_named(otel_capture, name), f"missing round-phase span {name}"
    order = span_start_order(otel_capture, _SPAN_COMMITTED, _SPAN_INITIATIVE, _SPAN_RESOLVED)
    assert order == [_SPAN_COMMITTED, _SPAN_INITIATIVE, _SPAN_RESOLVED], (
        f"round phases must run committed -> initiative -> resolved; got {order}"
    )


def test_resolved_span_carries_the_walked_order(two_pc_combat, otel_capture, monkeypatch):
    """AC2: the resolution_order attribute is the engine-walked sequence —
    descending persisted initiative, opponent in its slot."""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_PC_B, 8), (_OPP, 5), (_PC_A, 3)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")
    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_B, player_id="p2")

    resolved = spans_named(otel_capture, _SPAN_RESOLVED)
    assert resolved, "round must emit wwn.round.resolved"
    assert resolved[0].attributes.get("resolution_order") == f"{_PC_B}, {_OPP}, {_PC_A}", (
        "resolution_order must be the comma-joined token_id sequence the "
        "engine actually walked (descending persisted initiative) — the "
        "lie-detector for 'whose shot lands first'; got "
        f"{resolved[0].attributes.get('resolution_order')!r}"
    )


@pytest.mark.skip(reason=_OPPONENT_ATTACK_BLOCKED)
def test_opponent_with_higher_initiative_acts_before_the_player_strike(
    two_pc_combat, otel_capture, monkeypatch
):
    """AC2 behavioral order proof: forced order [opponent, A, B] means the
    opponent's attack resolves BEFORE A's strike applies. Today the engine
    does the exact reverse (strike, then reprisal) — this is the d8 mattering.

    SKIPPED (125-8): needs the opponent attack to fire, which is the no_strike_beat
    production gap owned by epic-152 (see _OPPONENT_ATTACK_BLOCKED)."""
    monkeypatch.setattr("random.randint", lambda a, b: b)  # max: everyone hits hard
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_OPP, 9), (_PC_A, 5), (_PC_B, 3)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")
    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_B, player_id="p2")

    order = span_start_order(otel_capture, _SPAN_OPP_ATTACK, _SPAN_BEAT_APPLIED)
    assert order and order[0] == _SPAN_OPP_ATTACK, (
        "with the opponent first in initiative, opponent_attack_resolved must "
        f"START before any player beat applies; span start order: {order}"
    )


def test_solo_pc_commit_fires_the_round_immediately(otel_capture, monkeypatch):
    """One seated PC = the barrier closes on the first commit (solo play is
    a 1-participant table, not a special case). Round spans still fire and
    the resolution is still order-walked."""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC_A], [_OPP])
    arm_pc(snap, _PC_A)
    force_initiative(enc, [(_OPP, 9), (_PC_A, 3)])

    outcome = dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")

    assert outcome.commitment_pending is False, (
        "a solo commit is the last commit — it must resolve, not dangle"
    )
    assert spans_named(otel_capture, _SPAN_RESOLVED), (
        "solo rounds emit the same round-phase polygraph as MP rounds"
    )


# ─── Review rework round 1 (Reviewer findings, 2026-06-10) ───────────────────


def test_round_fire_clears_the_commit_ledger_and_payload_key(
    two_pc_combat, otel_capture, monkeypatch
):
    """[HIGH] regression net: firing the round must RESET the ledger —
    ``enc.wn_commits`` empties and ``committed_actors`` leaves the
    CONFRONTATION payload. A stale ledger silently reverts AC1 for every
    round after the first (the next first-commit would close the barrier
    alone), and before this test nothing pinned the reset."""
    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: nobody drops
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])
    cdef = next(c for c in pack.rules.confrontations if c.win_condition == "hp_depletion")

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")
    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_B, player_id="p2")

    assert enc.wn_commits == [], (
        "the round walk must consume and clear the sealed-commit ledger; "
        f"stale commits poison the next round's barrier: {enc.wn_commits!r}"
    )
    from sidequest.server.dispatch.confrontation import build_confrontation_payload

    payload = build_confrontation_payload(encounter=enc, cdef=cdef, genre_slug="heavy_metal")
    assert "committed_actors" not in payload, (
        "between rounds the payload must drop the committed_actors key (the "
        "UI legacy/no-indicator state) — a lingering previous-round list "
        f"would lie to the table; got {payload.get('committed_actors')!r}"
    )


def test_second_round_first_commit_seals_again(two_pc_combat, otel_capture, monkeypatch):
    """[HIGH] the barrier RE-ARMS each round: after a full round resolves
    (nobody drops), the next first commit must seal — not fire — and only
    round 1's resolved span exists. This is the round-2 proof the review
    found missing: every prior test ended after one round."""
    monkeypatch.setattr("random.randint", lambda a, b: a)  # min: misses, nobody drops
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")
    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_B, player_id="p2")
    assert not enc.resolved, "fixture precondition: a min-roll round resolves nothing"

    outcome_r2 = dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC_A,
        player_id="p1",
        request_id="req-102-4-round2-a",
    )

    assert outcome_r2.commitment_pending is True, (
        "round 2's first commit must SEAL — a False here means the round-1 "
        "ledger leaked and the barrier closed on one commit (AC1 silently "
        "reverted for every round after the first)"
    )
    assert len(spans_named(otel_capture, _SPAN_RESOLVED)) == 1, (
        "only round 1 may have resolved; a second wwn.round.resolved after a "
        "single round-2 commit is the stale-ledger failure mode"
    )


def test_double_commit_in_one_round_is_a_loud_typed_rejection(
    two_pc_combat, otel_capture, monkeypatch
):
    """[MEDIUM] one Main Action per round: the same actor committing twice
    while the barrier is open is a loud, typed DiceDispatchError and the
    ledger keeps exactly one entry (no partial write, no silent overwrite).

    Green-by-design characterization lock: the guard exists
    (wn_round.seal_wn_commit) but no test exercised it — a fast-click or
    network-retry client is a realistic production path."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = two_pc_combat
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")

    from sidequest.server.dispatch.dice import DiceDispatchError

    with pytest.raises(DiceDispatchError, match="already committed"):
        dispatch_throw(
            pack=pack,
            snap=snap,
            enc=enc,
            character_name=_PC_A,
            player_id="p1",
            request_id="req-102-4-double-a",
        )
    assert [c.actor for c in enc.wn_commits] == [_PC_A], (
        "the rejected double commit must leave exactly the first seal on the "
        f"ledger; got {[c.actor for c in enc.wn_commits]!r}"
    )


def test_round_walk_resolution_close_carries_wn_round_source(otel_capture, monkeypatch):
    """[MEDIUM] OTEL honesty: a resolution closed INSIDE the round walk must
    stamp ``source="wn_round"`` on the ``encounter.resolved`` span (and the
    persisted watcher row it feeds) — the GM panel and ADR-124 forensics must
    not attribute a walk-closed fight to the legacy in-dispatch path.

    RED driver: today the shared close hardcodes ``source="dice_throw_beat"``
    on both call paths."""
    monkeypatch.setattr("random.randint", lambda a, b: b)  # max: 2d6=12 kills the 10-HP blade
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC_A], [_OPP])
    arm_pc(snap, _PC_A)  # 2d6 weapon → pinned-max strike deals 12, kills the 10-HP blade
    force_initiative(enc, [(_PC_A, 9), (_OPP, 2)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")

    resolved = spans_named(otel_capture, "encounter.resolved")
    assert resolved, "the killing solo round must emit encounter.resolved"
    sources = {s.attributes.get("source") for s in resolved}
    assert "wn_round" in sources, (
        "a round-walk resolution close must carry source='wn_round' so the "
        "lie-detector's own label doesn't lie about which seam closed the "
        f"fight; got sources {sources!r}"
    )
