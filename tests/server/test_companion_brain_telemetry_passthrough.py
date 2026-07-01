"""RED (161-2, part c): the server accepts an explicit session_slug + severity.

The understudy companion runs in its OWN process; it cannot import the watcher hub,
so it POSTs each ``companion_brain_decide`` event to ``/internal/watcher/emit`` (the
same bridge the daemon uses). Two gaps to close:

  1. ``publish_event`` derives session_slug from the ContextVar — correct for the
     in-process narrator, but the companion's slug arrives over HTTP. Add an explicit
     ``session_slug`` override param (override beats the ContextVar; ``None`` keeps
     every existing in-process caller and the daemon unchanged).
  2. ``WatcherEmitPayload`` / the endpoint carry only event_type/fields/component.
     Add optional ``session_slug`` + ``severity`` and forward them.

These are behavior assertions on the hub (project rule: No Source-Text Wiring Tests):
spy on ``watcher_hub.publish``, drive the real path, assert the event that lands.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from sidequest.server.app import create_app
from sidequest.telemetry import watcher_hub as hub_mod


def _spy_hub(monkeypatch) -> list[dict]:
    """Capture every event that reaches the hub, without a bound event loop/DB."""
    captured: list[dict] = []
    monkeypatch.setattr(hub_mod.watcher_hub, "publish", captured.append)
    return captured


# --- publish_event: the session_slug override (part c core) --------------------


def test_publish_event_session_slug_override_beats_contextvar(monkeypatch):
    # RED today: publish_event has no session_slug parameter -> TypeError.
    captured = _spy_hub(monkeypatch)
    hub_mod.bind_session_slug("ctx-slug")
    try:
        hub_mod.publish_event(
            event_type="companion_brain_decide",
            fields={"seat": "Donut", "role": "pet"},
            component="companion_brain",
            session_slug="override-slug",
        )
    finally:
        hub_mod.bind_session_slug(None)
    assert captured, "publish_event must reach the hub"
    assert captured[0]["session_slug"] == "override-slug", (
        "an explicit session_slug must beat the ContextVar for cross-process emits"
    )


def test_publish_event_without_override_uses_contextvar(monkeypatch):
    # NON-REGRESSION GUARD (must stay green): with no override, publish_event keeps
    # deriving the slug from the ContextVar and defaulting severity to "info" — the
    # existing in-process + daemon behavior is unchanged.
    captured = _spy_hub(monkeypatch)
    hub_mod.bind_session_slug("ctx-slug")
    try:
        hub_mod.publish_event(event_type="x", fields={}, component="daemon")
    finally:
        hub_mod.bind_session_slug(None)
    assert captured[0]["session_slug"] == "ctx-slug"
    assert captured[0]["severity"] == "info"


# --- /internal/watcher/emit: forward session_slug + severity to the hub ---------


def test_emit_endpoint_forwards_session_slug_and_severity_to_hub(monkeypatch):
    # RED today: WatcherEmitPayload ignores session_slug/severity, so the hub event
    # carries session_slug=None (no ContextVar) and severity="info". This is the
    # server end of the AC #6 wiring: a companion_brain_decide lands on the hub with
    # the correct session_slug + seat + role.
    captured = _spy_hub(monkeypatch)
    hub_mod.bind_session_slug(None)  # no ambient slug — prove it comes from the payload
    app = create_app()
    client = TestClient(app)
    try:
        resp = client.post(
            "/internal/watcher/emit",
            json={
                "event_type": "companion_brain_decide",
                "fields": {"seat": "Donut", "role": "pet"},
                "component": "companion_brain",
                "session_slug": "game-1",
                "severity": "warning",
            },
        )
    finally:
        hub_mod.bind_session_slug(None)
    assert resp.status_code == 204
    assert captured, "the emit endpoint must reach the hub"
    ev = captured[0]
    assert ev["event_type"] == "companion_brain_decide"
    assert ev["session_slug"] == "game-1"
    assert ev["severity"] == "warning"
    assert ev["fields"]["seat"] == "Donut"
    assert ev["fields"]["role"] == "pet"


def test_emit_endpoint_daemon_path_unchanged(monkeypatch):
    # NON-REGRESSION GUARD (must stay green): a daemon-style POST (no session_slug /
    # severity) still publishes at default severity with the ContextVar slug — the
    # existing bridge is unaffected by the new optional fields.
    captured = _spy_hub(monkeypatch)
    hub_mod.bind_session_slug("live-session")
    app = create_app()
    client = TestClient(app)
    try:
        resp = client.post(
            "/internal/watcher/emit",
            json={"event_type": "render.done", "fields": {"k": "v"}, "component": "daemon"},
        )
    finally:
        hub_mod.bind_session_slug(None)
    assert resp.status_code == 204
    ev = captured[0]
    assert ev["event_type"] == "render.done"
    assert ev["severity"] == "info"
    assert ev["session_slug"] == "live-session"
