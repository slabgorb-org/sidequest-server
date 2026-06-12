"""RED tests — Story 73-8 (truthfulness bug in the hp_depletion beat-impact stamp).

Under ``win_condition="hp_depletion"`` (SWN combat, ADR-114) ``apply_beat``
SUPPRESSES dial application — the dial metrics are inert 1e6 HP placeholders and
the real move flows through the HP channel (``damage_channel="strike"`` +
``damage_resolver`` → ``apply_beat_hp_channel`` → ``state_patch_hp`` span). But
``describe_beat_impact`` classifies the stamped ``last_beat_impacts`` from the
NOMINAL dial deltas (beat_kinds.py line 549, computed before the suppression at
564-583). So a strike Success stamps ``effect="advance"``/``dial_moved=True`` for
a beat whose dial NEVER moved on-screen — a lie the GM panel can't catch (the
watcher says "dials suppressed", the HP bar shows HP changed, but the impact
claims a dial advance). 73-4 documented this caveat at lines 541-548 and deferred
the fix to this story.

WHAT THESE TESTS PIN — the OBSERVABLE contract, not the mechanism:
  Every assertion is on ``enc.last_beat_impacts[...]`` AFTER a real ``apply_beat``
  under hp_depletion — so EITHER fix (a ``hp_depletion_suppressed`` flag into
  describe_beat_impact, OR recomputing from the HP channel) satisfies them. The
  load-bearing truth is ``dial_moved is False`` (no dial moved → no false motion
  claim). Tags and resolution beats still report (they are not dial motion).

  NOTE on ``effect == "inert"``: that is the honest classification for "nothing
  happened on the dial" within the EXISTING closed ``BeatEffect`` union
  (advance/setback/resolution/tag/backfire/inert). A dedicated ``"suppressed"``
  literal is an acceptable Dev alternative, but it would require extending the
  union on BOTH server and the UI ``BeatEffect`` type — for a surface the UI does
  not render under hp_depletion (it draws HP bars). If Dev goes that route, only
  the two ``effect == "inert"`` assertions below change; the ``dial_moved is
  False`` truth is non-negotiable either way.

Fixture mirrors tests/game/test_apply_beat_hp_depletion.py (the canonical
hp_depletion apply_beat harness). Server hazard: run with ``-n0``.
"""

from __future__ import annotations

from sidequest.game.beat_kinds import BeatKind, ResolvedDeltas, apply_beat, describe_beat_impact
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.protocol.dice import RollOutcome


def _enc(win_condition: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        win_condition=win_condition,
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        actors=[
            EncounterActor(name="Hero", role="player", side="player"),
            EncounterActor(name="Pirate", role="opponent", side="opponent"),
        ],
    )


def _cores(pirate_hp: int, hero_hp: int = 10) -> tuple[object, dict[str, CreatureCore]]:
    cores = {
        "Hero": CreatureCore(
            name="Hero",
            description="a hero",
            personality="brave",
            hp=HpPool(current=hero_hp, max=10, base_max=10),
        ),
        "Pirate": CreatureCore(
            name="Pirate",
            description="a pirate",
            personality="greedy",
            hp=HpPool(current=pirate_hp, max=10, base_max=10),
        ),
    }
    return (lambda name: cores.get(name)), cores


class _StrikeBeat:
    id = "shoot"
    kind = "strike"
    base = 2  # strike Success -> own == base == 2 (a non-zero dial delta to suppress)
    stat_check = "Physique"
    damage_channel = "strike"


class _PushResolutionBeat:
    id = "retreat"
    kind = "push"
    base = 1
    stat_check = "Reflex"
    resolution = True


# ── RED: the lie ─────────────────────────────────────────────────────────────


def test_hp_depletion_strike_success_stamps_no_false_dial_move():
    # THE BUG. A strike Success under hp_depletion suppresses the dial (the move
    # goes to HP), so the stamped impact must NOT claim a dial advance.
    enc = _enc("hp_depletion")
    resolver, cores = _cores(pirate_hp=7)
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=resolver,
        damage_resolver=lambda: 3,
    )
    impact = enc.last_beat_impacts["player"]

    # Load-bearing truth: no dial moved on-screen.
    assert impact["dial_moved"] is False
    # And no false dial-motion category.
    assert impact["effect"] not in ("advance", "setback")
    # Honest classification within the existing union (see module docstring note).
    assert impact["effect"] == "inert"
    # The summary must not assert a dial edge gain that never happened.
    assert "to your edge" not in impact["summary"].lower()

    # Cross-channel consistency (the POINT of the fix): the move landed on HP, not
    # the dial — so the stamp saying "no dial" is consistent with HP actually
    # changing, the exact cross-check the GM panel needs.
    assert cores["Pirate"].hp.current == 4  # 7 - 3 damage, no mitigation


