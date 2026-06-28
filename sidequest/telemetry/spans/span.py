"""Span helper — opens OTEL spans with attribute boilerplate centralised."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace


class Span:
    """Open OTEL spans with typed attributes."""

    @staticmethod
    @contextmanager
    def open(
        name: str,
        attrs: dict[str, Any] | None = None,
        *,
        tracer_override: trace.Tracer | None = None,
    ) -> Iterator[trace.Span]:
        """Open ``name`` as the current span with ``attrs`` set verbatim.

        When ``tracer_override`` is None the default tracer is looked up via
        :mod:`sidequest.telemetry.spans` (lazy import) so test fixtures that
        monkeypatch ``spans.tracer`` to install an in-memory exporter still
        intercept the default path.
        """
        if tracer_override is None:
            from sidequest.telemetry import spans as _spans

            tracer_override = _spans.tracer()
        # Drop None-valued attributes (story 158-48): the OTEL SDK rejects a None
        # attribute value ("Invalid type NoneType for attribute '<k>'") and logs a
        # warning on every span that carries one — e.g. a resolution-signal span
        # whose ``yield_side`` is None for a non-yield outcome (player_victory).
        # The key was never actually recorded (OTEL drops it), so filtering here is
        # pure log-noise removal with no behavior change, centralized once.
        safe_attrs = {k: v for k, v in attrs.items() if v is not None} if attrs else {}
        with tracer_override.start_as_current_span(name, attributes=safe_attrs) as span:
            yield span
