"""Story 22-4 — ``SPAN_SEED_FIRED`` call-site emission tests.

``SPAN_SEED_FIRED`` fires once per active seed when the seed engine
surfaces seed context for the narrator — the "this seed was shown to
the narrator" moment, sibling discipline of ``SPAN_TROPE_ACTIVATE``
("this trope was promoted to progressing").

The call site is inside the seed-context build path (either
``build_seed_context_block`` or its caller in the orchestrator). The
existing span from 22-3 (``narrator.seed_context_rendered`` or similar)
is an aggregate "injection happened" span; this per-seed span lets the
GM panel attribute individual seeds to narrator turns.

Test discipline: drive ``build_seed_context_block`` directly with
synthetic fixtures. Per CLAUDE.md "No Source-Text Wiring Tests", we
assert on emitted spans, not on source-code patterns.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import SeedGhost, SeedState
from sidequest.genre.models.tropes import SeedTrope
from sidequest.telemetry import init_tracer


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


def _seedtrope(seed_id: str, **overrides) -> SeedTrope:
    defaults = dict(
        id=seed_id,
        name=f"Seed {seed_id}",
        description=f"Authored prose for {seed_id}.",
        flavor_tags=["test"],
        lifespan_turns=8,
        delivery_hints=[f"hint-{seed_id}"],
        narrative_hint=f"connect-{seed_id}",
    )
    defaults.update(overrides)
    return SeedTrope(**defaults)


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


# ---------------------------------------------------------------------------
# AC3 — SPAN_SEED_FIRED emits per active seed during context build
# ---------------------------------------------------------------------------


def test_seed_fired_span_emits_per_active_seed(otel_capture) -> None:
    """When ``build_seed_context_block`` renders active seeds into
    VALLEY-zone context, one ``seed.fired`` span must fire per active
    seed — not one per call, not zero. This is the per-seed lie-detector
    signal: the GM panel can confirm "seed X was surfaced this turn".

    The span name must match ``SPAN_SEED_FIRED`` from the seed spans
    module. We import the constant to avoid string-matching drift.
    """
    from sidequest.agents.seed_context_builder import build_seed_context_block
    from sidequest.telemetry.spans import SPAN_SEED_FIRED

    tropes = [_seedtrope("alpha"), _seedtrope("bravo"), _seedtrope("charlie")]
    actives = [_active("alpha"), _active("bravo"), _active("charlie")]
    trope_by_id = {t.id: t for t in tropes}

    build_seed_context_block(actives, [], trope_by_id)

    spans = otel_capture.get_finished_spans()
    fired_spans = [s for s in spans if s.name == SPAN_SEED_FIRED]
    assert len(fired_spans) == 3, (
        f"Expected one {SPAN_SEED_FIRED!r} span per active seed (3 actives); "
        f"got {len(fired_spans)}. Spans emitted: "
        f"{[(s.name, dict(s.attributes or {})) for s in spans]}"
    )


def test_seed_fired_span_carries_seed_id(otel_capture) -> None:
    """Each ``seed.fired`` span must carry ``seed_id`` so the GM panel
    can attribute the surfacing event to a specific seed. Without this
    the panel shows "something fired" but cannot tell what.
    """
    from sidequest.agents.seed_context_builder import build_seed_context_block
    from sidequest.telemetry.spans import SPAN_SEED_FIRED

    tropes = [_seedtrope("alpha"), _seedtrope("bravo")]
    actives = [_active("alpha"), _active("bravo")]
    trope_by_id = {t.id: t for t in tropes}

    build_seed_context_block(actives, [], trope_by_id)

    spans = otel_capture.get_finished_spans()
    fired_spans = [s for s in spans if s.name == SPAN_SEED_FIRED]
    fired_ids = {dict(s.attributes or {}).get("seed_id") for s in fired_spans}
    assert fired_ids == {"alpha", "bravo"}, (
        f"Expected seed_id attributes matching the active seeds; "
        f"got {fired_ids}. Each {SPAN_SEED_FIRED!r} span must carry "
        f"the seed_id of the seed being surfaced."
    )


def test_seed_fired_does_not_fire_for_ghosts(otel_capture) -> None:
    """Ghost seeds are faded — they were surfaced in a prior session
    and have since expired. ``seed.fired`` must NOT fire for ghosts;
    ghosts get rendered as "[Faded]" callbacks but are not "fired"
    in the active sense. Confusing fired and faded would mis-attribute
    injection state on the GM panel.
    """
    from sidequest.agents.seed_context_builder import build_seed_context_block
    from sidequest.telemetry.spans import SPAN_SEED_FIRED

    ghosts = [_ghost("ancestor"), _ghost("legacy")]

    build_seed_context_block([], ghosts, {})

    spans = otel_capture.get_finished_spans()
    fired_spans = [s for s in spans if s.name == SPAN_SEED_FIRED]
    assert len(fired_spans) == 0, (
        f"Ghost-only render should NOT emit {SPAN_SEED_FIRED!r} spans; "
        f"got {len(fired_spans)}. Ghosts are faded, not fired."
    )


def test_seed_fired_does_not_fire_for_empty_state(otel_capture) -> None:
    """No active seeds, no ghosts → no ``seed.fired`` span. The
    function returns None on empty input; even if it doesn't, no
    per-seed span should fire when there are zero seeds to surface.
    """
    from sidequest.agents.seed_context_builder import build_seed_context_block
    from sidequest.telemetry.spans import SPAN_SEED_FIRED

    build_seed_context_block([], [], {})

    spans = otel_capture.get_finished_spans()
    fired_spans = [s for s in spans if s.name == SPAN_SEED_FIRED]
    assert len(fired_spans) == 0, (
        f"Empty-state build should NOT emit {SPAN_SEED_FIRED!r} spans; "
        f"got {len(fired_spans)}."
    )


def test_seed_fired_with_mixed_actives_and_ghosts(otel_capture) -> None:
    """Mixed state: actives + ghosts. Only actives trigger
    ``seed.fired``; ghosts are rendered but do not fire. Catches an
    implementation that iterates all seeds (actives + ghosts) and
    emits fired for every one.
    """
    from sidequest.agents.seed_context_builder import build_seed_context_block
    from sidequest.telemetry.spans import SPAN_SEED_FIRED

    tropes = [_seedtrope("alpha")]
    actives = [_active("alpha")]
    ghosts = [_ghost("ancestor")]
    trope_by_id = {t.id: t for t in tropes}

    build_seed_context_block(actives, ghosts, trope_by_id)

    spans = otel_capture.get_finished_spans()
    fired_spans = [s for s in spans if s.name == SPAN_SEED_FIRED]
    assert len(fired_spans) == 1, (
        f"Expected 1 fired span (1 active, 1 ghost); got {len(fired_spans)}. "
        f"Only actives trigger {SPAN_SEED_FIRED!r}."
    )
    attrs = dict(fired_spans[0].attributes or {})
    assert attrs.get("seed_id") == "alpha", (
        f"Fired span should carry the active seed's id, not the ghost's; "
        f"got attrs={attrs}"
    )


# ---------------------------------------------------------------------------
# AC3 — Regression: existing seed spans still fire after routing migration
# ---------------------------------------------------------------------------


def test_seed_drawn_still_fires_after_routing_migration(otel_capture) -> None:
    """Regression guard: ``SPAN_SEED_DRAWN`` must still fire from
    ``ensure_initial_draw`` after the 22-4 routing migration moves it
    from ``FLAT_ONLY_SPANS`` to ``SPAN_ROUTES``. The span emission
    call site in ``seed_tick.py`` doesn't change — only the routing
    metadata does — but this test catches an accidental rename or
    removal during the migration.
    """
    from sidequest.game.seed_tick import ensure_initial_draw
    from sidequest.game.session import GameSnapshot
    from sidequest.telemetry.spans import SPAN_SEED_DRAWN

    snap = GameSnapshot(genre_slug="x", world_slug="y")

    class _Pack:
        seed_tropes = [_seedtrope(f"s{i}") for i in range(5)]
        tropes: list = []

    ensure_initial_draw(snap, _Pack(), session_id="sess-1", now_turn=0, hand_size=2)

    spans = otel_capture.get_finished_spans()
    drawn_spans = [s for s in spans if s.name == SPAN_SEED_DRAWN]
    assert len(drawn_spans) == 2, (
        f"Expected 2 {SPAN_SEED_DRAWN!r} spans (hand_size=2); got "
        f"{len(drawn_spans)}. Routing migration may have broken the "
        f"emission call site."
    )


def test_seed_expired_still_fires_after_routing_migration(otel_capture) -> None:
    """Regression guard: ``SPAN_SEED_EXPIRED`` must still fire from
    ``tick_seeds`` after the routing migration. Same rationale as the
    drawn regression test.
    """
    from sidequest.game.seed_tick import tick_seeds
    from sidequest.game.session import GameSnapshot
    from sidequest.telemetry.spans import SPAN_SEED_EXPIRED

    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active("alpha", activated_at=0)]

    class _Pack:
        seed_tropes: list = []
        tropes: list = []

    tick_seeds(snap, _Pack(), now_turn=20)

    spans = otel_capture.get_finished_spans()
    expired_spans = [s for s in spans if s.name == SPAN_SEED_EXPIRED]
    assert len(expired_spans) == 1, (
        f"Expected 1 {SPAN_SEED_EXPIRED!r} span (1 expired seed); got "
        f"{len(expired_spans)}. Routing migration may have broken the "
        f"emission call site."
    )


# ---------------------------------------------------------------------------
# Wiring test: seed spans are importable from the package namespace
# ---------------------------------------------------------------------------


def test_all_four_seed_span_constants_importable() -> None:
    """Integration guard: all four seed span constants must be
    importable from the top-level ``sidequest.telemetry.spans`` package.
    The ``__init__.py`` re-exports via ``from .seed import *`` — this
    test catches a missing ``__all__`` entry or a broken star-import.
    """
    from sidequest.telemetry import spans

    for name in ("SPAN_SEED_DRAWN", "SPAN_SEED_EXPIRED", "SPAN_SEED_FIRED", "SPAN_SEED_PROMOTED"):
        assert hasattr(spans, name), (
            f"{name} not importable from sidequest.telemetry.spans. "
            f"Check that it's defined in spans/seed.py and listed in "
            f"__all__."
        )
        val = getattr(spans, name)
        assert isinstance(val, str) and val, f"{name} must be a non-empty string; got {val!r}"