def test_hp_depletion_strike_critsuccess_reports_tag_not_advance():
    # A strike CritSuccess grants the fleeting "Opening" tag AND nominally moves
    # the dial. Under hp_depletion the dial is suppressed but the TAG still fires
    # (apply_beat grants it regardless). The truthful readout is the tag, not a
    # dial advance.
    enc = _enc("hp_depletion")
    resolver, _ = _cores(pirate_hp=7)
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.CritSuccess,
        turn=1,
        edge_resolver=resolver,
        damage_resolver=lambda: 3,
    )
    impact = enc.last_beat_impacts["player"]

    assert impact["dial_moved"] is False
    assert impact["effect"] == "tag"  # tags survive suppression; dial motion does not
    assert impact["tag"] == "Opening"


# ── GREEN guards: behavior that is already truthful and must STAY truthful ────


def test_hp_depletion_strike_fail_is_inert():
    # No-op case: a Fail strike yields no deltas, so it is honest today. Guard
    # against a fix that accidentally changes the no-op classification.
    enc = _enc("hp_depletion")
    resolver, _ = _cores(pirate_hp=7)
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Fail,
        turn=1,
        edge_resolver=resolver,
        damage_resolver=lambda: 0,
    )
    impact = enc.last_beat_impacts["player"]
    assert impact["dial_moved"] is False
    assert impact["effect"] == "inert"


def test_hp_depletion_suppressed_summary_does_not_claim_total_inertness():
    # Follow-up truthfulness gap: under suppression the move DID land (on HP, e.g.
    # Pirate 7->4), so the inert summary must not reuse the generic "moved nothing"
    # line — that would be a small lie on a fix-the-lie story.
    enc = _enc("hp_depletion")
    resolver, _ = _cores(pirate_hp=7)
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=resolver,
        damage_resolver=lambda: 3,
    )
    summary = enc.last_beat_impacts["player"]["summary"].lower()
    assert "moved nothing" not in summary
    assert "no change" not in summary
    assert any(k in summary for k in ("hp", "held", "suppress"))


def test_hp_depletion_resolution_beat_still_resolves():
    # AC 1.c: resolution beats are NEVER suppressed — a push resolution under
    # hp_depletion must still read as "resolution", not get flattened to inert.
    enc = _enc("hp_depletion")
    resolver, _ = _cores(pirate_hp=7)
    apply_beat(
        enc,
        enc.actors[0],
        _PushResolutionBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=resolver,
        damage_resolver=None,
    )
    impact = enc.last_beat_impacts["player"]
    assert impact["effect"] == "resolution"
    assert impact["dial_moved"] is False


# ── Regression: non-hp_depletion path is UNCHANGED ───────────────────────────


def test_dial_threshold_strike_success_still_advances():
    # The same strike on a dial pack MUST still advance the dial and stamp
    # effect="advance"/dial_moved=True — the fix is gated on hp_depletion only.
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 99  # avoid resolving so we can read the dial
    resolver, _ = _cores(pirate_hp=7)
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=resolver,
        damage_resolver=lambda: 0,
    )
    impact = enc.last_beat_impacts["player"]
    assert impact["effect"] == "advance"
    assert impact["dial_moved"] is True
    assert enc.player_metric.current == 2  # dial really moved on a dial pack


def test_describe_beat_impact_pure_dial_path_unchanged():
    # Pure-function regression on the CURRENT signature (no new param presupposed):
    # for an ordinary dial strike the classification is untouched. Guards Dev from
    # breaking the non-suppressed lane while adding the hp_depletion branch.
    impact = describe_beat_impact(
        ResolvedDeltas(own=2), kind=BeatKind.strike, outcome=RollOutcome.Success
    )
    assert impact.effect == "advance"
    assert impact.dial_moved is True
    assert impact.own == 2
