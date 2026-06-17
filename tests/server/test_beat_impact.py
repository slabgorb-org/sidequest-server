"""RED tests — Story 73-4: legible beat-kind impact descriptor.

The dial math is CORRECT (a ``push`` CritSuccess intentionally moves no dial —
``{own:0, opponent:0, resolution:True, grants_fleeting_tag:"Clean Exit"}``). The
bug is that the engine never *says* so, so a mechanics-first player (Sebastien /
Jade) reads "best roll → dial +0" as a broken roll.

This suite pins a single source of truth in ``beat_kinds.py``:

  ``describe_beat_impact(deltas, *, kind, outcome) -> BeatImpact``

a pure classifier over the *resolved* deltas (so per-tier overrides are honored,
per context-story-73-4 §"Mixed-effect beat"), plus the wiring that ``apply_beat``
exposes the impact on its ``ApplyResult`` AND stamps it onto the encounter
(``enc.last_beat_impacts[side]``) so the player-facing payload builder can surface
it without recomputing.

Per CLAUDE.md "Crunch in the genre … legible in player-facing surfaces" and the
audience boundary in context-story-73-4: this is a *player-UI* descriptor, NOT an
extension of the dev-side ``beat_no_op`` / ``beat_applied`` watcher emits.
"""

from __future__ import annotations

from sidequest.game.beat_kinds import (
    BeatImpact,
    BeatKind,
    apply_beat,
    describe_beat_impact,
    resolve_tier_deltas,
)
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.dice import RollOutcome

# ───────────────────────── fixtures ─────────────────────────


def _enc(*, p_thresh: int = 10, o_thresh: int = 10, p_cur: int = 0, o_cur: int = 0):
    return StructuredEncounter(
        encounter_type="social_duel",
        player_metric=EncounterMetric(name="barbs", current=p_cur, starting=0, threshold=p_thresh),
        opponent_metric=EncounterMetric(
            name="barbs", current=o_cur, starting=0, threshold=o_thresh
        ),
        actors=[
            EncounterActor(name="Pryce", role="duelist", side="player"),
            EncounterActor(name="Hamish", role="duelist", side="opponent"),
            EncounterActor(name="Host", role="bystander", side="neutral"),
        ],
    )


def _push_beat(beat_id: str = "concede") -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": beat_id,
            "label": "Concede Gracefully",
            "kind": "push",
            "base": 1,
            "stat_check": "Humour",
        }
    )


def _strike_beat(beat_id: str = "barb", base: int = 2) -> BeatDef:
    return BeatDef.model_validate(
        {"id": beat_id, "label": "Sharp Barb", "kind": "strike", "base": base, "stat_check": "Wit"}
    )


def _angle_beat(beat_id: str = "set_up", target_tag: str = "Off-Balance") -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": beat_id,
            "label": "Set Up",
            "kind": "angle",
            "target_tag": target_tag,
            "stat_check": "Cunning",
        }
    )


def _deltas(kind: BeatKind, outcome: RollOutcome, *, base: int = 1, target_tag: str | None = None):
    return resolve_tier_deltas(
        kind=kind, base=base, outcome=outcome, overrides=None, target_tag=target_tag
    )


# ═══════════════ describe_beat_impact — semantic classifier ═══════════════
#
# AC1/AC2/AC4 + context "Distinguish the three zero-ish cases".


def test_impact_is_a_beat_impact_dataclass():
    impact = describe_beat_impact(
        _deltas(BeatKind.push, RollOutcome.CritSuccess),
        kind=BeatKind.push,
        outcome=RollOutcome.CritSuccess,
    )
    assert isinstance(impact, BeatImpact)


# ── AC1: push CritSuccess — no dial by design, reads as a GOOD clean exit ──


