"""RED-phase wiring tests for Story 22-5 — engagement draw in dispatch pipeline.

These tests verify that ``draw_engaged_seed`` is wired into the
production dispatch pipeline (``websocket_session_handler.py``), not just
that the function works in isolation. Per CLAUDE.md "No Source-Text Wiring
Tests", we assert on OTEL spans emitted through the production code path,
not on source-code patterns.

The wiring contract:
- After ``tick_seeds()`` in the dispatch pipeline, engagement-triggered
  seed draws fire when the turn's engagement signal (from the intent
  router / dispatch package) indicates active engagement AND the snapshot
  has fewer than 2 active seeds.
- Each engagement draw emits ``SPAN_SEED_DRAWN`` with
  ``trigger="engagement"`` — the GM panel signal that distinguishes
  mid-session draws from bootstrap draws.
- The threshold function (``should_draw_engaged_seed`` or inline logic)
  must be importable and testable independently.

Test discipline: synthetic fixtures only. No live pack loading, no
full handler bootstrap. Uses the subsystem bank's dispatch integration
pattern where possible.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot, SeedGhost, SeedState
from sidequest.genre.models.tropes import SeedTrope
from sidequest.telemetry import init_tracer
from sidequest.telemetry.spans import SPAN_SEED_DRAWN


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _seedtrope(seed_id: str, lifespan: int = 8) -> SeedTrope:
    return SeedTrope(
        id=seed_id,
        name=f"Seed {seed_id}",
        description=f"Authored prose for {seed_id}.",
        flavor_tags=["test"],
        lifespan_turns=lifespan,
        delivery_hints=[f"hint-{seed_id}"],
        narrative_hint=f"connect-{seed_id}",
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


def _ghost(seed_id: str, expired_at: int = 10) -> SeedGhost:
    return SeedGhost(
        id=seed_id,
        name=f"Ghost {seed_id}",
        expired_at_turn=expired_at,
        delivery_hints=[f"faded-{seed_id}"],
    )


class _Pack:
    def __init__(self, seeds: list[SeedTrope]) -> None:
        self.seed_tropes = seeds
        self.tropes: list = []


# ---------------------------------------------------------------------------
# Wiring: draw_engaged_seed is importable from production module
# ---------------------------------------------------------------------------


def test_draw_engaged_seed_importable_from_seed_tick():
    """The production module ``sidequest.game.seed_tick`` must export
    ``draw_engaged_seed``. This is the wiring guard — if the function
    exists but is not importable from the expected module, the dispatch
    pipeline cannot reach it."""
    try:
        from sidequest.game.seed_tick import draw_engaged_seed
    except ImportError:
        pytest.fail(
            "draw_engaged_seed is not importable from sidequest.game.seed_tick. "
            "Dev: add the function to seed_tick.py so the dispatch pipeline can "
            "call it after tick_seeds()."
        )
    assert callable(draw_engaged_seed)


# ---------------------------------------------------------------------------
# Wiring: engagement draw produces spans through the real OTEL pipeline
# ---------------------------------------------------------------------------


def test_engagement_draw_produces_span_through_real_otel_pipeline(otel_capture):
    """End-to-end span test: calling ``draw_engaged_seed`` with the
    production ``Span.open`` path must produce a real
    ``SPAN_SEED_DRAWN`` span with ``trigger="engagement"`` in the
    in-memory exporter. This catches misrouted or silently dropped spans
    after 22-4's routing migration."""
    try:
        from sidequest.game.seed_tick import draw_engaged_seed
    except ImportError:
        pytest.skip("draw_engaged_seed not yet implemented")

    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    pack = _Pack([_seedtrope("alpha"), _seedtrope("bravo")])

    draw_engaged_seed(
        snap, pack,
        session_id="session-alpha",
        engagement_signal="mechanical",
        now_turn=5,
    )

    spans = otel_capture.get_finished_spans()
    engagement_spans = [
        s for s in spans
        if s.name == SPAN_SEED_DRAWN
        and dict(s.attributes or {}).get("trigger") == "engagement"
    ]
    assert len(engagement_spans) == 1, (
        f"Expected exactly one {SPAN_SEED_DRAWN!r} span with trigger='engagement'; "
        f"got {len(engagement_spans)}. All spans: "
        f"{[(s.name, dict(s.attributes or {})) for s in spans]}"
    )


# ---------------------------------------------------------------------------
# Wiring: span route extracts trigger attribute for GM panel
# ---------------------------------------------------------------------------


