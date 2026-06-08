"""RED-phase tests for Story 22-5 — engagement-triggered mid-session seed draw.

Contract under test:

* ``draw_engaged_seed(snapshot, pack, *, session_id, engagement_signal,
  now_turn)`` draws one seed from the remaining deck when called during a
  turn with active player engagement.
* The draw reuses the existing ``SeedDeck`` (22-1) with ``drawn_ids``
  reconstructed from ``snapshot.active_seeds + snapshot.seed_ghosts`` — no
  new persistence, idempotent on reload.
* Each draw emits ``SPAN_SEED_DRAWN`` with ``trigger="engagement"`` so the
  GM panel can distinguish mid-session draws from bootstrap draws (22-4).
* The draw is a no-op when the deck is exhausted or the pack has no seeds
  — no error, no change.

Test-data discipline (feedback_no_content_coupled_tests): seeds are
injected fixtures, never loaded from a live ``genre_packs/*`` directory.
"""

from __future__ import annotations

import pytest

from sidequest.game.session import GameSnapshot, SeedGhost, SeedState
from sidequest.genre.models.tropes import SeedTrope
from sidequest.telemetry.spans import SPAN_SEED_DRAWN


def _try_import_draw_engaged_seed():
    """Try to import draw_engaged_seed from the expected module."""
    try:
        from sidequest.game.seed_tick import draw_engaged_seed

        return draw_engaged_seed
    except ImportError:
        return None


def _seed(seed_id: str, lifespan: int = 5) -> SeedTrope:
    return SeedTrope(
        id=seed_id,
        name=f"Seed {seed_id}",
        description=f"Authored prose for {seed_id}",
        flavor_tags=["test", "engagement"],
        lifespan_turns=lifespan,
        delivery_hints=["hint A", "hint B"],
        narrative_hint="connect to whichever NPC matters",
    )


def _active(seed_id: str, activated_at: int, lifespan: int = 5) -> SeedState:
    return SeedState(
        id=seed_id,
        name=f"Seed {seed_id}",
        activated_at_turn=activated_at,
        flavor_tags=["test"],
        lifespan_turns=lifespan,
        delivery_hints=["hint A", "hint B"],
    )


def _ghost(seed_id: str, expired_at: int) -> SeedGhost:
    return SeedGhost(
        id=seed_id,
        name=f"Seed {seed_id}",
        expired_at_turn=expired_at,
        delivery_hints=["hint A"],
    )


class _Pack:
    """Duck-typed pack — only ``seed_tropes`` is read by the draw engine."""

    def __init__(self, seeds: list[SeedTrope]) -> None:
        self.seed_tropes = seeds
        self.tropes: list = []


# ---------------------------------------------------------------------------
# Phase A — AC tests
# ---------------------------------------------------------------------------


def test_draw_engaged_seed_function_exists():
    """RED guard: draw_engaged_seed must be importable from seed_tick.

    Dev: add ``draw_engaged_seed(snapshot, pack, *, session_id,
    engagement_signal, now_turn)`` to ``sidequest/game/seed_tick.py``.
    Mutates ``snapshot.active_seeds`` in place. Returns None.
    """
    fn = _try_import_draw_engaged_seed()
    assert fn is not None, (
        "Could not import draw_engaged_seed from sidequest.game.seed_tick. "
        "Dev: add the function — signature: "
        "draw_engaged_seed(snapshot, pack, *, session_id, engagement_signal, "
        "now_turn) -> None"
    )


