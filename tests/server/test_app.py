"""Tests for ``create_app`` — confirms the default client factory honours
``SIDEQUEST_LLM_BACKEND`` end-to-end (ADR-073 Phase 2, Local DM Group E T10).

``create_app()`` stores the resolved factory on ``app.state.claude_client_factory``
(each ``/ws`` connection constructs a ``WebSocketSessionHandler`` with this
factory). When no explicit factory is injected, the default must be
``build_llm_client`` so the backend env var swaps Claude → Ollama on server
start without any code changes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sidequest.server.app import create_app


def test_create_app_uses_build_llm_client_by_default(monkeypatch):
    """Phase D: default backend flipped from claude to anthropic_sdk.

    The factory still resolves via build_llm_client; only the default
    backend identity changes. ClaudeClient remains reachable via
    SIDEQUEST_LLM_BACKEND=claude (covered by
    test_explicit_claude_backend_still_resolves in test_llm_factory.py).
    """
    monkeypatch.delenv("SIDEQUEST_LLM_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # The factory-resolution assertion needs an AnthropicSdkClient instance,
    # not a live SDK: shadow the 93-1 hermeticity guard (which raises) with
    # an inert object so construction succeeds without touching the real
    # AsyncAnthropic (the client resolves the SDK late-bound through
    # ``llm_factory.build_async_anthropic`` — the single construction site).
    monkeypatch.setattr(
        "sidequest.agents.llm_factory.build_async_anthropic",
        lambda: object(),
    )
    app = create_app()
    from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient

    client = app.state.claude_client_factory()
    assert isinstance(client, AnthropicSdkClient)


def test_create_app_rejects_retired_ollama_env(monkeypatch):
    """Story 61-9 / ADR-101 amendment: ``ollama`` is retired for any
    purpose. The lazy ``claude_client_factory`` raises at first call
    when the env selects a retired backend — fail-loud at the config
    boundary, no silent fallback to a working narrator."""
    from sidequest.agents.llm_factory import NarratorBackendRetired

    monkeypatch.setenv("SIDEQUEST_LLM_BACKEND", "ollama")
    app = create_app()
    with pytest.raises(NarratorBackendRetired):
        app.state.claude_client_factory()


def test_create_app_discovers_render_root_via_daemon_handshake(
    monkeypatch,
    tmp_path,
):
    """Regression: when SIDEQUEST_OUTPUT_DIR isn't set, create_app() must
    fall back to ~/.sidequest/daemon-output-dir (the handshake file the
    daemon writes at startup). Without this discovery the dev-default
    flow leaves /renders unmounted and every image 404s in the UI
    (playtest 2026-04-25 [P1] regression).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    handshake_dir = fake_home / ".sidequest"
    handshake_dir.mkdir()
    daemon_output = tmp_path / "daemon-tmp" / "zimage"
    daemon_output.mkdir(parents=True)
    (handshake_dir / "daemon-output-dir").write_text(f"{daemon_output}\n")

    monkeypatch.delenv("SIDEQUEST_OUTPUT_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    create_app()

    # create_app must have promoted the handshake value into the env so
    # downstream `_render_url_from_path` calls see the same root.
    import os as _os

    assert _os.environ.get("SIDEQUEST_OUTPUT_DIR") == str(daemon_output)


def test_render_url_from_path_publishes_image_unavailable_on_fallthrough(
    monkeypatch,
):
    """Regression: when the render path can't be rewritten (env unset, or
    path lives outside SIDEQUEST_OUTPUT_DIR), `_render_url_from_path`
    must emit an `image_unavailable` watcher event so the GM panel
    surfaces the silent fallthrough. CLAUDE.md OTEL principle.
    """
    monkeypatch.delenv("SIDEQUEST_OUTPUT_DIR", raising=False)

    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        captured.append((event_type, fields, {"component": component, "severity": severity}))

    import sidequest.telemetry.watcher_hub as _hub

    monkeypatch.setattr(_hub, "publish_event", fake_publish)

    from sidequest.server.session_helpers import _render_url_from_path

    result = _render_url_from_path("/var/folders/h0/sq-daemon-xyz/zimage/render_abc.png")

    # Path returned verbatim (no rewrite available)
    assert result == "/var/folders/h0/sq-daemon-xyz/zimage/render_abc.png"
    # And the lie detector lit up
    unavailable = [(fields, meta) for et, fields, meta in captured if et == "image_unavailable"]
    assert unavailable, "expected image_unavailable watcher event on env-unset fallthrough"
    fields, meta = unavailable[0]
    assert fields["reason"] == "output_dir_unset"
    assert meta["component"] == "render"
    assert meta["severity"] == "warning"


def test_validator_starts_with_app() -> None:
    """create_app() registers a startup hook that boots the validator."""
    from fastapi.testclient import TestClient

    from sidequest.server.app import create_app

    app = create_app()
    with TestClient(app):
        validator = getattr(app.state, "validator", None)
        assert validator is not None, "app.state.validator should be populated at startup"
        assert validator.is_running()
    # On exit, the TestClient's shutdown lifespan triggers shutdown.
    assert not validator.is_running()


def test_db_pool_opens_and_closes_with_app() -> None:
    """ADR-115 F2: create_app() registers startup/shutdown hooks that open the
    Postgres pool at boot (fail-loud connectivity — the pool is ready before the
    first request instead of lazily on first use) and close/discard it on
    shutdown. Without this the pool opens lazily and a dead PG only surfaces on
    the first save mid-session, and the pool is never cleanly drained.
    """
    from sidequest.game import db_pool

    db_pool.close_pool()  # deterministic start: no live pool
    app = create_app()
    with TestClient(app):
        assert db_pool._POOL is not None, (
            "startup hook did not open the PG pool — _open_db_pool wiring in app.py did not run"
        )
        assert not db_pool._POOL.closed, "pool should be open after startup"
    # On TestClient exit the shutdown hook runs — pool must be closed + discarded.
    assert db_pool._POOL is None, (
        "shutdown hook did not close/discard the PG pool — _close_db_pool "
        "wiring in app.py did not run"
    )


def test_heartbeat_listener_starts_with_app() -> None:
    """Story 45-31 wiring guard (review M6): create_app() registers a
    startup hook that boots ``DaemonClient.heartbeat_listener`` as a
    background task on ``app.state.heartbeat_listener_task``. Without
    this wiring the daemon-state mirror is never populated, the
    dispatcher's UNRESPONSIVE branch is unreachable, and the Felix
    anti-13-minute-silence contract never engages.

    Verifies (a) the task exists on app.state after startup, (b) it's
    actually running (not done immediately), (c) the shutdown hook
    cancels it cleanly."""
    app = create_app()
    with TestClient(app):
        task = getattr(app.state, "heartbeat_listener_task", None)
        assert task is not None, (
            "app.state.heartbeat_listener_task missing after startup — "
            "the heartbeat_listener wiring in app.py did not run"
        )
        assert not task.done(), (
            "heartbeat_listener_task finished immediately after startup — "
            "the listener body must be a long-running loop, not a "
            "single-shot coroutine"
        )
    # On TestClient exit, the shutdown hook runs — task must be cancelled.
    assert task.done(), (
        "heartbeat_listener_task still alive after app shutdown — "
        "_stop_heartbeat_listener did not cancel it"
    )