def test_push_critsuccess_is_resolution_not_inert():
    # The reported bug: own=0/opponent=0 but resolution=True + "Clean Exit".
    impact = describe_beat_impact(
        _deltas(BeatKind.push, RollOutcome.CritSuccess),
        kind=BeatKind.push,
        outcome=RollOutcome.CritSuccess,
    )
    assert impact.effect == "resolution"
    assert impact.dial_moved is False
    assert impact.resolution is True
    assert impact.tag == "Clean Exit"
    # (73-9) Replaced a redundant bare-truthy `assert impact.summary` with explicit
    # field asserts: pin the no-dial numerics that ride a by-design resolution.
    # The summary's non-emptiness is already proven by the substring checks below.
    assert impact.own == 0
    assert impact.opponent == 0
    # Reads as intended, not broken: explains the no-move, never as failure.
    assert "resolv" in impact.summary.lower()
    assert ("no dial" in impact.summary.lower()) or ("by design" in impact.summary.lower())
    assert "broken" not in impact.summary.lower()
    assert "fail" not in impact.summary.lower()


def test_push_success_also_resolution():
    impact = describe_beat_impact(
        _deltas(BeatKind.push, RollOutcome.Success), kind=BeatKind.push, outcome=RollOutcome.Success
    )
    assert impact.effect == "resolution"
    assert impact.dial_moved is False
    assert impact.resolution is True


# ── AC2: angle tag-grant tiers — tag IS the legible impact, not a bare 0 ──


def test_angle_success_is_tag_grant_naming_the_tag():
    impact = describe_beat_impact(
        _deltas(BeatKind.angle, RollOutcome.Success, target_tag="Off-Balance"),
        kind=BeatKind.angle,
        outcome=RollOutcome.Success,
    )
    assert impact.effect == "tag"
    assert impact.dial_moved is False
    assert impact.tag == "Off-Balance"
    assert "Off-Balance" in impact.summary


def test_angle_tie_is_tag_grant_fleeting():
    impact = describe_beat_impact(
        _deltas(BeatKind.angle, RollOutcome.Tie, target_tag="Off-Balance"),
        kind=BeatKind.angle,
        outcome=RollOutcome.Tie,
    )
    assert impact.effect == "tag"
    assert impact.dial_moved is False
    assert impact.tag == "Off-Balance"


def test_angle_critfail_is_backfire_distinct_from_tag():
    impact = describe_beat_impact(
        _deltas(BeatKind.angle, RollOutcome.CritFail, target_tag="Off-Balance"),
        kind=BeatKind.angle,
        outcome=RollOutcome.CritFail,
    )
    assert impact.effect == "backfire"


def test_angle_critfail_backfire_full_field_set():
    # 73-9 characterization: 73-4 only pinned effect=="backfire". Pin the FULL
    # descriptor so a refactor can't silently change the player-facing readout.
    # NOTE (real behavior, vs the AC's loose wording): the `tag` field is the
    # angle's OWN target tag ("Off-Balance"), not the literal string "backfire" —
    # the backfire is the `effect`, the tag is what rebounded. A backfire grants
    # no dial motion.
    impact = describe_beat_impact(
        _deltas(BeatKind.angle, RollOutcome.CritFail, target_tag="Off-Balance"),
        kind=BeatKind.angle,
        outcome=RollOutcome.CritFail,
    )
    assert impact.effect == "backfire"
    assert impact.dial_moved is False
    assert impact.own == 0
    assert impact.opponent == 0
    assert impact.resolution is False
    assert impact.tag == "Off-Balance"
    assert "backfire" in impact.summary.lower()
    assert "Off-Balance" in impact.summary


