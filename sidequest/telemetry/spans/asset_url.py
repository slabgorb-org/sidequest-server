"""Asset-URL resolution span — fires every time the server emits a media URL."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_ASSET_URL_RESOLVED = "server.asset_url.resolved"

# Flat-only: the GM panel reads it via the agent_span_close fan-out; no
# typed event extractor needed yet (forensics-only span).
FLAT_ONLY_SPANS.add(SPAN_ASSET_URL_RESOLVED)


@contextmanager
def asset_url_resolved_span(
    *,
    relative_path: str,
    base_url: str,
    mode: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_ASSET_URL_RESOLVED,
        {
            "asset.relative_path": relative_path,
            "asset.base_url": base_url,
            "asset.mode": mode,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span
