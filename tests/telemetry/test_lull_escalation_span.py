"""Story 77-7 — ``SPAN_LULL_ESCALATION`` OTEL proof (ADR-024/025/128, ADR-031).

OTEL is LOAD-BEARING for this story — it is the whole point. The lull-escalation
step is the engine PUSHING a Bang when the game lulls; the span is the GM-panel
lie detector that distinguishes "the engine pushed" from "Claude improvised a
complication with zero mechanical backing." Without the span the dashboard's
pacing/tension grid stays dark and there is no way to tell the selector fired.

The new span routes under ``component='tension'`` (sibling of ``SPAN_PACING_HINT``
from 81-3) so it lights the pacing/tension subsystem on the GM panel ③ grid, and
it must fire on EVERY engaged run (fire, cooldown-skip, and none-available) —
never only on the happy path — carrying ``boring_streak``, ``drama_weight``,
``fired``, ``selected_seed_id``, and ``reason``. On a fire it also gives
``SPAN_SEED_FIRED`` (22-4, currently consumer-less in the lull path) its first
engine consumer.

Discipline: per CLAUDE.md "No Source-Text Wiring Tests", emission is asserted by
driving ``apply_lull_escalation`` and reading finished spans — never a grep of
source. Static route checks mirror ``tests/telemetry/test_seed_span_routing.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.session import GameSnapshot, SeedState
from sidequest.game.tension_tracker import RoundResult, TensionTracker
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.genre.models.tropes import SeedTrope
from sidequest.telemetry import init_tracer

# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def _seedtrope(seed_id: str, *, narrative_hint: str | None = None) -> SeedTrope:
    return SeedTrope(
        id=seed_id,
        name=f"Seed {seed_id}",
        flavor_tags=["test"],
        lifespan_turns=8,
        delivery_hints=[f"hint-{seed_id}"],
        narrative_hint=narrative_hint if narrative_hint is not None else f"connect-{seed_id}",
    )


def _active(seed_id: str) -> SeedState:
    return SeedState(
        id=seed_id,
        name=f"Seed {seed_id}",
        activated_at_turn=1,
        flavor_tags=["test"],
        lifespan_turns=8,
        delivery_hints=[f"hint-{seed_id}"],
    )


class _Pack:
    def __init__(self, seed_tropes: list[SeedTrope]) -> None:
        self.seed_tropes = seed_tropes
        self.tropes: list = []


def _tracker_in_lull(boring_turns: int) -> TensionTracker:
    tracker = TensionTracker()
    boring = RoundResult(round=1, damage_events=[], effects_applied=[], effects_expired=[])
    for _ in range(boring_turns):
        tracker.observe(boring, killed=None, lowest_hp_ratio=None)
    return tracker


def _snapshot(active: list[SeedState] | None = None) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="gulliver")
    if active:
        snap.active_seeds = list(active)
    return snap


def _lull_spans(exporter: InMemorySpanExporter) -> list:
    from sidequest.telemetry.spans import SPAN_LULL_ESCALATION

    return [s for s in exporter.get_finished_spans() if s.name == SPAN_LULL_ESCALATION]


# ---------------------------------------------------------------------------
# AC4 — static route registration (GM panel ③ pacing/tension grid)
# ---------------------------------------------------------------------------


def test_span_lull_escalation_constant_is_defined() -> None:
    """``SPAN_LULL_ESCALATION`` must be importable from the spans package — Dev
    adds it (e.g. in ``spans/pacing.py`` beside ``SPAN_PACING_HINT``) and
    re-exports it via ``__init__.py``.
    """
    from sidequest.telemetry.spans import SPAN_LULL_ESCALATION

    assert isinstance(SPAN_LULL_ESCALATION, str) and SPAN_LULL_ESCALATION, (
        f"SPAN_LULL_ESCALATION must be a non-empty string; got {SPAN_LULL_ESCALATION!r}"
    )


def test_lull_escalation_span_is_routed_not_flat() -> None:
    """The span must be ROUTED (in ``SPAN_ROUTES``), not flat-only — otherwise
    the watcher falls back to firehose-only emission and the GM panel's
    pacing/tension grid never surfaces the engine-push event.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_LULL_ESCALATION,
        SPAN_ROUTES,
    )

    assert SPAN_LULL_ESCALATION in SPAN_ROUTES, (
        f"{SPAN_LULL_ESCALATION!r} not in SPAN_ROUTES — the GM panel cannot "
        "verify the lull-escalation engine fired; route it like SPAN_PACING_HINT."
    )
    assert SPAN_LULL_ESCALATION not in FLAT_ONLY_SPANS, (
        f"{SPAN_LULL_ESCALATION!r} must not be flat-only — it needs typed routing."
    )