def test_brace_critfail_is_opponent_setback():
    # 73-9 characterization: the opponent>0 branch of describe_beat_impact. A brace
    # CritFail nudges the OPPONENT's dial UP (+1) — a real setback for the actor,
    # and (unlike a backfire) the dial DID move. Pins the "their edge rises" summary.
    deltas = _deltas(BeatKind.brace, RollOutcome.CritFail, base=2)
    assert deltas.opponent == 1  # guard the fixture: this tier really yields opponent+1
    impact = describe_beat_impact(deltas, kind=BeatKind.brace, outcome=RollOutcome.CritFail)
    assert impact.effect == "setback"
    assert impact.dial_moved is True
    assert impact.own == 0
    assert impact.opponent == 1
    assert impact.tag is None
    assert "their edge rises" in impact.summary.lower()


# ── AC4: dial-moving outcomes still read as a dial move; tag not swallowed ──


def test_strike_success_is_dial_advance():
    impact = describe_beat_impact(
        _deltas(BeatKind.strike, RollOutcome.Success, base=2),
        kind=BeatKind.strike,
        outcome=RollOutcome.Success,
    )
    assert impact.effect == "advance"
    assert impact.dial_moved is True
    assert impact.own == 2


def test_strike_critsuccess_advances_and_keeps_opening_tag():
    # AC4 edge: a CritSuccess that DOES move a dial must not be swallowed by the
    # no-op explanation — the dial advance AND the "Opening" tag both surface.
    impact = describe_beat_impact(
        _deltas(BeatKind.strike, RollOutcome.CritSuccess, base=2),
        kind=BeatKind.strike,
        outcome=RollOutcome.CritSuccess,
    )
    assert impact.effect == "advance"
    assert impact.dial_moved is True
    assert impact.own == 2
    assert impact.tag == "Opening"


def test_brace_success_drain_is_a_favorable_dial_move():
    impact = describe_beat_impact(
        _deltas(BeatKind.brace, RollOutcome.Success, base=2),
        kind=BeatKind.brace,
        outcome=RollOutcome.Success,
    )
    assert impact.effect == "advance"  # draining the opponent dial is favorable
    assert impact.dial_moved is True
    assert impact.opponent == -2


# ── Edge: the three zero-ish cases must NOT read identically ──


def test_three_zeroish_cases_are_distinguishable():
    resolution = describe_beat_impact(
        _deltas(BeatKind.push, RollOutcome.CritSuccess),
        kind=BeatKind.push,
        outcome=RollOutcome.CritSuccess,
    )
    inert = describe_beat_impact(
        _deltas(BeatKind.push, RollOutcome.Fail), kind=BeatKind.push, outcome=RollOutcome.Fail
    )
    setback = describe_beat_impact(
        _deltas(BeatKind.push, RollOutcome.CritFail),
        kind=BeatKind.push,
        outcome=RollOutcome.CritFail,
    )
    # All three move the *player's* dial by 0/0/-1 — but they are NOT the same.
    assert resolution.effect == "resolution"
    assert inert.effect == "inert"
    assert setback.effect == "setback"
    assert len({resolution.effect, inert.effect, setback.effect}) == 3
    # "by design" (resolution) vs "nothing happened" (inert): both dial_moved False
    # but the categories differ so the UI can render them differently.
    assert resolution.dial_moved is False
    assert inert.dial_moved is False
    # A negative move is a real setback, not a "no change".
    assert setback.dial_moved is True
    assert setback.own == -1


def test_fail_tier_is_inert_with_nonempty_summary():
    impact = describe_beat_impact(
        _deltas(BeatKind.strike, RollOutcome.Fail, base=3),
        kind=BeatKind.strike,
        outcome=RollOutcome.Fail,
    )
    assert impact.effect == "inert"
    assert impact.dial_moved is False
    # The inert summary must actually convey "nothing changed" — not just be
    # non-empty (a wrong-but-non-empty string would pass a bare truthy check).
    assert "no change" in impact.summary.lower()


# ── Edge: classifier reads RESOLVED deltas, not the kind's nominal default ──


