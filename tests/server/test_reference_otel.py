"""OTEL span coverage for reference subsystem (rolled-in from story 63-1).

Tests the three context-manager span helpers in
``sidequest/telemetry/spans/reference.py``:

- ``reference_url_attached_span`` — INFO, URL constructed and set
- ``reference_url_skipped_span`` — INFO, entity missing from registry
- ``reference_url_failed_span`` — ERROR, programmer-error path

Tests also cover the Task 18 chrome-failure spans this story introduces:

- ``reference_theme_missing_span`` — ERROR, theme.yaml missing required field
- ``reference_hero_unbound_span`` — WARN, lore.yaml absent, falling back to pack name

Per project policy, every backend fix must emit OTEL on the decision so the
GM panel can confirm wiring (CLAUDE.md observability principle).
"""

from __future__ import annotations

from unittest.mock import MagicMock

# --- URL-attachment spans (already-shipped helpers; coverage gap from 63-1) ---


def test_url_attached_span_yields_and_sets_attrs():
    """reference_url_attached_span opens a span, sets reference.* attrs, yields."""
    from sidequest.telemetry.spans.reference import reference_url_attached_span

    tracer = MagicMock()
    span = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False

    with reference_url_attached_span(
        kind="ability",
        pack="caverns_and_claudes",
        world="caverns_sunden",
        keys=("classes", "knight", "charge"),
        _tracer=tracer,
    ) as observed:
        assert observed is span

    # Span name must be the constant defined in spans/reference.py
    name = tracer.start_as_current_span.call_args[0][0]
    assert name == "sidequest.reference.url_attached"


def test_url_skipped_span_carries_reason_attr():
    """reference_url_skipped_span attaches reference.reason for content-drift
    diagnostics. The reason is the load-bearing attr — GM panel filters by it.

    Span.open passes the full attrs dict via ``attributes=`` to
    tracer.start_as_current_span; we inspect that kwarg directly."""
    from sidequest.telemetry.spans.reference import reference_url_skipped_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_url_skipped_span(
        kind="ability",
        pack="demo",
        world=None,
        keys=("classes", "unknown_class"),
        reason="entity_not_in_registry",
        _tracer=tracer,
    ):
        pass

    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.reason"] == "entity_not_in_registry"
    assert attrs["reference.kind"] == "ability"
    assert attrs["reference.pack"] == "demo"
    assert attrs["reference.keys"] == "classes/unknown_class"


def test_url_failed_span_uses_failed_span_name():
    """The ERROR-tier span name is sidequest.reference.url_failed — verifying
    constants stay aligned with helper outputs (regression for rename drift)."""
    from sidequest.telemetry.spans.reference import (
        SPAN_REFERENCE_URL_FAILED,
        reference_url_failed_span,
    )

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_url_failed_span(
        kind="ability",
        pack="demo",
        world=None,
        keys=("classes", "bug"),
        reason="impossible_state",
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == SPAN_REFERENCE_URL_FAILED
    assert name == "sidequest.reference.url_failed"


def test_span_names_match_constants():
    """All three SPAN_REFERENCE_URL_* constants follow the
    sidequest.reference.url_* convention."""
    from sidequest.telemetry.spans.reference import (
        SPAN_REFERENCE_URL_ATTACHED,
        SPAN_REFERENCE_URL_FAILED,
        SPAN_REFERENCE_URL_SKIPPED,
    )

    assert SPAN_REFERENCE_URL_ATTACHED == "sidequest.reference.url_attached"
    assert SPAN_REFERENCE_URL_SKIPPED == "sidequest.reference.url_skipped"
    assert SPAN_REFERENCE_URL_FAILED == "sidequest.reference.url_failed"


def test_url_spans_registered_in_flat_only_set():
    """All three URL spans must be in FLAT_ONLY_SPANS so the GM-panel agent
    span fan-out reads them flat (no typed-event extractor)."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS
    from sidequest.telemetry.spans.reference import (
        SPAN_REFERENCE_URL_ATTACHED,
        SPAN_REFERENCE_URL_FAILED,
        SPAN_REFERENCE_URL_SKIPPED,
    )

    assert SPAN_REFERENCE_URL_ATTACHED in FLAT_ONLY_SPANS
    assert SPAN_REFERENCE_URL_SKIPPED in FLAT_ONLY_SPANS
    assert SPAN_REFERENCE_URL_FAILED in FLAT_ONLY_SPANS


# --- Chrome-failure spans (new for 63-4 Task 18 + Task 21) ---


def test_theme_missing_span_constant_exists():
    """Task 18 raises MissingThemeFieldError; the helper emits an ERROR span
    so the GM panel can see chrome-render failures without grepping logs."""
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_THEME_MISSING

    assert SPAN_REFERENCE_THEME_MISSING == "sidequest.reference.theme_missing"


def test_theme_missing_span_helper_emits_field_attr():
    """reference_theme_missing_span attaches reference.field so the GM panel
    can show WHICH theme.yaml field was missing for which pack."""
    from sidequest.telemetry.spans.reference import reference_theme_missing_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_theme_missing_span(
        pack="demo",
        field="display_font_family",
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == "sidequest.reference.theme_missing"
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.pack"] == "demo"
    assert attrs["reference.field"] == "display_font_family"


def test_hero_unbound_span_constant_exists():
    """Task 21 fallback path emits a WARN span when lore.yaml is missing and the
    hero falls back to the pack name."""
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_HERO_UNBOUND

    assert SPAN_REFERENCE_HERO_UNBOUND == "sidequest.reference.hero_unbound"


def test_hero_unbound_span_helper_emits_pack_world_attrs():
    """reference_hero_unbound_span carries pack + world so the GM panel can
    surface which world is missing lore.yaml."""
    from sidequest.telemetry.spans.reference import reference_hero_unbound_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_hero_unbound_span(
        pack="demo",
        world="glenross",
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == "sidequest.reference.hero_unbound"
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.pack"] == "demo"
    assert attrs["reference.world"] == "glenross"


def test_chrome_failure_spans_registered_in_flat_only_set():
    """theme_missing and hero_unbound must also be flat-only so they fan out
    to the GM panel without a typed-event extractor."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS
    from sidequest.telemetry.spans.reference import (
        SPAN_REFERENCE_HERO_UNBOUND,
        SPAN_REFERENCE_THEME_MISSING,
    )

    assert SPAN_REFERENCE_THEME_MISSING in FLAT_ONLY_SPANS
    assert SPAN_REFERENCE_HERO_UNBOUND in FLAT_ONLY_SPANS
