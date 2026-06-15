"""RED (Story 117-6, AC-2): the ``narration.unminted_objective.suspected`` span
must carry a ``detection_method`` so the GM panel (the lie-detector) can tell
WHICH path flagged the objective — the new classifier vs. the legacy keyword
backstop. Without it a reviewer cannot tell whether the un-seeded classifier
engaged or the brittle substring matcher merely got lucky.

The span exists (wired in 117-4) and is routed to component="narrator". 117-6
adds the ``detection_method`` attribute and surfaces it through the SPAN_ROUTES
extract so it reaches the panel feed.

Today ``narration_unminted_objective_span`` takes only ``evidence`` and the
extract surfaces only ``evidence`` — so the ``detection_method`` assertions below
FAIL (unexpected-keyword / missing key) until 117-6 threads the method through.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def _fresh_tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


# A curated phrase that IS in _UNMINTED_OBJECTIVE_MARKERS, so the keyword backstop
# fires on it with no router package.
_CURATED_HOOK = "Your task is to find the missing courier before the Conglomerate does."

_SPAN_NAME = "narration.unminted_objective.suspected"


def _watcher_span_attrs(narration, *, package):
    """Drive the sync run_unminted_objective_watcher (the 117-4 path) and return the
    attributes of the emitted unminted-objective span (or None if it stayed silent)."""
    from sidequest.agents.dispatch_engagement_watcher import run_unminted_objective_watcher
    from sidequest.game.session import GameSnapshot

    tracer, exporter = _fresh_tracer_and_exporter()
    run_unminted_objective_watcher(
        narration=narration,
        snapshot=GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud"),
        package=package,
        tracer=tracer,
    )
    spans = [s for s in exporter.get_finished_spans() if s.name == _SPAN_NAME]
    return dict(spans[0].attributes or {}) if spans else None


def test_sync_watcher_tags_keyword_path_keyword() -> None:
    """The legacy curated-substring backstop (no router package) tags
    detection_method='keyword' — honest GM-panel attribution (Story 117-6)."""
    attrs = _watcher_span_attrs(_CURATED_HOOK, package=None)
    assert attrs is not None, "curated hook + empty quest_log must fire the keyword backstop"
    assert attrs.get("detection_method") == "keyword", (
        f"the keyword backstop must tag detection_method='keyword'; got {attrs}"
    )


def test_sync_watcher_tags_router_path_router() -> None:
    """The 117-4 router-backed path (a quest_offer accept that never minted) must NOT
    be mislabeled 'keyword' — it rides the router's structural classification, so it
    tags detection_method='router' (Story 117-6 honesty fix)."""
    from sidequest.protocol.dispatch import (
        DispatchPackage,
        PlayerDispatch,
        SubsystemDispatch,
        VisibilityTag,
    )

    package = DispatchPackage(
        turn_id="turn-1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="yeah, I'll look into it",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="quest_offer",
                        params={"quest_id": "floor_boss_missing_person", "decision": "accept"},
                        idempotency_key="qo-1",
                        confidence=0.9,
                        visibility=VisibilityTag(visible_to="all"),
                    )
                ],
            )
        ],
        confidence_global=0.9,
    )
    # An open-ended hook that trips ZERO curated markers — only the router signal fires.
    open_ended = (
        'The floor-boss leans in. "I have a... situation. Someone of mine stopped '
        'checking in down in the under-levels. Discreet work."'
    )
    attrs = _watcher_span_attrs(open_ended, package=package)
    assert attrs is not None, "router quest_offer accept + empty quest_log must fire the span"
    assert attrs.get("detection_method") == "router", (
        f"the router-backed path must tag detection_method='router', not 'keyword'; got {attrs}"
    )


def test_span_accepts_and_records_classifier_detection_method() -> None:
    """The classifier path tags the span detection_method='classifier'."""
    from sidequest.telemetry.spans.dispatch_engagement import (
        narration_unminted_objective_span,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    with narration_unminted_objective_span(
        evidence="floor-boss handed a discreet job; quest_log empty",
        detection_method="classifier",
        _tracer=tracer,
    ):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("detection_method") == "classifier", (
        f"span must record detection_method='classifier'; got {attrs}"
    )
    # The evidence attribute is preserved alongside the new field.
    assert "quest_log empty" in attrs.get("evidence", "")


def test_span_defaults_detection_method_to_keyword_for_legacy_path() -> None:
    """The legacy keyword backstop carries detection_method='keyword' so the GM
    panel can distinguish a real classification from a brittle substring hit. The
    span's default (no explicit method) represents the legacy keyword path."""
    from sidequest.telemetry.spans.dispatch_engagement import (
        narration_unminted_objective_span,
    )

    tracer, exporter = _fresh_tracer_and_exporter()
    with narration_unminted_objective_span(
        evidence="curated marker fired; quest_log empty",
        _tracer=tracer,
    ):
        pass

    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attrs.get("detection_method") == "keyword", (
        f"the legacy keyword backstop must tag detection_method='keyword'; got {attrs}"
    )


def test_span_route_extract_surfaces_detection_method() -> None:
    """The SPAN_ROUTES extract for the span must include detection_method so the
    GM-panel feed carries it through to the narrator-attributed event — not just
    the raw OTEL attribute."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    name = "narration.unminted_objective.suspected"
    assert name in SPAN_ROUTES, f"{name} not registered in SPAN_ROUTES"
    route = SPAN_ROUTES[name]
    assert route.component == "narrator"

    class _FakeSpan:
        attributes = {
            "evidence": "floor-boss handed a discreet job",
            "detection_method": "classifier",
        }

    extracted = route.extract(_FakeSpan())
    assert extracted.get("detection_method") == "classifier", (
        f"the span route extract must surface detection_method; got {extracted}"
    )
