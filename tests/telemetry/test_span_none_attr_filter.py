"""Span.open drops None-valued attributes (story 158-48 benign-glance fix).

The OTEL SDK rejects a None attribute value and logs "Invalid type NoneType for
attribute '<k>'" on every span that carries one — surfaced on the WN
resolution-signal path where ``yield_side`` is None for a non-yield outcome
(player_victory). ``Span.open`` previously passed ``attrs`` verbatim. The key was
never actually recorded (OTEL drops it), so filtering it out is pure log-noise
removal; this pins that the centralized filter is in place and does not disturb
the non-None attributes.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.telemetry.spans.span import Span


@pytest.fixture
def exporter() -> Generator[InMemorySpanExporter, None, None]:
    """In-memory span exporter on the live singleton provider (mirrors
    tests/telemetry/test_tool_dispatch_span.py — set_tracer_provider is
    once-per-process, so add a processor to the existing provider instead)."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


def test_span_open_filters_none_attrs_keeps_others(exporter: InMemorySpanExporter) -> None:
    with Span.open(
        "test.none_filter",
        {"kept_str": "x", "kept_int": 0, "kept_false": False, "dropped": None},
    ):
        pass

    spans = [s for s in exporter.get_finished_spans() if s.name == "test.none_filter"]
    assert len(spans) == 1, f"expected one test.none_filter span; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    # The None-valued key must NOT reach OTEL (no "Invalid type NoneType" warning).
    assert "dropped" not in attrs, (
        "Span.open must filter None-valued attributes before they reach the OTEL SDK"
    )
    # Non-None attributes survive verbatim — including falsy-but-valid 0 / False
    # (the filter keys on `is None`, never on truthiness).
    assert attrs["kept_str"] == "x"
    assert attrs["kept_int"] == 0
    assert attrs["kept_false"] is False


def test_span_open_handles_none_attrs_arg(exporter: InMemorySpanExporter) -> None:
    """The attrs=None default path still opens a clean span (no crash)."""
    with Span.open("test.no_attrs"):
        pass

    spans = [s for s in exporter.get_finished_spans() if s.name == "test.no_attrs"]
    assert len(spans) == 1
    assert dict(spans[0].attributes or {}) == {}
