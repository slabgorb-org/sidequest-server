"""Track B, Task 4 (Story 164-2): site.* seam spans MUST reach turn_telemetry.

OTEL doctrine (CLAUDE.md OTEL Observability Principle; SOUL.md — OTEL is the
Illusionism detector): the GM panel is the lie detector. ``Span.open`` alone
reaches Jaeger + the live GM dashboard, but the Postgres ``turn_telemetry`` sink
is fed ONLY by ``publish_event`` (see ``spans/movement.py``
``_mirror_movement_span_to_sink``). A site seam that opens a span but never
mirrors it is invisible to the panel — so we could not tell whether a player
actually crossed into a tavern/vault/the deep, or whether the narrator merely
improvised it.

RED: ``sidequest.telemetry.spans.site`` does not exist yet — Dev creates it in
GREEN (Task 4), mirroring the movement span module's SPAN_ROUTES + publish_event
pattern.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.telemetry.spans.site import (
    site_enter_span,
    site_enter_unresolved_span,
    site_exit_span,
    site_exit_unresolved_span,
)


def _recording(monkeypatch) -> InMemorySpanExporter:
    """Install an in-memory RECORDING provider so ``Span.open`` yields a recording
    span (one that carries ``.attributes``). The sink mirror only publishes for a
    recording span — with the default no-op provider the span has no attributes to
    mirror, so this fixture is what makes the positive assertion deterministic."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-site-spans-to-sink")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _capture_publish(monkeypatch) -> list[tuple]:
    """Capture every ``publish_event`` call made by the site span module."""
    import sidequest.telemetry.spans.site as site_spans

    calls: list[tuple] = []
    monkeypatch.setattr(
        site_spans,
        "publish_event",
        lambda event_type, fields, **kw: calls.append((event_type, fields, kw)),
    )
    return calls


def test_site_enter_span_mirrors_to_turn_telemetry(monkeypatch) -> None:
    """``site_enter_span`` must publish a ``state_transition`` / ``op: site.enter``
    event to the sink carrying the destination + resolution — not Span.open alone."""
    _recording(monkeypatch)
    calls = _capture_publish(monkeypatch)

    with site_enter_span(pc_name="Rux", site_id="frontier", from_region="the_dropmouth") as span:
        span.set_attribute("to_region", "frontier:entrance")
        span.set_attribute("resolved_via", "site_enter")

    assert calls, "site.enter MUST reach turn_telemetry via publish_event, not Span.open alone"
    event_type, fields, _kw = calls[0]
    assert event_type == "state_transition"
    assert fields["op"] == "site.enter"
    assert fields["site_id"] == "frontier"
    assert fields["from_region"] == "the_dropmouth"
    assert fields["to_region"] == "frontier:entrance"
    assert fields["resolved_via"] == "site_enter"


def test_site_exit_span_mirrors_to_turn_telemetry(monkeypatch) -> None:
    """``site_exit_span`` must publish ``op: site.exit`` with the surface owner as
    the destination."""
    _recording(monkeypatch)
    calls = _capture_publish(monkeypatch)

    with site_exit_span(pc_name="Rux", site_id="frontier", from_region="frontier:entrance") as span:
        span.set_attribute("to_region", "the_dropmouth")
        span.set_attribute("resolved_via", "site_exit")

    assert calls, "site.exit MUST reach turn_telemetry via publish_event"
    event_type, fields, _kw = calls[0]
    assert event_type == "state_transition"
    assert fields["op"] == "site.exit"
    assert fields["site_id"] == "frontier"
    assert fields["to_region"] == "the_dropmouth"


def test_site_enter_unresolved_span_mirrors_to_turn_telemetry(monkeypatch) -> None:
    """An unresolvable enter must ALSO reach the sink (``op: site.enter_unresolved``)
    with the reason + descriptor — a fail-loud that the GM panel can see, never a
    silent swallow."""
    _recording(monkeypatch)
    calls = _capture_publish(monkeypatch)

    with site_enter_unresolved_span(
        pc_name="Rux", from_region="square", reason="ambiguous_site", descriptor="the door"
    ):
        pass

    assert calls, "site.enter_unresolved MUST reach turn_telemetry via publish_event"
    event_type, fields, _kw = calls[0]
    assert event_type == "state_transition"
    assert fields["op"] == "site.enter_unresolved"
    assert fields["reason"] == "ambiguous_site"
    assert fields["descriptor"] == "the door"


def test_site_exit_unresolved_span_mirrors_to_turn_telemetry(monkeypatch) -> None:
    """An unresolvable EXIT must ALSO reach the sink (``op: site.exit_unresolved``)
    with the reason — the symmetric partner of the enter-unresolved mirror, so the
    GM panel's ``sites`` component is not blind to exit failures (Story 164-3
    review finding)."""
    _recording(monkeypatch)
    calls = _capture_publish(monkeypatch)

    with site_exit_unresolved_span(
        pc_name="Rux", from_region="frontier:entrance", reason="dangling_site_owner"
    ):
        pass

    assert calls, "site.exit_unresolved MUST reach turn_telemetry via publish_event"
    event_type, fields, _kw = calls[0]
    assert event_type == "state_transition"
    assert fields["op"] == "site.exit_unresolved"
    assert fields["reason"] == "dangling_site_owner"


def test_site_span_mirror_skips_when_span_not_recording(monkeypatch) -> None:
    """Robustness (mirrors the movement suite's NonRecordingSpan guard): with NO
    recording provider the span is non-recording and carries no attributes — the
    mirror must SKIP silently. It must NOT raise (the span wraps the live seam
    resolver; an exception here would fail the player's turn) and MUST NOT publish
    a half-empty event to the sink."""
    from opentelemetry.trace import NoOpTracer

    monkeypatch.setattr(spans_module, "tracer", lambda: NoOpTracer())
    calls = _capture_publish(monkeypatch)

    # Must not raise even though the span is non-recording.
    with site_enter_span(pc_name="Rux", site_id="frontier", from_region="the_dropmouth") as span:
        span.set_attribute("to_region", "frontier:entrance")

    assert calls == [], (
        "a non-recording span has no attributes to mirror; the sink must not be "
        f"published to. got: {calls}"
    )
