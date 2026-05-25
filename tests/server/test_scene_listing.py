"""RED tests for Story 51-4 — Remove DEV_SCENES gate + GET /dev/scenes listing.

AC-1/AC-2: Scene harness router is ALWAYS registered (no env var gate).
AC-3: New ``GET /dev/scenes`` endpoint returns fixture metadata.
AC-4: ``SIDEQUEST_FIXTURES_DIR`` env var dropped — ``fixtures_dir`` is a
    constructor arg on ``create_app`` with a sensible default.

All tests currently RED:

- AC-1/AC-2 tests fail because ``create_app`` gates the scene harness
  behind ``DEV_SCENES=1``.
- AC-3 tests fail because ``GET /dev/scenes`` is not implemented.
- AC-4 tests fail because ``create_app`` does not accept ``fixtures_dir``.

OTEL: listing endpoint must emit a ``scene_harness.list`` span per the
CLAUDE.md Observability Principle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_FIXTURES_DIR = REPO_ROOT / "scenarios" / "fixtures"


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, dict]]:
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(
        event_type: str,
        fields: dict[str, Any],
        *,
        component: str = "",
        severity: str = "info",
    ) -> None:
        captured.append((event_type, dict(fields), {"component": component, "severity": severity}))

    import sidequest.telemetry.watcher_hub as _hub

    monkeypatch.setattr(_hub, "publish_event", fake_publish)
    return captured


def _build_dev_scenes_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    save_dir: Path,
    fixtures_dir: Path = CANONICAL_FIXTURES_DIR,
):
    """Construct the app the OLD way (DEV_SCENES=1) — used only for AC-3
    listing tests where the route must be registered to test the endpoint."""
    monkeypatch.setenv("DEV_SCENES", "1")
    monkeypatch.setenv("SIDEQUEST_FIXTURES_DIR", str(fixtures_dir))

    from sidequest.server.app import create_app

    return create_app(
        save_dir=save_dir,
        genre_pack_search_paths=[],
    )


# ── AC-1/AC-2: Scene harness always registered ───────────────────────────────


def test_scene_harness_route_registered_without_dev_scenes_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Story 51-4 AC-1/AC-2: the scene harness route MUST be registered
    even when ``DEV_SCENES`` is not set. Cloudflare Zero Trust gates
    access; the env var adds zero security value."""
    monkeypatch.delenv("DEV_SCENES", raising=False)
    monkeypatch.delenv("SIDEQUEST_FIXTURES_DIR", raising=False)

    from sidequest.server.app import create_app

    app = create_app(save_dir=tmp_path, genre_pack_search_paths=[])

    paths = {getattr(r, "path", "") for r in app.routes}
    scene_routes = [p for p in paths if "/dev/scene" in p]
    assert scene_routes, (
        f"Scene harness route must be registered WITHOUT DEV_SCENES env var "
        f"(Story 51-4 AC-1/AC-2). Routes: {sorted(paths)!r}"
    )


