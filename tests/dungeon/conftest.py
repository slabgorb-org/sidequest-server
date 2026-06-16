"""Shared test infra for the dungeon test package.

OTEL global-provider hermeticity (Plan 6, Tasks 4 + 6)
------------------------------------------------------
`test_commit_and_ledger_emit_spans` installs its own `_Capture`
`TracerProvider` via `trace.set_tracer_provider()` so it can assert that
`dungeon.persist.commit` / `ledger.add` / `ledger.resolve` spans really
fire. OTEL 1.x gates `set_tracer_provider()` behind a once-only guard
(`_TRACER_PROVIDER_SET_ONCE`): in a full-suite session an earlier
conftest's `init_tracer()` already won that guard, so the test's call
was a SILENT no-op — the `_Capture` exporter never installed, the test
never actually asserted anything (a dead green).

Plan 6 Task 4 added `reset_otel_provider()` so the call becomes
effective and the test really runs. But making it effective without a
matching restore meant the test then LEAKED its `_Capture` provider into
the global slot for the rest of the pytest process — contaminating
later `otel_capture`-using tests (`test_visibility_classifier`,
`test_chargen_persist_and_play`). Task 4's reset-before was only half
the fix; the restore-after is the other half. Plan 6 owns completing it
(it is Plan 6's helper that activated the previously-dormant no-op).

The fix is hermetic save-and-restore:

* `capture_otel_provider_state()` snapshots the prior global provider
  reference AND the `Once` guard's `_done` flag.
* `reset_otel_provider()` clears both so `set_tracer_provider()` is not
  a no-op (unchanged behaviour — kept for the call site).
* `restore_otel_provider_state(state)` puts the captured prior provider
  and guard flag back, so after the test the global tracer provider is
  EXACTLY what it was before — no downstream contamination.

`test_commit_and_ledger_emit_spans` wraps its provider install +
assertions in `try/finally`: capture → reset → set → assert → (finally)
restore. It both (a) starts from a clean provider and (b) leaves the
global provider exactly as found. Its three span assertions are intact:
it still really runs and really asserts.

Private-API access (`trace._TRACER_PROVIDER`,
`trace._TRACER_PROVIDER_SET_ONCE`) — update if the OTEL SDK moves the
guard. This mirrors the established private-API helper at
`tests/telemetry/test_spans.py:_reset_otel_provider`; it is duplicated
here (a sibling package-local helper) rather than cross-imported — the
underscore-prefixed test-module helper is intentionally module-private
and must not be reached across test modules.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace


def capture_otel_provider_state() -> dict[str, Any]:
    """Snapshot the global OTEL provider + once-guard so it can be restored.

    Returns a state dict consumed only by ``restore_otel_provider_state``.
    Captures both the stored provider reference and the ``Once._done``
    flag (the two pieces ``reset_otel_provider`` clobbers), so a test that
    installs its own provider can put the prior global state back exactly
    as it found it and never leak into the rest of the pytest session.
    """
    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    return {
        "provider": getattr(trace, "_TRACER_PROVIDER", None),
        "once": once,
        "once_done": getattr(once, "_done", None) if once is not None else None,
    }


def reset_otel_provider() -> None:
    """Reset the OTEL global provider so set_tracer_provider() is not a no-op.

    OTEL 1.x gates set_tracer_provider behind a Once guard
    (_TRACER_PROVIDER_SET_ONCE). In a test session the first call wins and
    all subsequent calls are silently dropped. We reset both the stored
    provider reference and the Once._done flag before a test that calls
    set_tracer_provider directly. Always pair this with
    ``capture_otel_provider_state`` (before) and
    ``restore_otel_provider_state`` (after, in a ``finally``) so the
    test's own provider does NOT leak into the rest of the suite.
    Private-API access — update if the OTEL SDK moves the guard.
    """
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None:
        once._done = False


def restore_otel_provider_state(state: dict[str, Any]) -> None:
    """Restore the global OTEL provider + once-guard captured earlier.

    The inverse of ``reset_otel_provider`` + ``set_tracer_provider``:
    puts back the prior provider reference and the prior ``Once._done``
    flag so, after the test that installed its own provider, the global
    tracer provider is byte-for-byte what it was before. This is the
    "restore-after" half of the hermetic fix — without it, the test's
    own ``_Capture`` provider leaks and contaminates every later
    ``otel_capture``-using test in the same pytest process.
    """
    trace._TRACER_PROVIDER = state["provider"]  # type: ignore[attr-defined]
    once = state["once"]
    if once is not None and state["once_done"] is not None:
        once._done = state["once_done"]
