"""Lobby spans — slug disambiguation and existing-game join."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_LOBBY_FORCE_NEW_DISAMBIGUATED = "lobby.force_new_disambiguated"
SPAN_LOBBY_SESSION_JOIN_EXISTING = "lobby.session_join_existing"

FLAT_ONLY_SPANS.update(
    {
        SPAN_LOBBY_FORCE_NEW_DISAMBIGUATED,
        SPAN_LOBBY_SESSION_JOIN_EXISTING,
    }
)


@contextmanager
def lobby_force_new_disambiguated_span(
    requested_slug: str,
    final_slug: str,
    attempts: int,
    *,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_LOBBY_FORCE_NEW_DISAMBIGUATED,
        {
            "requested_slug": requested_slug,
            "final_slug": final_slug,
            "attempts": attempts,
            **attrs,
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def lobby_session_join_existing_span(
    slug: str,
    mode: str,
    *,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_LOBBY_SESSION_JOIN_EXISTING,
        {"slug": slug, "mode": mode, **attrs},
        tracer_override=_tracer,
    ) as span:
        yield span