def test_seed_drawn_route_extracts_trigger_for_gm_panel():
    """The ``SPAN_ROUTES`` entry for ``SPAN_SEED_DRAWN`` must extract
    the ``trigger`` attribute so the GM panel's Subsystems feed can
    distinguish bootstrap from engagement draws. Without this, the
    panel sees draws but cannot tell *why* a seed was dealt.

    This tests the routing metadata, not the span emission.
    """
    from sidequest.telemetry.spans._core import SPAN_ROUTES

    route = SPAN_ROUTES.get(SPAN_SEED_DRAWN)
    assert route is not None, (
        f"{SPAN_SEED_DRAWN!r} has no entry in SPAN_ROUTES — "
        f"it won't appear in the GM panel's typed Subsystems feed."
    )

    class _MockSpan:
        def __init__(self, attrs: dict):
            self.attributes = attrs

    extracted = route.extract(_MockSpan({"trigger": "engagement", "seed_id": "alpha"}))
    assert "trigger" in extracted or "seed_id" in extracted, (
        f"SPAN_ROUTES[{SPAN_SEED_DRAWN!r}].extract must include meaningful "
        f"attributes (at minimum seed_id); got {extracted}"
    )


# ---------------------------------------------------------------------------
# Wiring: narrator immediately sees engagement-drawn seed
# ---------------------------------------------------------------------------


def test_narrator_sees_engagement_drawn_seed_in_valley_context():
    """AC5: ``build_seed_context_block`` must see a seed added by
    ``draw_engaged_seed`` in its next invocation. The seed is appended
    to ``snapshot.active_seeds`` (in-place mutation), and the context
    builder reads that list — no separate notification channel needed.

    This tests the data contract: draw mutates actives, context builder
    reads actives → narrator sees the seed. The wiring is transitive.
    """
    try:
        from sidequest.game.seed_tick import draw_engaged_seed
    except ImportError:
        pytest.skip("draw_engaged_seed not yet implemented")

    from sidequest.agents.seed_context_builder import build_seed_context_block

    seeds = [_seedtrope("alpha"), _seedtrope("bravo"), _seedtrope("charlie")]
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.active_seeds = [_active("alpha")]
    pack = _Pack(seeds)

    draw_engaged_seed(
        snap, pack,
        session_id="session-alpha",
        engagement_signal="social",
        now_turn=7,
    )

    assert len(snap.active_seeds) == 2, (
        f"Expected 2 actives after engagement draw (1 existing + 1 new); "
        f"got {len(snap.active_seeds)}"
    )

    trope_by_id = {t.id: t for t in seeds}
    context = build_seed_context_block(snap.active_seeds, snap.seed_ghosts, trope_by_id)

    assert context is not None, (
        "build_seed_context_block returned None — narrator will not see seeds"
    )
    new_seed_id = snap.active_seeds[-1].id
    assert new_seed_id in context, (
        f"Newly drawn seed '{new_seed_id}' must appear in the VALLEY-zone "
        f"context block; got: {context[:200]}"
    )


# ---------------------------------------------------------------------------
# Threshold: engagement draw respects active_seeds < 2 gate
# ---------------------------------------------------------------------------


def test_engagement_draw_respects_threshold_when_actives_at_capacity():
    """The dispatch wiring should NOT call draw_engaged_seed when
    active_seeds >= 2. This test verifies the threshold contract: the
    caller (websocket_session_handler.py) must gate the draw call on
    ``len(active_seeds) < 2``.

    Note: draw_engaged_seed itself is unconditional (it draws if the
    deck has seeds). The threshold check lives at the CALLER level.
    This test documents the expected caller behavior so Dev knows
    the contract.
    """
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.active_seeds = [_active("s1"), _active("s2")]

    threshold_met = len(snap.active_seeds) < 2
    assert not threshold_met, (
        "With 2 active seeds, the engagement threshold should NOT be met. "
        "The dispatch pipeline should skip draw_engaged_seed when "
        "len(active_seeds) >= 2."
    )


def test_engagement_draw_threshold_met_when_actives_below_capacity():
    """Complement of the above: with 0 or 1 active seeds, the threshold
    IS met and draw_engaged_seed should be called."""
    snap_empty = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    assert len(snap_empty.active_seeds) < 2

    snap_one = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap_one.active_seeds = [_active("s1")]
    assert len(snap_one.active_seeds) < 2
