"""Story 91-5 — Dark-spend detector: GM dashboard REST endpoint (RED).

The GM dashboard needs a surface for the dark-spend reconciliation result.
Story 91-5 adds two REST endpoints to the server:

  GET  /api/debug/cost/instrumented
       — Returns the current process-level instrumented total from the
         SessionCostLedger. Used by the reconciliation script and the
         GM dashboard's "Layer 1" panel.

  GET  /api/debug/cost/reconciliation
       — Returns the LAST reconciliation result stored by the script
         (or an empty/null object if no reconciliation has run yet).

  POST /api/debug/cost/reconciliation
       — Stores a reconciliation result sent by the script. The script
         calls this after comparing instrumented vs Admin API. The server
         fires ``dark_spend.gap_detected`` if alert=True.

These tests pin the endpoint contracts:

**AC-E1 — GET /api/debug/cost/instrumented returns ledger total.**
The endpoint must reflect the REAL ledger (not a stub), so the script
sees the accurate server-side figure. After recording a session's cost,
the endpoint must return a value > 0.

**AC-E2 — GET /api/debug/cost/reconciliation returns null/empty before
any reconciliation.**
First-run safety: the dashboard must not show stale results from a
prior server run. An empty result is better than a missing endpoint (500).

**AC-E3 — POST /api/debug/cost/reconciliation stores result and is
readable via GET.**
The script writes results here; the dashboard reads them. Round-trip
contract: write → read must preserve instrumented_usd, billed_usd,
gap_pct, and alert.

**AC-E4 — POST result with alert=True fires dark_spend.gap_detected watcher.**
The watcher event is the "loud" part of the "loud alert". It must carry
gap_pct, billed_usd, instrumented_usd, and alert=True in its fields so
the GM panel can display and color it correctly.

**AC-E5 — POST result with alert=False does NOT fire dark_spend.gap_detected.**
The event must not fire on every reconciliation run — only when there IS
a gap. A constant stream of false-alert watcher events degrades the GM
panel's signal.

**AC-E6 — POST schema validation: missing required fields → 422.**
A malformed script payload (missing billed_usd, etc.) must return 422,
not silently store a broken result. (No Silent Fallbacks + input validation
at boundaries, Python rule #11.)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests._helpers.doubles import FakeSocket

# ---------------------------------------------------------------------------
# App fixture — creates a minimal FastAPI test client
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Minimal FastAPI test client wired to the real router."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://localhost/test_notreal")
    from sidequest.server.app import create_app

    app = create_app()
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Watcher hub fixture (canonical pattern from test_91_4)
# ---------------------------------------------------------------------------


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


async def _subscribe(bound_hub: WatcherHub) -> FakeSocket:
    sock = FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    return sock


async def _drain() -> None:
    await asyncio.sleep(0.05)


def _events(sock: FakeSocket, event_type: str) -> list[dict[str, Any]]:
    return [e for e in sock.events if e.get("event_type") == event_type]


# ===========================================================================
# AC-E1 — GET /api/debug/cost/instrumented returns ledger total
# ===========================================================================


def test_get_instrumented_endpoint_exists(client: TestClient) -> None:
    """The endpoint must exist and return 200 with JSON — not 404."""
    resp = client.get("/api/debug/cost/instrumented")
    assert resp.status_code == 200, (
        f"GET /api/debug/cost/instrumented must return 200, got {resp.status_code} "
        f"({resp.text[:200]})"
    )
    data = resp.json()
    assert "instrumented_usd" in data, f"response must contain 'instrumented_usd' key, got: {data}"


def test_get_instrumented_reflects_ledger_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    """After recording spend in the ledger, the endpoint must reflect it.
    This is the wiring test: the endpoint must read the REAL ledger singleton,
    not a stub or always-0 response."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://localhost/test_notreal")
    from sidequest.agents.anthropic_cost import compute_cost_usd
    from sidequest.agents.cost_safety import ledger
    from sidequest.server.app import create_app

    cost_ledger = ledger()
    cost = compute_cost_usd(
        input_tokens=5_000,
        output_tokens=200,
        cached_input_read_tokens=0,
        model="claude-haiku-4-5-20251001",
    )
    with patch("sidequest.agents.cost_safety._watcher_publish_event"):
        cost_ledger.update_cumulative(
            session_id="91-5-endpoint-wire",
            cost_usd=cost,
            model="claude-haiku-4-5-20251001",
            ceiling_usd=1_000.0,
        )

    app = create_app()
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/debug/cost/instrumented")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("instrumented_usd", 0.0) == pytest.approx(cost, abs=1e-9), (
        f"endpoint must reflect the ledger total {cost:.6f}, got {data.get('instrumented_usd')}"
    )


