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

# Chrome-render failure spans (Story 63-4 Tasks 18 + 21; Story 63-7 Task C).
SPAN_REFERENCE_THEME_MISSING = "sidequest.reference.theme_missing"
SPAN_REFERENCE_HERO_UNBOUND = "sidequest.reference.hero_unbound"
SPAN_REFERENCE_TOC_MISSING = "sidequest.reference.toc_missing"

# Body-presenter dispatch spans (Story 63-8 / reference-body-presenters plan).
SPAN_REFERENCE_UNKNOWN_FIELD = "sidequest.reference.unknown_field"
SPAN_REFERENCE_UNPRESENTED_FIELD = "sidequest.reference.unpresented_field"
SPAN_REFERENCE_PRESENTER_ERROR = "sidequest.reference.presenter_error"

FLAT_ONLY_SPANS.update(
    {
        SPAN_REFERENCE_URL_ATTACHED,
        SPAN_REFERENCE_URL_SKIPPED,
        SPAN_REFERENCE_URL_FAILED,
        SPAN_REFERENCE_THEME_MISSING,
        SPAN_REFERENCE_HERO_UNBOUND,
        SPAN_REFERENCE_TOC_MISSING,
        SPAN_REFERENCE_UNKNOWN_FIELD,
        SPAN_REFERENCE_UNPRESENTED_FIELD,
        SPAN_REFERENCE_PRESENTER_ERROR,
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


# --- Chrome-render failure spans (Story 63-4) ---


@contextmanager
def reference_theme_missing_span(
    *,
    pack: str,
    field: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """ERROR span fired when theme.yaml lacks a required chrome field.

    The renderer raises ``MissingThemeFieldError`` from inside this span so
    the OTEL exporter sees the failure status as well as the attributes.
    """
    with Span.open(
        SPAN_REFERENCE_THEME_MISSING,
        {"reference.pack": pack, "reference.field": field},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_hero_unbound_span(
    *,
    pack: str,
    world: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """WARN span fired when lore.yaml is missing or unbound for a lore page;
    the hero falls back to the pack name instead of the world name."""
    with Span.open(
        SPAN_REFERENCE_HERO_UNBOUND,
        {"reference.pack": pack, "reference.world": world},
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_toc_missing_span(
    *,
    pack: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """ERROR span fired when ``PACK_TOC`` has no entry for the requested pack
    and the renderer falls through to the documented 2-item default TOC
    (``reckoning``, ``bearing``).

    Not a silent fallback per SOUL doctrine — the GM panel surfaces the
    gap so authoring drift (a new pack added to content without chrome
    metadata) is visible in OTEL rather than buried in "the reference
    page looks slightly off."
    """
    with Span.open(
        SPAN_REFERENCE_TOC_MISSING,
        {"reference.pack": pack},
        tracer_override=_tracer,
    ) as span:
        yield span


# --- Body-presenter dispatch spans (Story 63-8) ---


@contextmanager
def reference_unknown_field_span(
    *,
    pack: str,
    world: str | None,
    file_stem: str,
    key_path: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """WARN — fired when a YAML field is in neither PUBLIC nor KEEPER.

    The dispatcher drops the field. Stderr log-once deduping happens in the
    renderer; the span itself fires every render so OTEL traces stay honest
    about the per-render state."""
    attrs: dict[str, Any] = {
        "reference.pack": pack,
        "reference.file_stem": file_stem,
        "reference.key_path": ".".join(key_path) or "<root>",
    }
    if world is not None:
        attrs["reference.world"] = world
    with Span.open(
        SPAN_REFERENCE_UNKNOWN_FIELD,
        attrs,
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_unpresented_field_span(
    *,
    pack: str,
    file_stem: str,
    key_path: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """INFO — fired when a PUBLIC field has no registered presenter; the
    generic dispatcher renders it as label/value. Intentional fallback, not
    a defect — surfaced so coverage gaps are visible without alarming."""
    with Span.open(
        SPAN_REFERENCE_UNPRESENTED_FIELD,
        {
            "reference.pack": pack,
            "reference.file_stem": file_stem,
            "reference.key_path": ".".join(key_path) or "<root>",
        },
        tracer_override=_tracer,
    ) as span:
        yield span


@contextmanager
def reference_presenter_error_span(
    *,
    pack: str,
    file_stem: str,
    key_path: tuple[str, ...],
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """ERROR — fired when a presenter raises. Caller (the dispatcher) is
    responsible for recording the exception and re-raising; this helper
    matches the thin Span.open shape used by the rest of the module."""
    with Span.open(
        SPAN_REFERENCE_PRESENTER_ERROR,
        {
            "reference.pack": pack,
            "reference.file_stem": file_stem,
            "reference.key_path": ".".join(key_path) or "<root>",
        },
        tracer_override=_tracer,
    ) as span:
        yield span
