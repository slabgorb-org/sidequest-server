"""Reference-URL attachment spans — v2 hyperlink subsystem observability.

Per CLAUDE.md OTEL principle, every subsystem decision emits a span. The
``reference`` URL subsystem (Tasks 6-9) attaches URLs onto protocol
objects at construction time. Three outcomes are observable:

- ``sidequest.reference.url_attached`` — INFO; URL was constructed and set
  on the protocol object. Carries the keyed identity (kind + key path).
- ``sidequest.reference.url_skipped`` — INFO; the keyed entity is not in
  the YAML registry (content drift; recoverable — the UI renders plain
  text in this branch).
- ``sidequest.reference.url_failed`` — ERROR; programmer error path
  reserved for impossible states (e.g. unknown pack/world flowing into a
  builder post-session-bind). Should never fire in practice.

All three are flat-only — the GM dashboard reads them via the
``agent_span_close`` fan-out; no typed-event extractor needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_REFERENCE_URL_ATTACHED = "sidequest.reference.url_attached"
SPAN_REFERENCE_URL_SKIPPED = "sidequest.reference.url_skipped"
SPAN_REFERENCE_URL_FAILED = "sidequest.reference.url_failed"

FLAT_ONLY_SPANS.update(
    {
        SPAN_REFERENCE_URL_ATTACHED,
        SPAN_REFERENCE_URL_SKIPPED,
        SPAN_REFERENCE_URL_FAILED,
    }
)


def _attrs(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "reference.kind": kind,
        "reference.pack": pack,
        "reference.keys": "/".join(keys),
    }
    if world is not None:
        out["reference.world"] = world
    if extras:
        out.update(extras)
    return out


@contextmanager
def reference_url_attached_span(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_REFERENCE_URL_ATTACHED,
        _attrs(kind=kind, pack=pack, world=world, keys=keys),
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_url_skipped_span(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    reason: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_REFERENCE_URL_SKIPPED,
        _attrs(
            kind=kind,
            pack=pack,
            world=world,
            keys=keys,
            extras={"reference.reason": reason},
        ),
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_url_failed_span(
    *,
    kind: str,
    pack: str,
    world: str | None,
    keys: tuple[str, ...],
    reason: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    with Span.open(
        SPAN_REFERENCE_URL_FAILED,
        _attrs(
            kind=kind,
            pack=pack,
            world=world,
            keys=keys,
            extras={"reference.reason": reason},
        ),
        tracer_override=_tracer,
    ) as span:
        yield span
