# sidequest-server/tests/server/test_games_mp_join_existing.py
"""POST /api/games — MP-mode join-existing semantics.

Playtest 2026-04-26 S4-UX bug: Player 2 went through the lobby with
identical (genre, world, mode=multiplayer) selections as Player 1 and
ended up routed to ``<slug>-2`` instead of joining Player 1's session.

Root cause: the lobby UI sets ``force_new=True`` whenever the typed
display name does not match any **local-browser** Past Journey for
(genre, world, mode). On a second host the local journey list is empty,
so the request always carries ``force_new=True`` — and the server then
faithfully disambiguated, splitting the table.

Server fix shape (load-bearing for tonight): in ``mode=multiplayer``,
when an existing same-slug game exists and is itself a multiplayer game,
return the existing slug regardless of ``force_new``. Solo behavior is
unchanged — solo journeys are per-player, so ``force_new=True`` still
mints a disambiguated solo slug.

Watcher events:
  * ``lobby.session_join_existing`` — emitted when MP join short-circuit
    fires. Lets the GM panel see "P2 joined P1's table" instead of
    silently routing.
  * ``lobby.force_new_disambiguated`` (existing) — only emitted when the
    disambiguator actually allocates a new slug. Must NOT fire on the
    MP-join short-circuit path.
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

    Same pattern as test_games_force_new.py — span helpers close over the
    global provider, so the only reliable observation is to add a
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


def _post_mp(client: TestClient, **extra) -> dict:
    body = {
        "genre_slug": "mutant_wasteland",
        "world_slug": "flickering_reach",
        "mode": "multiplayer",
    }
    body.update(extra)
    return client.post("/api/games", json=body)


def _post_solo(client: TestClient, **extra) -> dict:
    body = {
        "genre_slug": "low_fantasy",
        "world_slug": "moldharrow-keep",
        "mode": "solo",
    }
    body.update(extra)
    return client.post("/api/games", json=body)


# ---------------------------------------------------------------------------
# Core MP join-existing behavior
# ---------------------------------------------------------------------------


def test_mp_force_new_with_existing_mp_game_joins_existing(client: TestClient):
    """The S4-UX bug, frozen as a regression test.

    P1 starts an MP game; P2 (different host, no local history) hits the
    lobby and the UI sends force_new=True because the typed name has no
    Past Journey match locally. The server MUST NOT mint ``-2`` — it
    must return the existing same-slug game so P2 joins P1's table.
    """
    # P1: starts MP game.
    p1 = _post_mp(client, player_name="Paul")
    assert p1.status_code == 201
    assert p1.json()["slug"] == "2026-04-26-flickering_reach-mp"

    # P2: identical lobby selections, force_new=True (no local history).
    p2 = _post_mp(client, player_name="John", force_new=True)
    assert p2.status_code == 200, p2.text
    body = p2.json()
    assert body["slug"] == "2026-04-26-flickering_reach-mp", (
        f"P2 should JOIN P1's MP game, got fresh slug {body['slug']!r}"
    )
    assert body["resumed"] is True
    assert body["mode"] == "multiplayer"
    # Echo through P2's typed name so the lobby can render it.
    assert body["player_name"] == "John"


def test_mp_no_force_new_with_existing_mp_game_joins_existing(client: TestClient):
    """force_new=False (or omitted) on an existing MP slug already
    resumed before the fix — guard the regression against silent change.
    """
    assert _post_mp(client, player_name="Paul").status_code == 201
    p2 = _post_mp(client, player_name="John")  # no force_new
    assert p2.status_code == 200
    assert p2.json()["slug"] == "2026-04-26-flickering_reach-mp"
    assert p2.json()["resumed"] is True


def test_mp_join_existing_does_not_emit_force_new_disambiguation_span(
    client: TestClient, otel_capture: InMemorySpanExporter
):
    """No silent fallbacks: when the MP-join short-circuit returns the
    existing slug, the disambiguation span must NOT fire — that span is
    reserved for actual ``-N`` allocations.
    """
    assert _post_mp(client, player_name="Paul").status_code == 201

    otel_capture.clear()
    p2 = _post_mp(client, player_name="John", force_new=True)
    assert p2.status_code == 200

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "lobby.force_new_disambiguated" not in span_names


def test_mp_join_existing_emits_session_join_existing_span(
    client: TestClient, otel_capture: InMemorySpanExporter
):
    """OTEL observability principle (CLAUDE.md): the GM panel must be
    able to see when the MP-join short-circuit fired. Without a typed
    span the GM cannot tell whether the server joined P2 to P1's table
    intentionally vs. a coincidence of routing.
    """
    assert _post_mp(client, player_name="Paul").status_code == 201

    otel_capture.clear()
    p2 = _post_mp(client, player_name="John", force_new=True)
    assert p2.status_code == 200

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "lobby.session_join_existing" in span_names

    span = next(
        s for s in otel_capture.get_finished_spans() if s.name == "lobby.session_join_existing"
    )
    assert span.attributes["slug"] == "2026-04-26-flickering_reach-mp"
    assert span.attributes["player_name"] == "John"
    assert span.attributes["mode"] == "multiplayer"
    assert span.attributes["force_new_requested"] is True


# ---------------------------------------------------------------------------
# Solo behavior is preserved (regression guard against over-broad fix)
# ---------------------------------------------------------------------------


def test_solo_force_new_with_existing_solo_game_still_disambiguates(
    client: TestClient,
):
    """Solo behavior is unchanged: ``force_new=True`` on a solo collision
    still mints a disambiguated slug. The MP-join short-circuit must not
    leak into solo mode — solo journeys are per-player.
    """
    assert _post_solo(client, player_name="Laverne").status_code == 201
    second = _post_solo(client, player_name="Lenny", force_new=True)
    assert second.status_code == 201
    assert second.json()["slug"] == "2026-04-26-moldharrow-keep-2"
    assert second.json()["resumed"] is False


def test_solo_force_new_into_existing_mp_slug_disambiguates(client: TestClient):
    """Cross-mode collision: starting a solo game for a world where an MP
    game already exists for today must NOT join the MP table — solo is a
    different mode and must mint its own slug.

    (In practice the slug derivation includes the mode suffix so this
    won't actually collide, but the test guards the join short-circuit
    against accidentally firing across modes.)
    """
    assert _post_mp(client, player_name="Paul").status_code == 201
    solo = client.post(
        "/api/games",
        json={
            "genre_slug": "mutant_wasteland",
            "world_slug": "flickering_reach",
            "mode": "solo",
            "player_name": "Solo Sam",
            "force_new": True,
        },
    )
    # Either status is acceptable — what matters is the slug is not the
    # MP slug. Solo's own slug derivation already includes a different
    # suffix, so no -N is needed.
    assert solo.status_code in (200, 201)
    assert solo.json()["slug"] != "2026-04-26-flickering_reach-mp"
    assert solo.json()["mode"] == "solo"


# ---------------------------------------------------------------------------
# Backward-compat
# ---------------------------------------------------------------------------


def test_mp_first_call_creates_game_normally(client: TestClient):
    """The first MP request (no existing game) creates and returns 201
    with the base slug — short-circuit must not affect cold-start.
    """
    r = _post_mp(client, player_name="Paul", force_new=True)
    assert r.status_code == 201
    assert r.json()["slug"] == "2026-04-26-flickering_reach-mp"
    assert r.json()["resumed"] is False


def test_mp_existing_solo_game_at_same_slug_does_not_join(
    client: TestClient, otel_capture: InMemorySpanExporter
):
    """Defensive: if somehow an existing same-slug game is solo-mode and
    an MP request lands on it with ``force_new=True``, the MP-join
    short-circuit must NOT fire — joining a solo game with MP semantics
    would corrupt the seat model. The disambiguator should mint a fresh
    slug instead.

    (In practice the slug suffix derivation includes the mode, so this
    cross-mode collision is rare; the guard is cheap and explicit so
    the join short-circuit cannot leak across modes.)
    """
    # Manually craft a slug collision by writing a solo game to the MP
    # base slug — bypasses the mode-suffix derivation. Uses the same
    # store helpers the route uses.
    from sidequest.game.persistence import (
        GameMode,
        SqliteStore,
        db_path_for_slug,
        upsert_game,
    )

    save_dir: Path = client.app.state.save_dir
    rogue_slug = "2026-04-26-flickering_reach-mp"
    db = db_path_for_slug(save_dir, rogue_slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=rogue_slug,
        mode=GameMode.SOLO,
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
    )

    otel_capture.clear()
    r = _post_mp(client, player_name="Paul", force_new=True)
    # Disambiguator allocates a fresh slug because the existing row is
    # solo (not MP) — short-circuit must not fire across modes.
    assert r.status_code == 201
    assert r.json()["slug"] == "2026-04-26-flickering_reach-mp-2"
    assert r.json()["mode"] == "multiplayer"

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    # Cross-mode disambiguation: force_new event fires, join event must not.
    assert "lobby.force_new_disambiguated" in span_names
    assert "lobby.session_join_existing" not in span_names
