"""Story 77-7 — engine lull-escalation: selector + fire + cooldown (ADR-024/025/128).

RED premise (Keith playtest, wry_whimsy/gulliver, 2026-06-04): the engine is
PASSIVE. The lull SIGNAL already exists (``TensionTracker.pacing_hint`` trips
``escalation_beat`` at ``boring_streak >= escalation_streak``) and the Bang
CATALOG already exists (the ADR-128 seed deck), but NOTHING wires the signal to
the catalog — no engine selects a seed and FIRES it as a concrete escalation
directive when the game lulls. ``active_seeds=[]``, ``active_stakes=''``, the
scenario subsystem silent for 10 turns. For a forever-GM who wants to be
SURPRISED, that is the gap between "fun but I had to prod" and "fun and it kept
coming at me."

These tests pin the SELECTOR — a new ``apply_lull_escalation`` step that, on the
live lull signal, picks one active seed (or draws one) and FIRES it, returning
the seed's ``narrative_hint`` as the concrete escalation directive for the next
turn. v1 reuse-first slice, NO new ADR.

API contract these tests assume (TEA test design, 77-7):

    from sidequest.game.lull_escalation import (
        apply_lull_escalation, LullEscalationResult,
    )

    result = apply_lull_escalation(
        snapshot, pack,
        tracker=<TensionTracker>,
        thresholds=<DramaThresholds>,
        session_id="...",
        now_turn=<int>,
    )

``LullEscalationResult`` fields: ``fired: bool``, ``selected_seed_id: str | None``,
``reason: str`` (one of ``"fired" | "cooldown" | "none_available" |
"not_triggered"``), ``directive: str | None`` (the fired seed's
``narrative_hint``).

Selection is deterministic and resume-safe (derived from session_id + turn +
the candidate seed ids via the seed_deck SHA-256 pattern) — NO ``random`` /
wallclock — so a resume re-fires identically (AC2/AC5). The OTEL span proof for
this step lives in ``tests/telemetry/test_lull_escalation_span.py``; the
handler/turn-path wiring proof lives in
``tests/server/test_lull_escalation_turn_wiring.py``.
"""

from __future__ import annotations

from sidequest.game.session import GameSnapshot, SeedState
from sidequest.game.tension_tracker import RoundResult, TensionTracker
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.genre.models.tropes import SeedTrope

# ---------------------------------------------------------------------------
# Helpers — synthetic pack / seeds / a tracker driven into a lull
# ---------------------------------------------------------------------------


def _seedtrope(seed_id: str, *, narrative_hint: str | None = None) -> SeedTrope:
    return SeedTrope(
        id=seed_id,
        name=f"Seed {seed_id}",
        description=f"Authored prose for {seed_id}.",
        flavor_tags=["test"],
        lifespan_turns=8,
        delivery_hints=[f"hint-{seed_id}"],
        narrative_hint=narrative_hint if narrative_hint is not None else f"connect-{seed_id}",
    )


def _active(seed_id: str, activated_at: int = 1) -> SeedState:
    return SeedState(
        id=seed_id,
        name=f"Seed {seed_id}",
        activated_at_turn=activated_at,
        flavor_tags=["test"],
        lifespan_turns=8,
        delivery_hints=[f"hint-{seed_id}"],
    )


class _Pack:
    """Minimal pack stand-in carrying the seed deck the selector reads."""

    def __init__(self, seed_tropes: list[SeedTrope]) -> None:
        self.seed_tropes = seed_tropes
        self.tropes: list = []


def _boring_round() -> RoundResult:
    return RoundResult(round=1, damage_events=[], effects_applied=[], effects_expired=[])


def _tracker_in_lull(boring_turns: int) -> TensionTracker:
    """A tracker driven to ``boring_streak == boring_turns`` via real Boring
    observations (gambler's ramp) — no private-field poking."""
    tracker = TensionTracker()
    for _ in range(boring_turns):
        tracker.observe(_boring_round(), killed=None, lowest_hp_ratio=None)
    assert tracker.boring_streak() == boring_turns, (
        f"expected boring_streak={boring_turns}, got {tracker.boring_streak()} "
        "— a Boring observation must bump the streak by one"
    )
    return tracker


def _snapshot(active: list[SeedState] | None = None) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="gulliver")
    if active:
        snap.active_seeds = list(active)
    return snap


# ---------------------------------------------------------------------------
# AC1 — the lull trigger reuses the LIVE signal; below threshold is a no-op
# ---------------------------------------------------------------------------