def test_override_that_adds_a_dial_move_is_described_as_moved():
    # context-story-73-4 §"Mixed-effect beat": a per-tier override that adds a dial
    # move to a normally-no-move tier must read as a move, not "no dial by design".
    overridden = resolve_tier_deltas(
        kind=BeatKind.push,
        base=1,
        outcome=RollOutcome.CritSuccess,
        overrides={RollOutcome.CritSuccess: {"own": 3, "resolution": False}},
        target_tag=None,
    )
    impact = describe_beat_impact(overridden, kind=BeatKind.push, outcome=RollOutcome.CritSuccess)
    assert impact.dial_moved is True
    assert impact.own == 3
    assert impact.effect == "advance"


# ═══════════════ apply_beat wiring — impact on result + encounter ═══════════════
#
# Per CLAUDE.md "Every Test Suite Needs a Wiring Test": the descriptor must reach
# the production output of apply_beat (which BOTH narration paths call) and be
# stamped onto the encounter so the payload builder can surface it.


def test_apply_beat_exposes_impact_on_result():
    enc = _enc()
    pryce = enc.find_actor("Pryce")
    result = apply_beat(enc, pryce, _push_beat(), RollOutcome.CritSuccess)
    assert result.impact is not None
    assert isinstance(result.impact, BeatImpact)
    assert result.impact.effect == "resolution"
    assert result.impact.dial_moved is False


def test_apply_beat_stamps_player_side_impact_on_encounter():
    enc = _enc()
    pryce = enc.find_actor("Pryce")
    apply_beat(enc, pryce, _push_beat(), RollOutcome.CritSuccess)
    # Per-side so an opposed_check opponent beat can't clobber the player's
    # readout (the exact repro: player crit clean-exit vs opponent riposte).
    stamped = enc.last_beat_impacts["player"]
    assert stamped["effect"] == "resolution"
    assert stamped["dial_moved"] is False
    assert stamped["summary"]
    assert stamped["tag"] == "Clean Exit"


def test_apply_beat_records_each_side_separately():
    enc = _enc()
    pryce = enc.find_actor("Pryce")
    hamish = enc.find_actor("Hamish")
    # Player sets up a tag (no resolution → encounter stays live so the
    # opponent beat also applies and records its own side).
    apply_beat(enc, pryce, _angle_beat(), RollOutcome.Success)
    apply_beat(enc, hamish, _strike_beat(base=2), RollOutcome.Success)
    assert enc.last_beat_impacts["player"]["effect"] == "tag"
    assert enc.last_beat_impacts["opponent"]["effect"] == "advance"


def test_skipped_beat_records_no_impact():
    enc = _enc()
    host = enc.find_actor("Host")  # neutral → skipped
    result = apply_beat(enc, host, _strike_beat(), RollOutcome.Success)
    assert result.skipped_reason == "neutral_actor"
    assert result.impact is None
    assert "player" not in enc.last_beat_impacts
    assert "neutral" not in enc.last_beat_impacts


def test_same_side_beat_overwrites_prior_impact():
    # 73-9 characterization of the same-side overwrite invariant: last_beat_impacts
    # holds ONE entry per side — a second player beat REPLACES the first (it is the
    # latest readout for that side), it is NOT appended or merged into a history.
    enc = _enc()
    pryce = enc.find_actor("Pryce")
    # First player beat: a dial advance.
    apply_beat(enc, pryce, _strike_beat(base=2), RollOutcome.Success)
    assert enc.last_beat_impacts["player"]["effect"] == "advance"
    # Second player beat (encounter stays live — angle doesn't resolve): a tag.
    apply_beat(enc, pryce, _angle_beat(), RollOutcome.Success)
    stamped = enc.last_beat_impacts["player"]
    assert isinstance(stamped, dict)  # a single impact dict, NOT a list/append
    assert stamped["effect"] == "tag"  # the SECOND beat's impact, not the first
    assert stamped["tag"] == "Off-Balance"
    assert list(enc.last_beat_impacts.keys()) == ["player"]  # no duplicate slot