def test_lull_escalation_routes_under_tension_component() -> None:
    """It must share ``component='tension'`` with ``SPAN_PACING_HINT`` so it
    groups under the same GM-panel pacing/tension predicate (81-3 discipline) —
    this is the pacing subsystem, not the seeds subsystem.
    """
    from sidequest.telemetry.spans import SPAN_LULL_ESCALATION, SPAN_ROUTES

    route = SPAN_ROUTES.get(SPAN_LULL_ESCALATION)
    assert route is not None, f"{SPAN_LULL_ESCALATION!r} missing from SPAN_ROUTES"
    assert route.component == "tension", (
        f"route component={route.component!r}, expected 'tension' (sibling of "
        "SPAN_PACING_HINT) so it lights the pacing/tension grid cell."
    )
    assert route.event_type == "state_transition", (
        f"route event_type={route.event_type!r}, expected 'state_transition'."
    )


def test_lull_escalation_extractor_surfaces_decision_fields() -> None:
    """The route extractor must surface the five decision fields the GM panel
    renders: ``boring_streak``, ``drama_weight``, ``fired``, ``selected_seed_id``,
    ``reason``. These are exactly what proves "engine push, not improv".
    """
    from sidequest.telemetry.spans import SPAN_LULL_ESCALATION, SPAN_ROUTES

    route = SPAN_ROUTES[SPAN_LULL_ESCALATION]

    class _FakeSpan:
        name = SPAN_LULL_ESCALATION
        attributes = {
            "boring_streak": 5,
            "drama_weight": 0.2,
            "fired": True,
            "selected_seed_id": "alpha",
            "reason": "fired",
        }

    fields = route.extract(_FakeSpan())  # type: ignore[arg-type]
    assert fields.get("boring_streak") == 5, f"must surface boring_streak; got {fields}"
    assert fields.get("drama_weight") == 0.2, f"must surface drama_weight; got {fields}"
    assert fields.get("fired") is True, f"must surface fired; got {fields}"
    assert fields.get("selected_seed_id") == "alpha", f"must surface selected_seed_id; got {fields}"
    assert fields.get("reason") == "fired", f"must surface reason; got {fields}"


# ---------------------------------------------------------------------------
# AC4 — runtime emission: fire / cooldown / none-available / below-threshold
# ---------------------------------------------------------------------------


def test_span_emitted_on_fire_with_reason_fired(otel_capture) -> None:
    """A fire emits exactly one ``SPAN_LULL_ESCALATION`` carrying ``fired=True``,
    ``reason='fired'``, and the selected seed id — the GM-panel proof that the
    engine pushed this specific Bang.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    snap = _snapshot([_active("alpha")])
    apply_lull_escalation(
        snap,
        _Pack([_seedtrope("alpha")]),
        tracker=_tracker_in_lull(3),
        thresholds=DramaThresholds(escalation_streak=3),
        session_id="s1",
        now_turn=10,
    )

    spans = _lull_spans(otel_capture)
    assert len(spans) == 1, f"a fire must emit exactly one lull span; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("fired") is True, f"fired must be True on a fire; got {attrs}"
    assert attrs.get("reason") == "fired", f"reason must be 'fired'; got {attrs}"
    assert attrs.get("selected_seed_id") == "alpha", f"selected seed must be carried; got {attrs}"
    assert isinstance(attrs.get("boring_streak"), int), f"boring_streak must be an int; got {attrs}"
    assert isinstance(attrs.get("drama_weight"), float), (
        f"drama_weight must be a float; got {attrs}"
    )


def test_span_carries_session_and_world_attribution(otel_capture) -> None:
    """sq-playtest 2026-06-07 (77-7 forensics, split item b): the afternoon's
    ``fired=False reason=none_available`` spans carried NO session/genre/world
    attribution — the GM could not tell WHICH session's deck was empty without
    timestamp inference. Every lull span (all three reasons) must carry
    ``session_slug`` + ``genre_slug`` + ``world_slug``.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    # none_available — the exact forensics shape.
    snap = _snapshot([])
    apply_lull_escalation(
        snap,
        _Pack([]),
        tracker=_tracker_in_lull(3),
        thresholds=DramaThresholds(escalation_streak=3),
        session_id="2026-05-30-perseus_cloud",
        now_turn=10,
    )
    # fired — same attribution on the happy path.
    snap2 = _snapshot([_active("alpha")])
    apply_lull_escalation(
        snap2,
        _Pack([_seedtrope("alpha")]),
        tracker=_tracker_in_lull(3),
        thresholds=DramaThresholds(escalation_streak=3),
        session_id="2026-05-30-perseus_cloud",
        now_turn=10,
    )

    spans = _lull_spans(otel_capture)
    assert len(spans) == 2
    for span in spans:
        attrs = dict(span.attributes or {})
        assert attrs.get("session_slug") == "2026-05-30-perseus_cloud", (
            f"lull span must carry session attribution; got {attrs}"
        )
        assert attrs.get("genre_slug") == "wry_whimsy", attrs
        assert attrs.get("world_slug") == "gulliver", attrs


