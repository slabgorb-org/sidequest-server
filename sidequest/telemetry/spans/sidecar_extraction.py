"""Post-narration sidecar-extractor spans — ADR-150 step 2 (Story 151-2).

The extractor reads the narrator's prose and derives the eleven bucket-B
sidecar fields in SHADOW mode (computes + emits OTEL, applies nothing). Every
decision is on the GM panel per CLAUDE.md "OTEL Observability Principle":

* ``sidecar_extraction.run`` (INFO) — once per successful extraction pass.
  Attributes: ``model``, ``prose_length``, ``field_count``, ``latency_ms``.
* ``sidecar_extraction.{field}`` (INFO) — once per bucket-B field, marking
  ``emitted`` True / empty False. Dynamically named (the field rides the span
  name, mirroring ``dispatch_engagement.{subsystem}``); these carry no
  ``SPAN_`` constant and ride the always-on ``agent_span_close`` fan-out — the
  run / mismatch / failed spans below carry the typed GM-panel routes.
* ``sidecar_extraction.mismatch`` (INFO) — the lie-detector: the extractor's
  output disagrees with engine-owned state (e.g. an ``npcs_present`` name the
  engine never seated). Mirrors ``dispatch_engagement.{subsystem}.mismatch``.
* ``sidecar_extraction.failed`` (ERROR) — once per failed attempt (timeout /
  transport / schema-invalid), so a retry-also-fails shows two ERROR spans
  (the same register ``intent_router.failed`` uses).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_SIDECAR_EXTRACTION_RUN = "sidecar_extraction.run"
SPAN_ROUTES[SPAN_SIDECAR_EXTRACTION_RUN] = SpanRoute(
    event_type="state_transition",
    component="sidecar_extraction",
    extract=lambda span: {
        "field": "sidecar_extraction.run",
        "model": (span.attributes or {}).get("model", ""),
        "prose_length": (span.attributes or {}).get("prose_length", 0),
        "field_count": (span.attributes or {}).get("field_count", 0),
        "latency_ms": (span.attributes or {}).get("latency_ms", 0),
    },
)

SPAN_SIDECAR_EXTRACTION_MISMATCH = "sidecar_extraction.mismatch"
SPAN_ROUTES[SPAN_SIDECAR_EXTRACTION_MISMATCH] = SpanRoute(
    event_type="state_transition",
    component="sidecar_extraction",
    extract=lambda span: {
        "field": (span.attributes or {}).get("field", ""),
        "evidence": (span.attributes or {}).get("evidence", ""),
    },
)

SPAN_SIDECAR_EXTRACTION_FAILED = "sidecar_extraction.failed"
SPAN_ROUTES[SPAN_SIDECAR_EXTRACTION_FAILED] = SpanRoute(
    event_type="state_transition",
    component="sidecar_extraction",
    extract=lambda span: {
        "field": "sidecar_extraction.failed",
        "reason": (span.attributes or {}).get("reason", ""),
        "raw_preview": (span.attributes or {}).get("raw_preview", ""),
        "retry_count": (span.attributes or {}).get("retry_count", 0),
    },
)

# Per-field spans are dynamically named ``sidecar_extraction.{field}`` (the field
# rides the name, mirroring ``dispatch_engagement.{subsystem}``). They have no
# ``SPAN_`` constant by design — eleven per-field typed routes would be noise on
# the panel — so they ride the always-on ``agent_span_close`` fan-out while the
# run / mismatch / failed spans above carry the typed routes.
_FIELD_SPAN_PREFIX = "sidecar_extraction."


@contextmanager
def sidecar_extraction_run_span(
    *,
    model: str,
    prose_length: int,
    field_count: int,
    latency_ms: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Producer-success span — once per successful extraction pass."""
    with Span.open(
        SPAN_SIDECAR_EXTRACTION_RUN,
        {
            "model": model,
            "prose_length": prose_length,
            "field_count": field_count,
            "latency_ms": latency_ms,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def sidecar_extraction_field_span(
    *,
    field: str,
    emitted: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Per-field emitted/empty span. ``emitted`` is the GM-panel signal that the
    extractor looked and found content — distinct from never having run."""
    with Span.open(
        f"{_FIELD_SPAN_PREFIX}{field}",
        {"field": field, "emitted": emitted, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def sidecar_extraction_mismatch_span(
    *,
    field: str,
    evidence: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Lie-detector span — the extractor's output disagrees with engine-owned
    state. ``field`` names the diverging field; ``evidence`` is the GM-panel
    detail (the analogue of ``dispatch_engagement`` mismatch evidence)."""
    with Span.open(
        SPAN_SIDECAR_EXTRACTION_MISMATCH,
        {"field": field, "evidence": evidence, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def sidecar_extraction_failed_span(
    *,
    reason: str,
    raw_preview: str = "",
    retry_count: int = 0,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Producer-failure span (ERROR-level), fired once per failed attempt.

    Marks the OTEL span status ERROR so the GM panel surfaces a real failure,
    not an INFO breadcrumb (No Silent Fallbacks). Two ERROR spans fire when the
    retry also fails; one fires on a retry that then succeeds."""
    with Span.open(
        SPAN_SIDECAR_EXTRACTION_FAILED,
        {"reason": reason, "raw_preview": raw_preview, "retry_count": retry_count, **attrs},
        tracer_override=_tracer,
    ) as span:
        span.set_status(StatusCode.ERROR, description=reason)
        yield span