def test_scene_harness_post_works_without_dev_scenes_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Wiring test: ``POST /dev/scene/{name}`` succeeds without ``DEV_SCENES=1``.

    Uses canonical fixtures dir — if the route is registered but hydration
    fails, the wiring test catches the difference between "registered" and
    "functional"."""
    monkeypatch.delenv("DEV_SCENES", raising=False)
    monkeypatch.setenv("SIDEQUEST_FIXTURES_DIR", str(CANONICAL_FIXTURES_DIR))

    from sidequest.server.app import create_app

    app = create_app(save_dir=tmp_path, genre_pack_search_paths=[])
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200, (
        f"POST /dev/scene/combat_brawl_wasteland must work without DEV_SCENES; "
        f"got {r.status_code}, body: {r.text}"
    )


# ── AC-3: GET /dev/scenes listing endpoint ────────────────────────────────────


def test_listing_endpoint_returns_200(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Story 51-4 AC-3: ``GET /dev/scenes`` must return 200 with a JSON list."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200, (
        f"GET /dev/scenes must return 200; got {r.status_code}, body: {r.text}"
    )
    body = r.json()
    assert isinstance(body, list), (
        f"GET /dev/scenes must return a JSON list; got {type(body).__name__}: {body!r}"
    )


def test_listing_returns_fixture_metadata_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Story 51-4 AC-3: each item in the listing carries
    ``name``, ``genre``, ``world``, and ``description``."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "test_fixture.yaml").write_text(
        "name: Test Fixture\ngenre: test_genre\nworld: test_world\n"
        "description: A test fixture for listing\n",
        encoding="utf-8",
    )

    app = _build_dev_scenes_app(
        monkeypatch, save_dir=tmp_path, fixtures_dir=fixtures_dir
    )
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1, f"expected 1 fixture; got {len(body)}: {body!r}"

    item = body[0]
    assert item["name"] == "test_fixture", (
        f"name must be the fixture file stem; got {item!r}"
    )
    assert item["genre"] == "test_genre", f"genre field missing or wrong; got {item!r}"
    assert item["world"] == "test_world", f"world field missing or wrong; got {item!r}"
    assert item["description"] == "A test fixture for listing", (
        f"description field missing or wrong; got {item!r}"
    )


def test_listing_includes_fixtures_without_description(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fixtures without a ``description`` field still appear in the listing
    with ``description=null`` — the UI renders these as cards without a subtitle."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "no_desc.yaml").write_text(
        "name: No Description\ngenre: caverns_and_claudes\nworld: default\n",
        encoding="utf-8",
    )

    app = _build_dev_scenes_app(
        monkeypatch, save_dir=tmp_path, fixtures_dir=fixtures_dir
    )
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["description"] is None, (
        f"missing description should be null; got {body[0].get('description')!r}"
    )


def test_listing_scans_all_valid_yaml_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The listing scans all ``*.yaml`` files and returns one entry per valid fixture."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "alpha.yaml").write_text(
        "name: Alpha\ngenre: g1\nworld: w1\n", encoding="utf-8"
    )
    (fixtures_dir / "beta.yaml").write_text(
        "name: Beta\ngenre: g2\nworld: w2\n", encoding="utf-8"
    )
    (fixtures_dir / "readme.txt").write_text("not a fixture", encoding="utf-8")

    app = _build_dev_scenes_app(
        monkeypatch, save_dir=tmp_path, fixtures_dir=fixtures_dir
    )
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200
    names = sorted(item["name"] for item in r.json())
    assert names == ["alpha", "beta"], f"expected 2 YAML fixtures; got {names!r}"


def test_listing_excludes_invalid_yaml_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fixture files with invalid/incomplete YAML (missing required fields)
    are excluded from the listing rather than crashing the endpoint."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "good.yaml").write_text(
        "name: Good\ngenre: g1\nworld: w1\n", encoding="utf-8"
    )
    (fixtures_dir / "bad.yaml").write_text(
        "this is not valid fixture yaml\n", encoding="utf-8"
    )

    app = _build_dev_scenes_app(
        monkeypatch, save_dir=tmp_path, fixtures_dir=fixtures_dir
    )
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200
    names = [item["name"] for item in r.json()]
    assert names == ["good"], f"invalid YAML should be excluded; got {names!r}"


def test_listing_returns_empty_list_when_no_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty fixtures directory returns ``[]``, not 404 or 500."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()

    app = _build_dev_scenes_app(
        monkeypatch, save_dir=tmp_path, fixtures_dir=fixtures_dir
    )
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200
    assert r.json() == [], f"empty fixtures dir should return []; got {r.json()!r}"


def test_listing_validates_fixture_names_with_regex(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fixture filenames that fail ``_FIXTURE_NAME_RE`` are excluded from
    the listing (same validation as ``POST /dev/scene/{name}``)."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "valid_name.yaml").write_text(
        "name: Valid\ngenre: g1\nworld: w1\n", encoding="utf-8"
    )
    (fixtures_dir / "has spaces.yaml").write_text(
        "name: Spaced\ngenre: g1\nworld: w1\n", encoding="utf-8"
    )

    app = _build_dev_scenes_app(
        monkeypatch, save_dir=tmp_path, fixtures_dir=fixtures_dir
    )
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200
    names = [item["name"] for item in r.json()]
    assert "valid_name" in names
    assert not any(" " in n for n in names), (
        f"fixtures with names failing _FIXTURE_NAME_RE should be excluded; got {names!r}"
    )


def test_listing_emits_otel_span(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OTEL observability: ``GET /dev/scenes`` emits a ``scene_harness.list``
    span so the GM panel can see scene-library usage."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "otel_probe.yaml").write_text(
        "name: OTEL Probe\ngenre: g1\nworld: w1\n", encoding="utf-8"
    )

    captured = _capture_events(monkeypatch)
    app = _build_dev_scenes_app(
        monkeypatch, save_dir=tmp_path, fixtures_dir=fixtures_dir
    )
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200

    list_events = [e for e in captured if "scene_harness" in e[0] and "list" in e[0]]
    assert list_events, (
        f"GET /dev/scenes must emit a scene_harness.list span; "
        f"captured event types: {sorted({e[0] for e in captured})!r}"
    )
    fields = list_events[0][1]
    assert "fixture_count" in fields, (
        f"scene_harness.list span must report fixture_count; got fields={fields!r}"
    )


# ── AC-4: fixtures_dir as constructor arg ─────────────────────────────────────


def test_create_app_accepts_fixtures_dir_kwarg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Story 51-4 AC-4: ``create_app()`` accepts a ``fixtures_dir`` constructor
    arg, replacing the ``SIDEQUEST_FIXTURES_DIR`` env var indirection."""
    monkeypatch.delenv("DEV_SCENES", raising=False)
    monkeypatch.delenv("SIDEQUEST_FIXTURES_DIR", raising=False)

    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()

    from sidequest.server.app import create_app

    app = create_app(
        save_dir=tmp_path,
        genre_pack_search_paths=[],
        fixtures_dir=fixtures_dir,
    )

    assert hasattr(app.state, "fixtures_dir"), (
        "app.state.fixtures_dir must be set when passed as constructor arg"
    )
    assert app.state.fixtures_dir == fixtures_dir, (
        f"app.state.fixtures_dir must match the constructor arg; "
        f"got {app.state.fixtures_dir!r}"
    )


