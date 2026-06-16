"""Playtest 2026-06-01 (the_real_mccoy standoff): a ``dial_threshold``
confrontation driven by ``advance_confrontation`` alone never resolved and
never left ``Setup``.

Two parallel dial-mutation tools exist:

* ``advance_encounter_beat`` → ``apply_beat`` (beat_kinds.py) increments
  ``beat``, derives ``structured_phase`` from it, AND runs the
  ``current >= threshold`` resolution check.
* ``advance_confrontation`` mutates ``metric.current`` ONLY — no beat, no
  phase, no resolution; it returns ``crossed_threshold`` and trusts the
  narrator to follow up.

So a standoff the narrator drives purely with ``advance_confrontation`` heats
the dial while ``beat``/``structured_phase`` stay frozen at ``Setup``/0, and
crossing the threshold never resolves the encounter — the engine wedges.

The narration-apply pipeline already runs a post-turn resolution sweep for the
ADR-116 end-on-no-Other case (``_resolve_if_no_opponent_remains``) but had NO
dial-threshold sweep. ``_resolve_dial_threshold_and_phase`` is that missing
sweep: it runs every turn after all tool calls, so it sees the final dial no
matter which tool moved it. At/over threshold it resolves (mirroring
``apply_beat``); below threshold it advances ``structured_phase`` forward to
track the dial's heat so the GM panel and mechanics-first players see the
standoff progressing instead of frozen in Setup.

The RED-state failure: before the sweep exists, a threshold-crossed
``advance_confrontation`` dial leaves ``resolved=False`` and
``structured_phase=Setup`` after narration-apply.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.server.narration_apply import (
    _apply_narration_result_to_snapshot,
    _resolve_dial_threshold_and_phase,
)
from tests._helpers.session_room import room_for


def _standoff(
    *,
    player_current: int,
    opponent_current: int,
    threshold: int = 10,
    win_condition: str = "dial_threshold",
    structured_phase: EncounterPhase | None = EncounterPhase.Setup,
    resolved: bool = False,
) -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="standoff",
        win_condition=win_condition,  # type: ignore[arg-type]
        player_metric=EncounterMetric(
            name="tension", current=player_current, starting=0, threshold=threshold
        ),
        opponent_metric=EncounterMetric(
            name="tension", current=opponent_current, starting=0, threshold=threshold
        ),
        structured_phase=structured_phase,
        beat=0,
        actors=[
            EncounterActor(name="Vyvyan", role="participant", side="player"),
            EncounterActor(name="Denis Gilligan", role="participant", side="opponent"),
        ],
    )
    enc.resolved = resolved
    return enc


# ── unit: the sweep itself ──────────────────────────────────────────────────


def test_player_dial_at_threshold_resolves_player_victory() -> None:
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = _standoff(player_current=10, opponent_current=5)

    _resolve_dial_threshold_and_phase(snap)

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
    assert enc.structured_phase is EncounterPhase.Resolution


def test_opponent_dial_at_threshold_resolves_opponent_victory() -> None:
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = _standoff(player_current=4, opponent_current=12)

    _resolve_dial_threshold_and_phase(snap)

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "opponent_victory"
    assert enc.structured_phase is EncounterPhase.Resolution


def test_below_threshold_advances_phase_out_of_setup() -> None:
    """The headline symptom: tension 6/10 must NOT stay frozen in Setup. The
    dial is 60% of the way to threshold → Escalation."""
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = _standoff(player_current=6, opponent_current=5)

    _resolve_dial_threshold_and_phase(snap)

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is False, "below threshold — must not resolve"
    assert enc.structured_phase is EncounterPhase.Escalation


def test_phase_never_regresses() -> None:
    """If a beat already advanced the phase to Climax, a cooler dial reading
    must not drag it back."""
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = _standoff(
        player_current=2, opponent_current=1, structured_phase=EncounterPhase.Climax
    )

    _resolve_dial_threshold_and_phase(snap)

    enc = snap.encounter
    assert enc is not None
    assert enc.structured_phase is EncounterPhase.Climax


def test_already_resolved_is_noop() -> None:
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = _standoff(
        player_current=10, opponent_current=10, resolved=True, structured_phase=EncounterPhase.Setup
    )
    snap.encounter.outcome = "resolution_beat:holster"

    _resolve_dial_threshold_and_phase(snap)

    enc = snap.encounter
    assert enc is not None
    # Untouched — the sweep must not overwrite an already-settled outcome.
    assert enc.outcome == "resolution_beat:holster"
    assert enc.structured_phase is EncounterPhase.Setup


def test_hp_depletion_dials_are_inert_no_resolve() -> None:
    """For hp_depletion the dials are synthesized inert (threshold ~1e6); a high
    ``current`` must never falsely resolve via the dial sweep — HP is the only
    resolver for that win condition."""
    snap = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    snap.encounter = _standoff(
        player_current=10, opponent_current=10, threshold=10, win_condition="hp_depletion"
    )

    _resolve_dial_threshold_and_phase(snap)

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is False
    # hp_depletion phase is owned by the HP path, not the dial sweep.
    assert enc.structured_phase is EncounterPhase.Setup


def test_no_encounter_is_noop() -> None:
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = None
    _resolve_dial_threshold_and_phase(snap)  # must not raise
    assert snap.encounter is None


# ── wiring: reachable from the production narration-apply path ───────────────


def test_sweep_runs_in_narration_apply_resolves_threshold_dial() -> None:
    """Wiring: the sweep fires from ``_apply_narration_result_to_snapshot`` (the
    real per-turn entry point), so a dial the narrator pushed to threshold via
    ``advance_confrontation`` resolves at end of turn even with NO beat_selections
    and NO pack — exactly the standoff repro."""
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = _standoff(player_current=10, opponent_current=6)

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(narration="", beat_selections=[]),
        player_name="Vyvyan",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
    assert enc.structured_phase is EncounterPhase.Resolution


def test_sweep_runs_in_narration_apply_advances_phase_below_threshold() -> None:
    """Wiring: below threshold the production path advances the phase off Setup."""
    snap = GameSnapshot(genre_slug="spaghetti_western", world_slug="the_real_mccoy")
    snap.encounter = _standoff(player_current=6, opponent_current=5)

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(narration="", beat_selections=[]),
        player_name="Vyvyan",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is False
    assert enc.structured_phase is EncounterPhase.Escalation
