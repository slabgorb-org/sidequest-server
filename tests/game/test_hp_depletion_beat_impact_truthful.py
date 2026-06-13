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
            name="Hero", description="a hero", personality="brave",
            hp=HpPool(current=hero_hp, max=10, base_max=10),
        ),
        "Pirate": CreatureCore(
            name="Pirate", description="a pirate", personality="greedy",
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


def test_hp_depletion_strike_success_reads_the_hp_channel():
    # sq-playtest 2026-06-13 (impact-chip-gap) REVISED the 73-8 contract. 73-8
    # assumed the overlay does not render last_beat_impact under hp_depletion (it
    # draws HP bars), so it settled for the honest "inert / no dial motion". The
    # playtest proved that assumption WRONG: BeatImpactPanel (ConfrontationOverlay
    # line ~1256) renders the summary UNCONDITIONALLY, so a damaging crit showed
    # the player "No change — moved nothing · Δ0" — a lie on the single most
    # salient mechanical event. New contract: under hp_depletion a strike that
    # ablated HP reads the HP CHANNEL — effect="advance" with the real HP delta.
    enc = _enc("hp_depletion")
    resolver, cores = _cores(pirate_hp=7)
    apply_beat(
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Success,
        turn=1, edge_resolver=resolver, damage_resolver=lambda: 3,
    )
    impact = enc.last_beat_impacts["player"]

    # Load-bearing truth (UNCHANGED from 73-8): no DIAL moved on-screen.
    assert impact["dial_moved"] is False
    # The HP it removed is the impact — not "inert".
    assert impact["effect"] == "advance"
    assert "3" in impact["summary"] and "hp" in impact["summary"].lower()
    # Still no phantom dial-edge claim — the move landed on HP, not the edge.
    assert "to your edge" not in impact["summary"].lower()

    # Cross-channel consistency: the readout's HP delta matches the real ablation.
    assert cores["Pirate"].hp.current == 4  # 7 - 3 damage, no mitigation


def test_hp_depletion_strike_critsuccess_leads_with_hp_keeps_tag():
    # A strike CritSuccess grants the fleeting "Opening" tag AND removes HP. Under
    # the revised hp_depletion contract (sq-playtest 2026-06-13) the HP the strike
    # ablated is the lead readout (effect="advance" with the HP delta) — but the
    # tag still fired (apply_beat grants it regardless) and must remain visible in
    # the summary, so a mechanics-first player sees both the damage and the angle.
    enc = _enc("hp_depletion")
    resolver, _ = _cores(pirate_hp=7)
    apply_beat(
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.CritSuccess,
        turn=1, edge_resolver=resolver, damage_resolver=lambda: 3,
    )
    impact = enc.last_beat_impacts["player"]

    assert impact["dial_moved"] is False
    assert impact["effect"] == "advance"  # HP channel leads under hp_depletion
    assert "3" in impact["summary"] and "hp" in impact["summary"].lower()
    assert impact["tag"] == "Opening"  # the tag still fired and is preserved
    assert "Opening" in impact["summary"]  # …and stays visible in the readout


# ── GREEN guards: behavior that is already truthful and must STAY truthful ────


def test_hp_depletion_strike_fail_is_inert():
    # No-op case: a Fail strike yields no deltas, so it is honest today. Guard
    # against a fix that accidentally changes the no-op classification.
    enc = _enc("hp_depletion")
    resolver, _ = _cores(pirate_hp=7)
    apply_beat(
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Fail,
        turn=1, edge_resolver=resolver, damage_resolver=lambda: 0,
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
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Success,
        turn=1, edge_resolver=resolver, damage_resolver=lambda: 3,
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
        enc, enc.actors[0], _PushResolutionBeat(), RollOutcome.Success,
        turn=1, edge_resolver=resolver, damage_resolver=None,
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
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Success,
        turn=1, edge_resolver=resolver, damage_resolver=lambda: 0,
    )
    impact = enc.last_beat_impacts["player"]
    assert impact["effect"] == "advance"
    assert impact["dial_moved"] is True
    assert enc.player_metric.current == 2  # dial really moved on a dial pack


def test_describe_beat_impact_hp_channel_branch_is_advance():
    # Pure-function pin of the new branch (sq-playtest 2026-06-13): under
    # suppression, a positive hp_removed reads as an advance carrying the HP
    # delta — the exact path that turns the overlay's "No change" on a crit into
    # a truthful "−N to their HP". Gated on suppression: hp_removed is ignored on
    # the dial path (a dial pack has no HP channel here).
    hp_impact = describe_beat_impact(
        ResolvedDeltas(),
        kind=BeatKind.strike,
        outcome=RollOutcome.CritSuccess,
        hp_depletion_suppressed=True,
        hp_removed=5,
    )
    assert hp_impact.effect == "advance"
    assert hp_impact.dial_moved is False
    assert "5" in hp_impact.summary and "hp" in hp_impact.summary.lower()

    # Same deltas, no suppression → hp_removed is not consulted (dial path).
    dial_impact = describe_beat_impact(
        ResolvedDeltas(),
        kind=BeatKind.strike,
        outcome=RollOutcome.CritSuccess,
        hp_depletion_suppressed=False,
        hp_removed=5,
    )
    assert dial_impact.effect == "inert"


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