def test_below_threshold_is_noop() -> None:
    """When ``boring_streak < escalation_streak`` the step must NOT fire — it
    rides the existing ADR-024 tension track and does nothing until the genre's
    escalation threshold is crossed. No seed is selected, nothing is consumed.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    tracker = _tracker_in_lull(2)
    thresholds = DramaThresholds(escalation_streak=5)  # 2 < 5 → below threshold
    snap = _snapshot([_active("alpha")])
    pack = _Pack([_seedtrope("alpha")])

    result = apply_lull_escalation(
        snap, pack, tracker=tracker, thresholds=thresholds, session_id="s1", now_turn=10
    )

    assert result.fired is False, "below the escalation threshold the step must not fire"
    assert result.reason == "not_triggered", (
        f"below threshold the reason must be 'not_triggered'; got {result.reason!r}"
    )
    assert result.selected_seed_id is None
    assert result.directive is None
    # The candidate seed is untouched — still active, not consumed.
    assert [s.id for s in snap.active_seeds] == ["alpha"]


def test_at_threshold_fires() -> None:
    """At exactly ``boring_streak == escalation_streak`` the step engages —
    the boundary is inclusive, matching ``pacing_hint``'s ``>=`` comparison.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    tracker = _tracker_in_lull(3)
    thresholds = DramaThresholds(escalation_streak=3)  # 3 >= 3 → fires
    snap = _snapshot([_active("alpha")])
    pack = _Pack([_seedtrope("alpha")])

    result = apply_lull_escalation(
        snap, pack, tracker=tracker, thresholds=thresholds, session_id="s1", now_turn=10
    )

    assert result.fired is True, "boring_streak == escalation_streak must fire (inclusive)"
    assert result.reason == "fired"


# ---------------------------------------------------------------------------
# AC2 — select-and-fire an active seed; directive IS the seed's narrative_hint
# ---------------------------------------------------------------------------


def test_fires_active_seed_and_directive_is_its_narrative_hint() -> None:
    """On a lull the step selects one active seed and FIRES it — the fired
    seed's ``narrative_hint`` becomes the concrete escalation directive
    (REPLACING the generic 'environment shifts' text). The directive must be
    the authored hint, not a synthesized string.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    tracker = _tracker_in_lull(3)
    thresholds = DramaThresholds(escalation_streak=3)
    snap = _snapshot([_active("alpha")])
    pack = _Pack([_seedtrope("alpha", narrative_hint="A Thern forges a map into your pack.")])

    result = apply_lull_escalation(
        snap, pack, tracker=tracker, thresholds=thresholds, session_id="s1", now_turn=10
    )

    assert result.fired is True
    assert result.selected_seed_id == "alpha", (
        f"the selected seed must be the active seed; got {result.selected_seed_id!r}"
    )
    assert result.directive == "A Thern forges a map into your pack.", (
        "the directive must be the fired seed's authored narrative_hint, "
        f"not a generic/synthesized string; got {result.directive!r}"
    )


def test_empty_active_seeds_draws_one_then_fires() -> None:
    """When ``active_seeds`` is empty, the step draws one from the deck first
    (reuse ``draw_engaged_seed``) and fires it — the lull still produces a Bang
    even from a cold start. The drawn seed becomes active and its hint is the
    directive.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    tracker = _tracker_in_lull(3)
    thresholds = DramaThresholds(escalation_streak=3)
    snap = _snapshot([])  # nothing active yet
    pack = _Pack([_seedtrope("beta", narrative_hint="A storm rolls in off the dead sea.")])

    result = apply_lull_escalation(
        snap, pack, tracker=tracker, thresholds=thresholds, session_id="s1", now_turn=10
    )

    assert result.fired is True, "empty active_seeds must draw one then fire, not no-op"
    assert result.reason == "fired"
    assert result.selected_seed_id == "beta"
    assert result.directive == "A storm rolls in off the dead sea."
    # The drawn seed is now an active seed on the snapshot (drew before firing).
    assert "beta" in {s.id for s in snap.active_seeds}, (
        "the drawn seed must be added to snapshot.active_seeds before firing"
    )