def test_engagement_draw_appends_seed_to_actives():
    """AC1: engagement draw adds a new seed to snapshot.active_seeds."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    assert snap.active_seeds == []

    pack = _Pack([_seed("s1"), _seed("s2"), _seed("s3")])

    fn(snap, pack, session_id="session-alpha", engagement_signal="mechanical", now_turn=5)

    assert len(snap.active_seeds) == 1, (
        f"Expected exactly one new seed in active_seeds; got {len(snap.active_seeds)}"
    )
    new_seed = snap.active_seeds[0]
    assert isinstance(new_seed, SeedState)
    assert new_seed.id in {"s1", "s2", "s3"}, (
        f"Drawn seed id must come from the pack; got '{new_seed.id}'"
    )


def test_engagement_draw_sets_correct_seed_state_fields():
    """The newly drawn SeedState must carry correct activated_at_turn,
    flavor_tags, lifespan_turns, and delivery_hints from the SeedTrope."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    pack = _Pack([_seed("s1", lifespan=8)])

    fn(snap, pack, session_id="session-alpha", engagement_signal="social", now_turn=10)

    assert len(snap.active_seeds) == 1
    state = snap.active_seeds[0]
    assert state.activated_at_turn == 10, (
        f"activated_at_turn must be now_turn (10); got {state.activated_at_turn}"
    )
    assert state.lifespan_turns == 8, (
        f"lifespan_turns must match the SeedTrope; got {state.lifespan_turns}"
    )
    assert state.flavor_tags == ["test", "engagement"], (
        f"flavor_tags must match the SeedTrope; got {state.flavor_tags}"
    )
    assert state.delivery_hints == ["hint A", "hint B"], (
        f"delivery_hints must match the SeedTrope; got {state.delivery_hints}"
    )


def test_engagement_draw_respects_already_drawn_seeds():
    """AC3: seeds already in active_seeds or seed_ghosts are never redrawn.

    The deck reconstruction from drawn_ids ensures previously dealt seeds
    are skipped — the draw-without-replacement contract from 22-1.
    """
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.active_seeds = [_active("s1", activated_at=0)]
    snap.seed_ghosts = [_ghost("s2", expired_at=3)]

    pack = _Pack([_seed("s1"), _seed("s2"), _seed("s3")])

    fn(snap, pack, session_id="session-alpha", engagement_signal="mechanical", now_turn=5)

    active_ids = {s.id for s in snap.active_seeds}
    assert "s3" in active_ids, (
        f"Expected s3 (the only undrawn seed) to be drawn; active_ids = {active_ids}"
    )
    assert len(snap.active_seeds) == 2, (
        f"Expected 2 actives (s1 from bootstrap + s3 from engagement); got {len(snap.active_seeds)}"
    )


def test_engagement_draw_no_op_when_deck_exhausted():
    """AC3 edge: when all seeds are already drawn (active + ghosted),
    further engagement draws are a no-op — no error, no change."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.active_seeds = [_active("s1", activated_at=0)]
    snap.seed_ghosts = [_ghost("s2", expired_at=3)]

    pack = _Pack([_seed("s1"), _seed("s2")])

    fn(snap, pack, session_id="session-alpha", engagement_signal="mechanical", now_turn=5)

    assert len(snap.active_seeds) == 1, (
        f"Exhausted deck must not add a seed; active count changed to {len(snap.active_seeds)}"
    )
    assert snap.active_seeds[0].id == "s1"


def test_engagement_draw_no_op_when_pack_has_no_seeds():
    """No-op when the pack has no seed_tropes at all — genre packs without
    authored seeds must not raise."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    pack = _Pack([])

    fn(snap, pack, session_id="session-alpha", engagement_signal="mechanical", now_turn=5)

    assert snap.active_seeds == []