def test_create_app_defaults_fixtures_dir_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When ``fixtures_dir`` is not passed, ``create_app`` resolves it to
    ``scenarios/fixtures`` relative to cwd (the orchestrator root)."""
    monkeypatch.delenv("DEV_SCENES", raising=False)
    monkeypatch.delenv("SIDEQUEST_FIXTURES_DIR", raising=False)

    from sidequest.server.app import create_app

    app = create_app(
        save_dir=tmp_path,
        genre_pack_search_paths=[],
    )

    assert hasattr(app.state, "fixtures_dir"), (
        "app.state.fixtures_dir must be set even without fixtures_dir kwarg or env var"
    )
    assert isinstance(app.state.fixtures_dir, Path), (
        f"fixtures_dir must be a Path; got {type(app.state.fixtures_dir).__name__}"
    )


# ── Wiring: listing endpoint uses canonical fixtures on real filesystem ───────


def test_listing_returns_canonical_fixtures_from_real_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Integration test: listing endpoint against the real ``scenarios/fixtures``
    directory returns at least the known canonical fixtures.

    This is the CLAUDE.md wiring test — it proves ``GET /dev/scenes`` is
    reachable through the production ``create_app()`` factory and actually
    scans the filesystem, not a hardcoded response."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.get("/dev/scenes")
    assert r.status_code == 200
    body = r.json()
    names = {item["name"] for item in body}

    expected = {"combat_brawl_wasteland", "social_poker_wasteland"}
    missing = expected - names
    assert not missing, (
        f"canonical fixtures missing from listing: {missing!r}; "
        f"returned names: {sorted(names)!r}"
    )
