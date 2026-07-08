"""RED (story 158-55, expanded scope) — the global OTEL ``TracerProvider`` must
be isolated between tests.

Root cause of the residual full-suite non-determinism (established 2026-07-08,
after story 162-1 retired the original ``PoolClosed`` symptom): dozens of test
files mutate the *process-global* ``TracerProvider`` — ``add_span_processor(...)``
onto the shared provider (the ``_attach_exporter`` pattern in
``tests/server/test_merged_mp_emitter_projection.py`` et al.) or a forced
``trace.set_tracer_provider(...)`` — and never restore it. Under ``-n auto``'s
dynamic ``--dist load`` distribution the victims land on a different worker each
run, so the failing set ROTATES: whichever OTEL-assertion test shares a worker
with a leaking mutator sees a polluted provider (accumulated ``SimpleSpanProcessor``s,
a dead ``BatchSpanProcessor`` → :4317 whose ``force_flush`` hangs the 30 s
budget, or a provider swapped out from under the production ``WatcherSpanProcessor``).

This pins the isolation CONTRACT the fix must satisfy: the set of span
processors registered on the global provider at the START of a test must not
carry a leftover processor registered by a PRIOR test. The GREEN fix is a
suite-wide autouse fixture in the root ``conftest.py`` that snapshots and
restores the global provider's processor set per test — the OTEL sibling of the
existing ``_watcher_hub_event_store_isolation`` guard.

VALIDATION: this is a cross-test isolation property, so it is validated serially
(``uv run pytest -n0 tests/telemetry/test_158_55_otel_provider_isolation.py``) —
the same ``-n0`` convention the story uses to prove order-dependent flakes. Test
``_a_...`` leaks a uniquely-typed processor onto the global provider; test
``_b_...`` asserts it did not survive into the next test. RED today (nothing
restores it); GREEN once the isolation fixture lands.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


class _LeakSentinelExporter(SpanExporter):
    """A uniquely-typed no-op exporter so the leak is unambiguous to find."""

    def export(self, spans):  # noqa: ANN001, D102
        return SpanExportResult.SUCCESS

    def shutdown(self):  # noqa: D102
        return None


def _global_span_processors():
    """The span processors registered on the current global provider (empty
    tuple when the API default proxy provider is active)."""
    provider = trace.get_tracer_provider()
    asp = getattr(provider, "_active_span_processor", None)
    return tuple(getattr(asp, "_span_processors", ()))


def _sentinel_leaks() -> list:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    return [
        p
        for p in _global_span_processors()
        if isinstance(p, SimpleSpanProcessor)
        and isinstance(getattr(p, "span_exporter", None), _LeakSentinelExporter)
    ]


def test_a_leaks_a_span_processor_onto_the_global_provider() -> None:
    """Mimic the suite-wide offending pattern: add a processor to the shared
    global provider and never remove it. (Always passes — it is the leaker.)"""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = trace.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):
        # No real SDK provider installed yet — install one, as the first
        # mutating test in a worker does. set_tracer_provider is once-only;
        # this is exactly the global mutation that then leaks.
        trace.set_tracer_provider(TracerProvider())
        provider = trace.get_tracer_provider()

    provider.add_span_processor(SimpleSpanProcessor(_LeakSentinelExporter()))
    assert _sentinel_leaks(), "precondition: the sentinel processor was registered"


def test_b_global_provider_has_no_leaked_processor_from_a_prior_test() -> None:
    """The isolation contract: a processor another test registered on the global
    provider must not survive into this test. RED today — nothing restores the
    global provider between tests, so ``test_a``'s sentinel is still attached."""
    leaks = _sentinel_leaks()
    assert not leaks, (
        f"{len(leaks)} span processor(s) leaked from a prior test onto the global "
        "OTEL TracerProvider — the suite has no per-test provider isolation, so "
        "processors accumulate per xdist worker and rotate OTEL-assertion failures "
        "(story 158-55). GREEN: add a root-conftest autouse fixture that snapshots "
        "and restores the global provider's processor set per test."
    )
