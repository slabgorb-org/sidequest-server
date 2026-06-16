# sidequest-server/tests/server/test_games_force_new.py
"""POST /api/games — force_new + player_name companion to UI develop 1436ebd.

The lobby now sends ``force_new=True`` when the typed name does not match
any past journey for (genre, world, mode); without these tests the server
silently returned the colliding slug and the lobby resumed the prior
character. Each behavior in :func:`create_or_resume_game` that the UI
fix relies on has a check below.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.server.rest import create_rest_router
from sidequest.telemetry.setup import init_tracer


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.save_dir = tmp_path
    app.state.genre_pack_search_paths = []
    app.state.today_fn = lambda: date(2026, 4, 26)
    app.include_router(create_rest_router())
    return TestClient(app)


@pytest.fixture
def otel_capture():
    """Install an in-memory exporter on the live tracer provider.

    Matches the pattern in tests/agents/conftest.py — span helpers close
    over the global provider, so the only reliable observation is to add a
    SimpleSpanProcessor on the singleton.
    """
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _post(client: TestClient, **extra) -> dict:
    body = {
        "genre_slug": "low_fantasy",
        "world_slug": "moldharrow-keep",
        "mode": "solo",
    }
    body.update(extra)
    return client.post("/api/games", json=body)


def test_player_name_echoes_back_on_create(client: TestClient):
    r = _post(client, player_name="Lenny")
    assert r.status_code == 201
    assert r.json()["player_name"] == "Lenny"


def test_player_name_echoes_back_on_resume(client: TestClient):
    first = _post(client, player_name="Laverne")
    assert first.status_code == 201
    second = _post(client, player_name="Laverne")
    assert second.status_code == 200
    assert second.json()["resumed"] is True
    assert second.json()["player_name"] == "Laverne"


def test_force_new_with_no_collision_keeps_base_slug(client: TestClient):
    r = _post(client, player_name="Lenny", force_new=True)
    assert r.status_code == 201
    assert r.json()["slug"] == "2026-04-26-moldharrow-keep"
    assert r.json()["resumed"] is False


def test_force_new_with_collision_appends_disambiguator(client: TestClient):
    """Pre-fix bug: lobby typed 'Lenny' but landed in 'Laverne' because
    the same-day base slug already existed and the server resumed it.
    With force_new=True the server must mint a fresh slug instead.
    """
    first = _post(client, player_name="Laverne")
    assert first.status_code == 201
    assert first.json()["slug"] == "2026-04-26-moldharrow-keep"

    second = _post(client, player_name="Lenny", force_new=True)
    assert second.status_code == 201, second.text
    assert second.json()["slug"] == "2026-04-26-moldharrow-keep-2"
    assert second.json()["resumed"] is False
    assert second.json()["player_name"] == "Lenny"


def test_force_new_walks_past_multiple_collisions(client: TestClient):
    assert _post(client, player_name="A").status_code == 201
    assert _post(client, player_name="B", force_new=True).json()["slug"].endswith("-2")
    assert _post(client, player_name="C", force_new=True).json()["slug"].endswith("-3")
    fourth = _post(client, player_name="D", force_new=True)
    assert fourth.json()["slug"] == "2026-04-26-moldharrow-keep-4"


def test_force_new_disambiguation_emits_watcher_span(
    client: TestClient, otel_capture: InMemorySpanExporter
):
    """OTEL observability principle (CLAUDE.md): every fix must emit a
    span the GM panel can watch. The lobby.force_new_disambiguated event
    is the only signal that the rename actually happened.
    """
    assert _post(client, player_name="Laverne").status_code == 201

    otel_capture.clear()
    second = _post(client, player_name="Lenny", force_new=True)
    assert second.status_code == 201

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "lobby.force_new_disambiguated" in span_names

    span = next(
        s for s in otel_capture.get_finished_spans() if s.name == "lobby.force_new_disambiguated"
    )
    assert span.attributes["requested_slug"] == "2026-04-26-moldharrow-keep"
    assert span.attributes["final_slug"] == "2026-04-26-moldharrow-keep-2"
    assert span.attributes["attempts"] == 2
    assert span.attributes["player_name"] == "Lenny"


def test_force_new_without_collision_does_not_emit_disambiguation_span(
    client: TestClient, otel_capture: InMemorySpanExporter
):
    """No silent fallbacks (CLAUDE.md): the disambiguation event must NOT
    fire when force_new is honored trivially (no collision to resolve)."""
    otel_capture.clear()
    r = _post(client, player_name="Lenny", force_new=True)
    assert r.status_code == 201

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "lobby.force_new_disambiguated" not in span_names


def test_force_new_false_still_resumes_collision(client: TestClient):
    """Backward compat: omitting force_new (or sending False) preserves
    the original resume-on-collision behavior — the older UI relies on it.
    """
    first = _post(client, player_name="Laverne")
    assert first.status_code == 201

    second = _post(client, player_name="Lenny")  # no force_new
    assert second.status_code == 200
    assert second.json()["resumed"] is True
    assert second.json()["slug"] == "2026-04-26-moldharrow-keep"


def test_endpoint_accepts_request_without_new_fields(client: TestClient):
    """Curl-based smoke tests and older UI builds don't send the new
    fields; the endpoint must not start rejecting them with 422.
    """
    r = client.post(
        "/api/games",
        json={
            "genre_slug": "low_fantasy",
            "world_slug": "moldharrow-keep",
            "mode": "solo",
        },
    )
    assert r.status_code == 201
    assert r.json()["player_name"] is None
