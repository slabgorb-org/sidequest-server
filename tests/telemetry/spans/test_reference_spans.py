"""OTEL coverage for reference URL attachment."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans.reference import (
    SPAN_REFERENCE_URL_ATTACHED,
    SPAN_REFERENCE_URL_FAILED,
    SPAN_REFERENCE_URL_SKIPPED,
    reference_url_attached_span,
    reference_url_failed_span,
    reference_url_skipped_span,
)


def _make_tracer() -> tuple[InMemorySpanExporter, trace.Tracer]:
    """Return (exporter, tracer) pair bound together."""
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    tracer = provider.get_tracer("test")
    return exp, tracer


def _by_name(exp: InMemorySpanExporter, name: str) -> list:
    return [s for s in exp.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# url_attached
# ---------------------------------------------------------------------------


def test_url_attached_span_carries_kind_pack_world_keys() -> None:
    exp, tracer = _make_tracer()
    with reference_url_attached_span(
        kind="class",
        pack="tea_and_murder",
        world=None,
        keys=("Burglar",),
        _tracer=tracer,
    ):
        pass
    [span] = _by_name(exp, SPAN_REFERENCE_URL_ATTACHED)
    assert span.attributes["reference.kind"] == "class"
    assert span.attributes["reference.pack"] == "tea_and_murder"
    # world is None — attribute must be absent (not set to None or "")
    assert "reference.world" not in (span.attributes or {})
    assert span.attributes["reference.keys"] == "Burglar"


def test_url_attached_span_joins_multiple_keys() -> None:
    exp, tracer = _make_tracer()
    with reference_url_attached_span(
        kind="ability",
        pack="tea_and_murder",
        world=None,
        keys=("Burglar", "Cosh"),
        _tracer=tracer,
    ):
        pass
    [span] = _by_name(exp, SPAN_REFERENCE_URL_ATTACHED)
    # Multiple keys joined with "/" for readability in the GM panel.
    assert span.attributes["reference.keys"] == "Burglar/Cosh"


def test_url_attached_span_includes_world_when_provided() -> None:
    exp, tracer = _make_tracer()
    with reference_url_attached_span(
        kind="location",
        pack="tea_and_murder",
        world="glenross",
        keys=("The Manse",),
        _tracer=tracer,
    ):
        pass
    [span] = _by_name(exp, SPAN_REFERENCE_URL_ATTACHED)
    assert span.attributes["reference.world"] == "glenross"


# ---------------------------------------------------------------------------
# url_skipped
# ---------------------------------------------------------------------------


def test_url_skipped_span_carries_reason_and_world() -> None:
    exp, tracer = _make_tracer()
    with reference_url_skipped_span(
        kind="location",
        pack="tea_and_murder",
        world="glenross",
        keys=("A Bush",),
        reason="not_in_locations_yaml",
        _tracer=tracer,
    ):
        pass
    [span] = _by_name(exp, SPAN_REFERENCE_URL_SKIPPED)
    assert span.attributes["reference.kind"] == "location"
    assert span.attributes["reference.pack"] == "tea_and_murder"
    assert span.attributes["reference.world"] == "glenross"
    assert span.attributes["reference.keys"] == "A Bush"
    assert span.attributes["reference.reason"] == "not_in_locations_yaml"


def test_url_skipped_span_omits_world_when_none() -> None:
    exp, tracer = _make_tracer()
    with reference_url_skipped_span(
        kind="class",
        pack="caverns_and_claudes",
        world=None,
        keys=("Rogue",),
        reason="not_in_classes_yaml",
        _tracer=tracer,
    ):
        pass
    [span] = _by_name(exp, SPAN_REFERENCE_URL_SKIPPED)
    assert "reference.world" not in (span.attributes or {})


# ---------------------------------------------------------------------------
# url_failed
# ---------------------------------------------------------------------------


def test_url_failed_span_carries_reason() -> None:
    exp, tracer = _make_tracer()
    with reference_url_failed_span(
        kind="class",
        pack="tea_and_murder",
        world=None,
        keys=("Burglar",),
        reason="unknown_pack",
        _tracer=tracer,
    ):
        pass
    [span] = _by_name(exp, SPAN_REFERENCE_URL_FAILED)
    assert span.attributes["reference.reason"] == "unknown_pack"
    assert span.attributes["reference.kind"] == "class"
    assert span.attributes["reference.pack"] == "tea_and_murder"
    assert span.attributes["reference.keys"] == "Burglar"


# ---------------------------------------------------------------------------
# Routing: all three must be flat-only, not routed
# ---------------------------------------------------------------------------


def test_all_three_spans_are_flat_only() -> None:
    """Reference spans are forensics — they have no typed-event extractor."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS, SPAN_ROUTES

    for name in (
        SPAN_REFERENCE_URL_ATTACHED,
        SPAN_REFERENCE_URL_SKIPPED,
        SPAN_REFERENCE_URL_FAILED,
    ):
        assert name in FLAT_ONLY_SPANS, f"{name} must be in FLAT_ONLY_SPANS"
        assert name not in SPAN_ROUTES, f"{name} must not be in SPAN_ROUTES"


# ---------------------------------------------------------------------------
# Constant name pins — renaming breaks the GM panel silently
# ---------------------------------------------------------------------------


def test_span_constants_have_canonical_names() -> None:
    assert SPAN_REFERENCE_URL_ATTACHED == "sidequest.reference.url_attached"
    assert SPAN_REFERENCE_URL_SKIPPED == "sidequest.reference.url_skipped"
    assert SPAN_REFERENCE_URL_FAILED == "sidequest.reference.url_failed"