def test_empty_active_and_empty_deck_is_none_available() -> None:
    """A lull with no active seeds AND an exhausted deck cannot fire — the step
    reports ``reason='none_available'`` (and still emits its span, see the
    telemetry suite) rather than silently doing nothing or raising.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    tracker = _tracker_in_lull(3)
    thresholds = DramaThresholds(escalation_streak=3)
    snap = _snapshot([])
    pack = _Pack([])  # empty deck

    result = apply_lull_escalation(
        snap, pack, tracker=tracker, thresholds=thresholds, session_id="s1", now_turn=10
    )

    assert result.fired is False
    assert result.reason == "none_available", (
        f"empty active + empty deck must be 'none_available'; got {result.reason!r}"
    )
    assert result.selected_seed_id is None
    assert result.directive is None


# ---------------------------------------------------------------------------
# AC2/AC5 — selection is deterministic and resume-safe (no random / wallclock)
# ---------------------------------------------------------------------------


def test_selection_is_deterministic_for_identical_inputs() -> None:
    """Two independent runs with identical (session_id, turn, candidate seeds)
    must select the SAME seed — the resume-safety property. A resume rebuilds
    the snapshot and must re-fire identically; a ``random``/wallclock pick would
    diverge.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    thresholds = DramaThresholds(escalation_streak=3)
    candidates = [_active("alpha"), _active("bravo"), _active("charlie")]
    pack = _Pack([_seedtrope(c.id) for c in candidates])

    picks = set()
    for _ in range(3):
        snap = _snapshot(candidates)
        result = apply_lull_escalation(
            snap,
            pack,
            tracker=_tracker_in_lull(3),
            thresholds=thresholds,
            session_id="session-fixed",
            now_turn=42,
        )
        assert result.fired is True
        picks.add(result.selected_seed_id)

    assert len(picks) == 1, (
        f"identical inputs must select the same seed every time (resume-safe); "
        f"got divergent picks {picks} — selection must not use random/wallclock"
    )


def test_selection_is_independent_of_active_seeds_list_order() -> None:
    """Selection must derive from the seed IDs (hash), not the list position —
    permuting ``active_seeds`` must NOT change the pick. This catches a lazy
    ``active_seeds[0]`` selector masquerading as deterministic: order-dependent
    selection is not resume-safe because save/load can reorder the list.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    thresholds = DramaThresholds(escalation_streak=3)
    pack = _Pack([_seedtrope("alpha"), _seedtrope("bravo"), _seedtrope("charlie")])

    snap_a = _snapshot([_active("alpha"), _active("bravo"), _active("charlie")])
    pick_a = apply_lull_escalation(
        snap_a,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=7,
    ).selected_seed_id

    snap_b = _snapshot([_active("charlie"), _active("bravo"), _active("alpha")])
    pick_b = apply_lull_escalation(
        snap_b,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=7,
    ).selected_seed_id

    assert pick_a == pick_b, (
        f"selection must be order-independent (id-hash, not list index); "
        f"permuted list gave {pick_a!r} vs {pick_b!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — respect the ADR-128 governor: never fire on consecutive turns
# ---------------------------------------------------------------------------


def test_cooldown_blocks_immediate_next_turn() -> None:
    """Two lull turns in a row: the step fires on turn 1, then the ADR-128
    ``FIRE_COOLDOWN_TURNS`` governor suppresses the immediate next turn with
    ``reason='cooldown'`` — the engine pushes, then breathes, never two turns
    running. The cooldown state persists on the same snapshot across calls.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    thresholds = DramaThresholds(escalation_streak=3)
    # Two active seeds so a (hypothetical) second fire would have a candidate —
    # proving the skip is the cooldown, not an empty deck.
    snap = _snapshot([_active("alpha"), _active("bravo")])
    pack = _Pack([_seedtrope("alpha"), _seedtrope("bravo")])

    first = apply_lull_escalation(
        snap,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=10,
    )
    assert first.fired is True and first.reason == "fired"

    second = apply_lull_escalation(
        snap,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=11,
    )
    assert second.fired is False, "must not fire on consecutive turns (FIRE_COOLDOWN_TURNS)"
    assert second.reason == "cooldown", (
        f"the immediate next turn must be suppressed with reason='cooldown'; got {second.reason!r}"
    )


def test_fires_again_after_cooldown_window_elapses() -> None:
    """The cooldown is a window, not a permanent lock — once
    ``FIRE_COOLDOWN_TURNS`` have elapsed since the last fire, a fresh lull fires
    again. (Uses the real ``FIRE_COOLDOWN_TURNS`` constant so the test tracks
    the governor, not a hardcoded number.)
    """
    from sidequest.game.lull_escalation import apply_lull_escalation
    from sidequest.game.trope_tuning import FIRE_COOLDOWN_TURNS

    thresholds = DramaThresholds(escalation_streak=3)
    snap = _snapshot([_active("alpha"), _active("bravo"), _active("charlie")])
    pack = _Pack([_seedtrope("alpha"), _seedtrope("bravo"), _seedtrope("charlie")])

    fire_turn = 10
    first = apply_lull_escalation(
        snap,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=fire_turn,
    )
    assert first.fired is True

    # A turn safely past the cooldown window must fire again.
    later = apply_lull_escalation(
        snap,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=fire_turn + FIRE_COOLDOWN_TURNS + 1,
    )
    assert later.fired is True, (
        f"after {FIRE_COOLDOWN_TURNS} cooldown turns elapse the step must fire "
        f"again; got reason={later.reason!r}"
    )
    assert later.reason == "fired"
