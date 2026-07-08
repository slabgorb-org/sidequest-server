"""Story 158-55 — the global OTEL ``TracerProvider``'s span-processor set must be
isolated between tests so a processor one test registers cannot leak into a later
test on the same worker (the rotating-OTEL-failure root cause). The fix is the
autouse ``_otel_provider_isolation`` fixture in ``tests/conftest.py``: it snapshots
the provider's processor tuple at setup and restores it at teardown.

WHY THIS SHAPE (the first cut was rejected in review): the obvious design — a
``test_a`` that leaks a processor and a separate ``test_b`` that asserts it did
not survive — passes VACUOUSLY under the suite's default ``-n auto``. xdist's
``load`` scheduler splits the two functions across workers, so ``test_b`` runs in
a different process and never observes ``test_a``'s leak; it is green whether or
not the fixture works. Pinning them to one worker with ``--dist loadgroup`` was
tried and REJECTED: loadgroup changes the whole suite's distribution and surfaces
DIFFERENT order-dependent flakes (net determinism regression — the opposite of
this story's goal). So the restore contract is instead exercised DETERMINISTICALLY
IN-PROCESS by driving the real fixture generator directly, plus a
scheduling-independent tripwire for the autouse wiring. Both are meaningful under
``-n auto`` and ``-n0`` alike, and neither touches global test distribution.

Mutation-checked: replace the fixture's restore body with a bare ``yield`` and
``test_isolation_fixture_restores_a_leaked_processor`` fails.
"""

from __future__ import annotations

import contextlib

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from tests.conftest import _otel_provider_isolation


class _LeakSentinelExporter(SpanExporter):
    """A uniquely-typed no-op exporter so the leaked processor is unambiguous."""

    def export(self, spans) -> SpanExportResult:  # noqa: ANN001
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def _sentinel_leaks() -> list[SimpleSpanProcessor]:
    """The sentinel processors currently registered on the global provider (empty
    once the isolation fixture has restored a clean processor set)."""
    provider = trace.get_tracer_provider()
    active = getattr(provider, "_active_span_processor", None)
    return [
        p
        for p in getattr(active, "_span_processors", ())
        if isinstance(p, SimpleSpanProcessor)
        and isinstance(getattr(p, "span_exporter", None), _LeakSentinelExporter)
    ]


def _drive_isolation_fixture_setup():  # noqa: ANN202 — returns a local teardown thunk
    """Run the REAL ``_otel_provider_isolation`` fixture's setup (snapshot) in this
    process and return a callable that runs its teardown (restore). Driving the
    generator directly makes the cross-test restore contract observable in ONE
    test — deterministic under ``-n auto`` with no reliance on xdist co-locating a
    separate leaker/observer pair. ``_get_wrapped_function`` is pytest's accessor
    for the undecorated fixture body; if a future pytest removes it this raises
    loudly here (no silent pass)."""
    generator = _otel_provider_isolation._get_wrapped_function()()
    next(generator)  # setup: snapshot the current processor tuple

    def _teardown() -> None:
        # A second next() runs the fixture body past its single yield (the
        # restore) and then exhausts the generator — StopIteration is the
        # expected completion signal, not a swallowed error.
        with contextlib.suppress(StopIteration):
            next(generator)

    return _teardown


def test_isolation_fixture_restores_a_leaked_processor() -> None:
    """Drive the real fixture setup, leak a sentinel processor onto the shared
    global provider (the offending suite-wide pattern), then drive the fixture
    teardown and assert the sentinel was stripped. Fails if the fixture's restore
    logic is removed or broken (mutation-checked). The autouse instance wrapping
    this test is a backstop cleanup, so a mid-test failure never leaks to peers."""
    run_teardown = _drive_isolation_fixture_setup()

    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        "the _otel_tracer_installed session fixture must have installed a real SDK "
        "TracerProvider before any test runs"
    )
    provider.add_span_processor(SimpleSpanProcessor(_LeakSentinelExporter()))
    assert _sentinel_leaks(), "precondition: the sentinel processor was registered"

    run_teardown()

    assert not _sentinel_leaks(), (
        "the _otel_provider_isolation fixture did not strip the leaked sentinel "
        "processor at teardown — its per-test restore of the global OTEL provider's "
        "processor set is broken, so processors would accumulate per xdist worker "
        "and rotate OTEL-assertion failures (story 158-55)."
    )


def test_isolation_fixture_is_autouse(request: pytest.FixtureRequest) -> None:
    """Scheduling-independent wiring tripwire: the isolation fixture must be
    autouse-registered so it runs around EVERY test. Fails if it is ever un-wired
    (deleted, or ``autouse`` dropped), regardless of xdist distribution."""
    assert "_otel_provider_isolation" in request.fixturenames, (
        "_otel_provider_isolation is no longer an autouse fixture — the global OTEL "
        "provider is no longer isolated between tests (story 158-55)."
    )