# ===========================================================================
# AC-E2 — GET /api/debug/cost/reconciliation returns null/empty before any run
# ===========================================================================


def test_get_reconciliation_endpoint_exists_before_any_run(
    client: TestClient,
) -> None:
    """GET /api/debug/cost/reconciliation must exist and return 200 even
    before the reconciliation script has ever run. The dashboard must not
    500 on a fresh server start."""
    resp = client.get("/api/debug/cost/reconciliation")
    assert resp.status_code == 200, (
        f"GET /api/debug/cost/reconciliation must return 200 before any run, "
        f"got {resp.status_code} ({resp.text[:200]})"
    )


def test_get_reconciliation_empty_before_any_run(client: TestClient) -> None:
    """Before the script runs, the result should be null or an empty object —
    never a stale result from a prior server run. The endpoint MUST exist (200),
    not 404."""
    resp = client.get("/api/debug/cost/reconciliation")
    assert resp.status_code == 200, (
        f"GET /api/debug/cost/reconciliation must return 200 (not 404), "
        f"got {resp.status_code} — the endpoint must exist before any POST"
    )
    data = resp.json()
    # Accept either None/null or an empty dict/object — both are valid
    # "no data yet" representations. What's NOT acceptable: a 500 or
    # a non-null result that never had a POST.
    assert data is None or data == {} or data.get("billed_usd") is None, (
        f"before any reconciliation POST, the GET must return null or empty — got: {data}"
    )


# ===========================================================================
# AC-E3 — POST stores result and GET returns it (round-trip)
# ===========================================================================


def test_post_reconciliation_roundtrip(client: TestClient) -> None:
    """POST result → GET must return the same result (no field loss)."""
    payload = {
        "instrumented_usd": 0.85,
        "billed_usd": 1.00,
        "gap_pct": 15.0,
        "alert": True,
    }
    post_resp = client.post("/api/debug/cost/reconciliation", json=payload)
    assert post_resp.status_code in (200, 201), (
        f"POST /api/debug/cost/reconciliation must return 200/201, "
        f"got {post_resp.status_code} ({post_resp.text[:200]})"
    )

    get_resp = client.get("/api/debug/cost/reconciliation")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data is not None, "GET after POST must return the stored result"
    assert data.get("instrumented_usd") == pytest.approx(0.85, abs=1e-6), (
        f"instrumented_usd not preserved in round-trip; got {data.get('instrumented_usd')}"
    )
    assert data.get("billed_usd") == pytest.approx(1.00, abs=1e-6)
    assert data.get("gap_pct") == pytest.approx(15.0, abs=0.01)
    assert data.get("alert") is True


# ===========================================================================
# AC-E4 — POST with alert=True fires dark_spend.gap_detected watcher event
# ===========================================================================