def test_span_emitted_on_cooldown_skip_with_reason_cooldown(otel_capture) -> None:
    """The cooldown-suppressed turn must STILL emit a span (``fired=False``,
    ``reason='cooldown'``) — silence on cooldown would read as "nothing
    happened" on the panel when in fact the governor actively held the engine
    back. ``selected_seed_id`` is the empty string (OTEL attributes reject None).
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    snap = _snapshot([_active("alpha"), _active("bravo")])
    pack = _Pack([_seedtrope("alpha"), _seedtrope("bravo")])
    thresholds = DramaThresholds(escalation_streak=3)

    apply_lull_escalation(
        snap,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=10,
    )
    otel_capture.clear()  # isolate the second (cooldown) emission

    apply_lull_escalation(
        snap,
        pack,
        tracker=_tracker_in_lull(3),
        thresholds=thresholds,
        session_id="s1",
        now_turn=11,
    )

    spans = _lull_spans(otel_capture)
    assert len(spans) == 1, f"the cooldown turn must still emit a span; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("fired") is False, f"fired must be False on cooldown; got {attrs}"
    assert attrs.get("reason") == "cooldown", f"reason must be 'cooldown'; got {attrs}"
    assert attrs.get("selected_seed_id") == "", (
        f"selected_seed_id must be '' (not None) when nothing fired — OTEL "
        f"attributes reject None; got {attrs.get('selected_seed_id')!r}"
    )


def test_span_emitted_when_none_available(otel_capture) -> None:
    """A lull with no seed to draw emits ``reason='none_available'`` — the panel
    must distinguish "engine wanted to push but the deck was empty" from "engine
    pushed" and from "no lull".
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    snap = _snapshot([])
    apply_lull_escalation(
        snap,
        _Pack([]),
        tracker=_tracker_in_lull(3),
        thresholds=DramaThresholds(escalation_streak=3),
        session_id="s1",
        now_turn=10,
    )

    spans = _lull_spans(otel_capture)
    assert len(spans) == 1, f"none-available must still emit a span; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("reason") == "none_available", f"reason must be 'none_available'; got {attrs}"
    assert attrs.get("fired") is False


def test_empty_deck_bootstrap_emits_seed_deck_empty_span(otel_capture) -> None:
    """77-7 split item (c): a session bootstrapping with NO authored seeds
    must emit ``seed.deck_empty`` once — the GM-panel signal that the
    lull-escalation engine was armed with an empty deck (No Silent
    Fallbacks). A deck WITH seeds emits nothing."""
    from sidequest.game.seed_tick import ensure_initial_draw

    snap = _snapshot()
    ensure_initial_draw(snap, _Pack([]), session_id="s-empty", now_turn=0)

    empty_spans = [s for s in otel_capture.get_finished_spans() if s.name == "seed.deck_empty"]
    assert len(empty_spans) == 1, "empty deck at bootstrap must emit seed.deck_empty"
    attrs = dict(empty_spans[0].attributes or {})
    assert attrs.get("genre_slug") == "wry_whimsy"
    assert attrs.get("world_slug") == "gulliver"
    assert attrs.get("session_slug") == "s-empty"

    # A populated deck stays quiet.
    snap2 = _snapshot()
    ensure_initial_draw(snap2, _Pack([_seedtrope("alpha")]), session_id="s-full", now_turn=0)
    empty_spans_after = [
        s for s in otel_capture.get_finished_spans() if s.name == "seed.deck_empty"
    ]
    assert len(empty_spans_after) == 1, "a populated deck must not emit seed.deck_empty"


def test_no_span_below_threshold(otel_capture) -> None:
    """Below the escalation threshold the step is a no-op and must emit NO lull
    span — the span fires only when the step actually engages (AC1 + AC4). A
    span every turn would drown the panel and lie about engagement.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation

    snap = _snapshot([_active("alpha")])
    apply_lull_escalation(
        snap,
        _Pack([_seedtrope("alpha")]),
        tracker=_tracker_in_lull(2),
        thresholds=DramaThresholds(escalation_streak=5),
        session_id="s1",
        now_turn=10,
    )

    assert _lull_spans(otel_capture) == [], (
        "below threshold the step is a no-op and must not emit SPAN_LULL_ESCALATION"
    )


def test_fire_gives_seed_fired_span_its_first_engine_consumer(otel_capture) -> None:
    """On a fire, ``SPAN_SEED_FIRED`` must also emit for the selected seed —
    the lull step is its first real ENGINE consumer (22-4 left it consumer-less
    in this path). The panel then attributes the fired Bang to the seed id.
    """
    from sidequest.game.lull_escalation import apply_lull_escalation
    from sidequest.telemetry.spans import SPAN_SEED_FIRED

    snap = _snapshot([_active("alpha")])
    apply_lull_escalation(
        snap,
        _Pack([_seedtrope("alpha")]),
        tracker=_tracker_in_lull(3),
        thresholds=DramaThresholds(escalation_streak=3),
        session_id="s1",
        now_turn=10,
    )

    fired = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_SEED_FIRED]
    assert len(fired) == 1, (
        f"a fire must emit one {SPAN_SEED_FIRED!r} for the selected seed; got {len(fired)}"
    )
    assert dict(fired[0].attributes or {}).get("seed_id") == "alpha", (
        "the seed.fired span must carry the fired seed's id"
    )