def test_engagement_draw_no_op_when_pack_missing_seed_tropes():
    """No-op when the pack object doesn't have a seed_tropes attribute at
    all — duck-type safe, no AttributeError."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")

    class _BarePackNoSeedTropes:
        tropes: list = []

    fn(
        snap,
        _BarePackNoSeedTropes(),
        session_id="session-alpha",
        engagement_signal="social",
        now_turn=5,
    )

    assert snap.active_seeds == []


def test_engagement_draw_is_reproducible_for_same_session():
    """AC7: deck behavior is reproducible — re-instantiating SeedDeck on
    the same snapshot state (same session_id, same drawn_ids) produces the
    same next card. Two calls on identical snapshots must draw the same seed."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    seeds = [_seed(f"s{i}") for i in range(10)]

    snap_a = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap_a.active_seeds = [_active("s0", activated_at=0)]
    pack_a = _Pack(list(seeds))

    snap_b = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap_b.active_seeds = [_active("s0", activated_at=0)]
    pack_b = _Pack(list(seeds))

    fn(snap_a, pack_a, session_id="fixed-session", engagement_signal="mechanical", now_turn=5)
    fn(snap_b, pack_b, session_id="fixed-session", engagement_signal="mechanical", now_turn=5)

    drawn_a = snap_a.active_seeds[-1].id
    drawn_b = snap_b.active_seeds[-1].id
    assert drawn_a == drawn_b, (
        f"Same session_id + same drawn_ids must produce same next draw; "
        f"got '{drawn_a}' vs '{drawn_b}'"
    )


# ---------------------------------------------------------------------------
# Phase B — OTEL span tests (AC4)
# ---------------------------------------------------------------------------


def test_engagement_draw_emits_seed_drawn_span(otel_capture):
    """AC4: SPAN_SEED_DRAWN must fire with trigger='engagement' so the
    GM panel can distinguish mid-session draws from bootstrap draws."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    pack = _Pack([_seed("s1")])

    fn(snap, pack, session_id="session-alpha", engagement_signal="mechanical", now_turn=5)

    spans = otel_capture.get_finished_spans()
    drawn_spans = [s for s in spans if s.name == SPAN_SEED_DRAWN]
    assert len(drawn_spans) == 1, (
        f"Expected exactly one {SPAN_SEED_DRAWN} span; got {len(drawn_spans)}. "
        f"All spans: {[(s.name, dict(s.attributes or {})) for s in spans]}"
    )
    attrs = dict(drawn_spans[0].attributes or {})
    assert attrs.get("trigger") == "engagement", (
        f"SPAN_SEED_DRAWN must carry trigger='engagement'; got {attrs}"
    )
    assert attrs.get("seed_id") == "s1", (
        f"SPAN_SEED_DRAWN must carry the drawn seed_id; got {attrs}"
    )


def test_engagement_draw_span_carries_activated_at_turn(otel_capture):
    """The span must carry activated_at_turn for GM panel attribution."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    pack = _Pack([_seed("s1")])

    fn(snap, pack, session_id="session-alpha", engagement_signal="social", now_turn=12)

    spans = otel_capture.get_finished_spans()
    drawn_spans = [s for s in spans if s.name == SPAN_SEED_DRAWN]
    assert len(drawn_spans) == 1
    attrs = dict(drawn_spans[0].attributes or {})
    assert attrs.get("activated_at_turn") == 12, (
        f"SPAN_SEED_DRAWN must carry activated_at_turn=12; got {attrs}"
    )