@pytest.mark.asyncio
async def test_post_alert_true_fires_watcher_event(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """When the reconciliation script POSTs a result with alert=True, the
    server must fire the dark_spend.gap_detected watcher event carrying
    gap_pct, billed_usd, instrumented_usd, and alert=True.
    This is the 'loud' half of the 'loud alert' requirement."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://localhost/test_notreal")
    from sidequest.server.app import create_app

    sock = await _subscribe(bound_hub)
    app = create_app()
    c = TestClient(app, raise_server_exceptions=True)

    payload = {
        "instrumented_usd": 0.0,
        "billed_usd": 3.50,
        "gap_pct": 100.0,
        "alert": True,
    }
    resp = c.post("/api/debug/cost/reconciliation", json=payload)
    assert resp.status_code in (200, 201), f"POST failed: {resp.status_code} {resp.text[:200]}"
    await _drain()

    events = _events(sock, "dark_spend.gap_detected")
    assert len(events) >= 1, (
        "POST reconciliation with alert=True must fire dark_spend.gap_detected "
        f"watcher event; got zero (all events: {[e.get('event_type') for e in sock.events]})"
    )
    fields = events[0].get("fields", {})
    assert fields.get("alert") is True, f"event.fields.alert must be True; got {fields}"
    assert fields.get("gap_pct") == pytest.approx(100.0, abs=0.01), (
        f"event.fields.gap_pct must be 100.0; got {fields.get('gap_pct')}"
    )
    assert "billed_usd" in fields, "event must carry billed_usd for dashboard display"
    assert "instrumented_usd" in fields, "event must carry instrumented_usd"
    assert events[0].get("severity") in ("warn", "warning", "error"), (
        "dark_spend.gap_detected must be severity warn or error — not info"
    )


# ===========================================================================
# AC-E5 — POST with alert=False does NOT fire dark_spend.gap_detected
# ===========================================================================


@pytest.mark.asyncio
async def test_post_alert_false_no_watcher_event(
    monkeypatch: pytest.MonkeyPatch, bound_hub: WatcherHub
) -> None:
    """A clean reconciliation (alert=False) must not fire dark_spend.gap_detected.
    If every reconciliation run fires the event, the GM panel's signal degrades
    and operators start ignoring it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://localhost/test_notreal")
    from sidequest.server.app import create_app

    sock = await _subscribe(bound_hub)
    app = create_app()
    c = TestClient(app, raise_server_exceptions=True)

    payload = {
        "instrumented_usd": 0.96,
        "billed_usd": 1.00,
        "gap_pct": 4.0,
        "alert": False,
    }
    resp = c.post("/api/debug/cost/reconciliation", json=payload)
    assert resp.status_code in (200, 201)
    await _drain()

    events = _events(sock, "dark_spend.gap_detected")
    assert not events, (
        "POST with alert=False must NOT fire dark_spend.gap_detected — "
        f"got {len(events)} events (fields: {[e.get('fields') for e in events]})"
    )


# ===========================================================================
# AC-E6 — POST schema validation: missing required fields → 422
# ===========================================================================


def test_post_reconciliation_missing_billed_usd_rejects(client: TestClient) -> None:
    """A payload missing billed_usd must return 422 — never silently store
    a partial result that makes gap_pct uncomputable. (Input validation at
    boundaries, Python rule #11.)"""
    bad_payload = {
        "instrumented_usd": 0.85,
        # billed_usd intentionally omitted
        "gap_pct": 15.0,
        "alert": True,
    }
    resp = client.post("/api/debug/cost/reconciliation", json=bad_payload)
    assert resp.status_code == 422, f"missing billed_usd must return 422, got {resp.status_code}"


def test_post_reconciliation_missing_alert_field_rejects(client: TestClient) -> None:
    """alert is a required boolean — a payload without it must be 422.
    A missing alert field makes the server unable to decide whether to
    fire the watcher event."""
    bad_payload = {
        "instrumented_usd": 0.85,
        "billed_usd": 1.00,
        "gap_pct": 15.0,
        # alert omitted
    }
    resp = client.post("/api/debug/cost/reconciliation", json=bad_payload)
    assert resp.status_code == 422, f"missing alert field must return 422, got {resp.status_code}"
