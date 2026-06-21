"""Story 125-9 — ``SIDEQUEST_NO_WATCHER`` / ``--no-watcher`` mode: flag predicate (RED).

The headless playtest/understudy harness drives the operator's live ``:8765`` server,
so every test session registers with the process-global ``watcher_hub`` singleton
(ADR-132) and lingers under ADR-122 never-evict — 41 stale ``test-*`` sessions bury the
genuinely-driven session on the GM dashboard. Story 126-34 shipped the *downstream*
mitigation (filter at the dashboard, tag the broadcast envelope). 125-9 is the
*upstream root fix*: a server flag that keeps test-run activity off the live hub at all
by not wiring the watcher during startup.

This file pins the flag PREDICATE. The startup-wiring behavior (the flag actually
skipping ``bind_loop``) and the opt-in/default-live contract live in
``test_no_watcher_startup_wiring.py``.

Design (Keith, "Both"): the flag is the DEFAULT isolation for most harness runs; OTEL-
asserting runs instead use a real watcher on a separate port (existing infra —
``uvicorn --port`` + ``playtest --server ws://localhost:8766/ws``). So the flag must be
OPT-IN, exact-match, never a fuzzy/global default — No Silent Fallbacks: a typo'd
value must not silently disable the operator's live dashboard.
"""

from __future__ import annotations

import pytest

from sidequest.telemetry import watcher_hub as wh


def test_no_watcher_enabled_true_when_env_is_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_NO_WATCHER", "1")
    assert wh.no_watcher_enabled() is True


def test_no_watcher_enabled_false_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_NO_WATCHER", raising=False)
    assert wh.no_watcher_enabled() is False


@pytest.mark.parametrize("value", ["0", "true", "yes", "", "TRUE", " 1", "01"])
def test_no_watcher_enabled_false_for_non_exact_1(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact ``"1"`` only — no fuzzy truthiness (mirrors ``_watcher_as_spans_enabled``).

    A typo'd ``SIDEQUEST_NO_WATCHER=true`` must NOT silently disable the live
    watcher: it stays ON and the operator keeps their dashboard, rather than a
    silently-deaf hub. Disabling observability is an explicit, classified act.
    """
    monkeypatch.setenv("SIDEQUEST_NO_WATCHER", value)
    assert wh.no_watcher_enabled() is False


def test_no_watcher_enabled_is_read_live_not_cached_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var is read at CALL time, not cached at import.

    ``watcher_hub`` is imported very early in the app graph — sometimes before the
    harness shell exports the flag — so an import-time cache would silently miss it
    (the exact failure mode ``_watcher_as_spans_enabled`` documents). Same process,
    no re-import: flipping the env between calls must change the answer.
    """
    monkeypatch.delenv("SIDEQUEST_NO_WATCHER", raising=False)
    assert wh.no_watcher_enabled() is False
    monkeypatch.setenv("SIDEQUEST_NO_WATCHER", "1")
    assert wh.no_watcher_enabled() is True