def test_engagement_draw_no_span_when_deck_exhausted(otel_capture):
    """No span emitted when the deck is exhausted — the engine must not
    emit phantom draws."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.active_seeds = [_active("s1", activated_at=0)]
    pack = _Pack([_seed("s1")])

    fn(snap, pack, session_id="session-alpha", engagement_signal="mechanical", now_turn=5)

    spans = otel_capture.get_finished_spans()
    drawn_spans = [s for s in spans if s.name == SPAN_SEED_DRAWN]
    assert len(drawn_spans) == 0, (
        f"Exhausted deck must not emit SPAN_SEED_DRAWN; got {len(drawn_spans)} spans"
    )


# ---------------------------------------------------------------------------
# Phase B — Rule-enforcement tests (python-review-checklist)
# ---------------------------------------------------------------------------


def test_draw_engaged_seed_has_type_annotations():
    """Rule #3 (type annotations at boundaries): draw_engaged_seed must
    have annotated parameters and return type."""
    fn = _try_import_draw_engaged_seed()
    if fn is None:
        pytest.skip("draw_engaged_seed not yet implemented")

    import inspect

    sig = inspect.signature(fn)
    hints = fn.__annotations__ if hasattr(fn, "__annotations__") else {}
    assert "return" in hints, "draw_engaged_seed must have a return type annotation"
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        assert param.annotation is not inspect.Parameter.empty, (
            f"Parameter '{param_name}' in draw_engaged_seed must have a type annotation"
        )


# ---------------------------------------------------------------------------
# Regression: existing seed operations unbroken
# ---------------------------------------------------------------------------


def test_ensure_initial_draw_still_works_after_engagement_addition():
    """Regression: ensure_initial_draw (22-1) must still function correctly
    after draw_engaged_seed is added to seed_tick.py."""
    from sidequest.game.seed_tick import ensure_initial_draw

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    pack = _Pack([_seed(f"s{i}") for i in range(5)])

    ensure_initial_draw(snap, pack, session_id="session-alpha", now_turn=0, hand_size=3)

    assert len(snap.active_seeds) == 3, (
        f"ensure_initial_draw must still deal the full hand; got {len(snap.active_seeds)}"
    )


def test_ensure_initial_draw_empty_deck_is_a_loud_noop(caplog):
    """sq-playtest 2026-06-07 (77-7 forensics, split item c): a session whose
    seed source authors NO seeds silently armed a lull-escalation engine with
    an empty deck — ``fired=False reason=none_available`` every turn,
    invisible until forensics. Per No Silent Fallbacks the bootstrap no-op
    must be LOUD: a WARNING naming the genre/world, once at session bootstrap
    (the span half is pinned in tests/telemetry/test_lull_escalation_span.py)."""
    import logging

    from sidequest.game.seed_tick import ensure_initial_draw

    snap = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    with caplog.at_level(logging.WARNING):
        ensure_initial_draw(snap, _Pack([]), session_id="session-empty", now_turn=0)

    assert snap.active_seeds == []
    warned = [r for r in caplog.records if "seed.deck_empty" in r.getMessage()]
    assert warned, (
        "an empty seed deck at bootstrap must WARN — a lull-escalation engine "
        "with no ammunition is a configuration smell, not a silent no-op"
    )
    assert "space_opera" in warned[0].getMessage()


def test_ensure_initial_draw_empty_deck_signals_once_per_session(caplog):
    """The empty-deck signal must latch per session: an empty deck never
    populates active_seeds/seed_ghosts, so ``ensure_initial_draw`` re-enters
    the empty branch EVERY turn — without the ``_deck_empty_signaled`` latch
    the warning (and span) would fire once per turn, all session long. A
    different session must still fire its own signal."""
    import logging

    from sidequest.game import seed_tick
    from sidequest.game.seed_tick import ensure_initial_draw

    seed_tick._deck_empty_signaled.clear()
    snap = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    with caplog.at_level(logging.WARNING):
        ensure_initial_draw(snap, _Pack([]), session_id="session-once", now_turn=0)
        ensure_initial_draw(snap, _Pack([]), session_id="session-once", now_turn=1)

    warned = [r for r in caplog.records if "seed.deck_empty" in r.getMessage()]
    assert len(warned) == 1, (
        f"empty-deck signal must latch once per session, got {len(warned)} warnings"
    )

    caplog.clear()
    other = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    with caplog.at_level(logging.WARNING):
        ensure_initial_draw(other, _Pack([]), session_id="session-other", now_turn=0)

    warned = [r for r in caplog.records if "seed.deck_empty" in r.getMessage()]
    assert len(warned) == 1, "a DIFFERENT session must still fire its own signal"


def test_tick_seeds_still_works_after_engagement_addition():
    """Regression: tick_seeds (22-3) must still function correctly after
    draw_engaged_seed is added to seed_tick.py."""
    from sidequest.game.seed_tick import tick_seeds

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.active_seeds = [_active("s1", activated_at=0, lifespan=2)]
    pack = _Pack([_seed("s1", lifespan=2)])

    tick_seeds(snap, pack, now_turn=3)

    assert snap.active_seeds == [], "Expired seed must be removed"
    assert len(snap.seed_ghosts) == 1
    assert snap.seed_ghosts[0].id == "s1"
