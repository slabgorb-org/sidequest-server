"""Unit tests for sidequest.server.rest endpoints.

Tests /api/genres, /api/sessions, and /api/debug/state.

No real genre pack files needed — tests use tmp_path fixtures and minimal
YAML stubs.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from sidequest.server.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_mock_genre_pack(
    packs_dir: Path,
    genre_slug: str,
    world_slug: str,
    cover_poi: str | None = None,
    *,
    cartography: bool | str = False,
) -> None:
    """Write minimal pack.yaml + world/world.yaml under packs_dir.

    ``cartography`` controls the sibling cartography.yaml that the lobby
    reads to derive a world's navigation_mode (location capability):
      - ``False`` (default): no cartography.yaml — world has no location
        capability, so /api/genres reports ``navigation_mode: None``.
      - ``True``: write a cartography.yaml WITHOUT an explicit
        ``navigation_mode`` key, exercising the CartographyConfig default
        (``region``).
      - a string (e.g. ``"room_graph"``): write cartography.yaml with that
        explicit ``navigation_mode``.
    """
    genre_dir = packs_dir / genre_slug
    genre_dir.mkdir(parents=True, exist_ok=True)

    # pack.yaml
    (genre_dir / "pack.yaml").write_text(
        yaml.dump(
            {
                "name": f"{genre_slug.replace('_', ' ').title()}",
                "description": f"Test description for {genre_slug}",
                "code": genre_slug,
                "version": "1.0",
                "genre": genre_slug,
                "system": "generic",
                "intended_audience": "all",
                "content_warnings": [],
                "tags": [],
            }
        ),
        encoding="utf-8",
    )

    # worlds/world_slug/world.yaml
    world_dir = genre_dir / "worlds" / world_slug
    world_dir.mkdir(parents=True, exist_ok=True)
    world_yaml: dict[str, object] = {
        "name": f"{world_slug.replace('_', ' ').title()}",
        "description": f"A world called {world_slug}",
        "starting_location": "Town Square",
        "era": "1878",
        "setting": "The frontier",
        "inspirations": ["Tombstone", "High Noon"],
        "axis_snapshot": {"tension": 0.4, "mystery": 0.6},
    }
    if cover_poi is not None:
        world_yaml["cover_poi"] = cover_poi
    (world_dir / "world.yaml").write_text(yaml.dump(world_yaml), encoding="utf-8")

    if cartography:
        cart_yaml: dict[str, object] = {
            "world_name": world_yaml["name"],
            "starting_region": "town_square",
        }
        if isinstance(cartography, str):
            cart_yaml["navigation_mode"] = cartography
        (world_dir / "cartography.yaml").write_text(yaml.dump(cart_yaml), encoding="utf-8")


def _make_app(tmp_path: Path) -> TestClient:
    packs_dir = tmp_path / "genre_packs"
    packs_dir.mkdir()
    _create_mock_genre_pack(packs_dir, "spaghetti_western", "dust_and_lead")
    _create_mock_genre_pack(packs_dir, "caverns_and_claudes", "flickering_reach")

    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    app = create_app(
        genre_pack_search_paths=[packs_dir],
        save_dir=saves_dir,
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/genres
# ---------------------------------------------------------------------------


def test_list_genres_returns_dict(tmp_path):
    """GET /api/genres returns a dict keyed by genre slug."""
    client = _make_app(tmp_path)
    resp = client.get("/api/genres")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


def test_list_genres_contains_expected_genres(tmp_path):
    """GET /api/genres includes genres from the mock packs directory."""
    client = _make_app(tmp_path)
    data = client.get("/api/genres").json()
    assert "spaghetti_western" in data
    assert "caverns_and_claudes" in data


def test_list_genres_has_name_and_description(tmp_path):
    """Genre entries have name and description fields."""
    client = _make_app(tmp_path)
    data = client.get("/api/genres").json()
    genre = data["spaghetti_western"]
    assert "name" in genre
    assert "description" in genre
    assert genre["name"] == "Spaghetti Western"


def test_list_genres_has_worlds(tmp_path):
    """Genre entries include a worlds list."""
    client = _make_app(tmp_path)
    data = client.get("/api/genres").json()
    worlds = data["spaghetti_western"]["worlds"]
    assert isinstance(worlds, list)
    assert len(worlds) >= 1
    world = worlds[0]
    assert world["slug"] == "dust_and_lead"
    assert world["name"] == "Dust And Lead"
    assert world["era"] == "1878"
    assert world["setting"] == "The frontier"
    assert world["inspirations"] == ["Tombstone", "High Noon"]


def test_list_genres_navigation_mode_none_without_cartography(tmp_path):
    """A world with no sibling cartography.yaml has no location capability,
    so /api/genres reports navigation_mode: null. The lobby uses this to
    keep the Location tab hidden for non-cartography worlds.
    """
    client = _make_app(tmp_path)
    world = client.get("/api/genres").json()["spaghetti_western"]["worlds"][0]
    assert world["navigation_mode"] is None


def test_list_genres_navigation_mode_defaults_region_with_cartography(tmp_path):
    """A world WITH cartography.yaml but no explicit navigation_mode key
    inherits the CartographyConfig default of 'region' — region-mode worlds
    are location-capable and must surface a stable Location tab.
    """
    packs_dir = tmp_path / "genre_packs"
    packs_dir.mkdir()
    _create_mock_genre_pack(packs_dir, "tea_and_murder", "glenross", cartography=True)
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    app = create_app(genre_pack_search_paths=[packs_dir], save_dir=saves_dir)
    client = TestClient(app)
    world = client.get("/api/genres").json()["tea_and_murder"]["worlds"][0]
    assert world["navigation_mode"] == "region"


def test_list_genres_navigation_mode_room_graph_passthrough(tmp_path):
    """An explicit navigation_mode in cartography.yaml is surfaced verbatim
    so room_graph worlds (e.g. the megadungeon) are location-capable too.
    """
    packs_dir = tmp_path / "genre_packs"
    packs_dir.mkdir()
    _create_mock_genre_pack(
        packs_dir, "caverns_and_claudes", "beneath_sunden", cartography="room_graph"
    )
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    app = create_app(genre_pack_search_paths=[packs_dir], save_dir=saves_dir)
    client = TestClient(app)
    world = client.get("/api/genres").json()["caverns_and_claudes"]["worlds"][0]
    assert world["navigation_mode"] == "room_graph"


def test_list_genres_skips_symlinked_world_aliases(tmp_path):
    """A world directory that is a symlink to another world (used as a
    backwards-compat alias for renamed slugs) must NOT be listed as a
    separate world. Otherwise the lobby renders the same world twice
    under both the old and new slug, with identical display names.
    """
    packs_dir = tmp_path / "genre_packs"
    packs_dir.mkdir()
    _create_mock_genre_pack(packs_dir, "caverns_and_claudes", "dungeon_survivor")

    # Create a backwards-compat symlink alias: primetime → dungeon_survivor
    worlds_dir = packs_dir / "caverns_and_claudes" / "worlds"
    (worlds_dir / "primetime").symlink_to(worlds_dir / "dungeon_survivor", target_is_directory=True)

    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    app = create_app(
        genre_pack_search_paths=[packs_dir],
        save_dir=saves_dir,
    )
    client = TestClient(app)
    worlds = client.get("/api/genres").json()["caverns_and_claudes"]["worlds"]
    slugs = [w["slug"] for w in worlds]
    assert slugs == ["dungeon_survivor"], f"symlinked alias must be skipped; got {slugs}"


def _make_app_with_cover_poi(
    tmp_path: Path,
    cover_poi: str | None,
) -> TestClient:
    packs_dir = tmp_path / "genre_packs"
    packs_dir.mkdir()
    _create_mock_genre_pack(packs_dir, "spaghetti_western", "dust_and_lead", cover_poi=cover_poi)
    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()
    app = create_app(genre_pack_search_paths=[packs_dir], save_dir=saves_dir)
    return TestClient(app)


def test_list_genres_hero_image_uses_cdn_seam_by_default(tmp_path, monkeypatch):
    """When cover_poi is set and no SIDEQUEST_ASSET_BASE_URL override is in
    effect, hero_image must be a fully-qualified CDN URL produced by
    resolve_asset_url. Bug fix: rest.py used to do an on-disk Path.exists()
    check and return null whenever the file wasn't local — hiding R2-hosted
    POI images from the lobby. The seam (asset_urls.resolve_asset_url) is
    the canonical resolver for all genre-pack-relative media URLs.
    """
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    client = _make_app_with_cover_poi(tmp_path, cover_poi="town_square")
    world = client.get("/api/genres").json()["spaghetti_western"]["worlds"][0]
    assert world["hero_image"] == (
        "https://cdn.slabgorb.com/genre_packs/spaghetti_western"
        "/worlds/dust_and_lead/assets/poi/town_square.png"
    )


def test_list_genres_hero_image_local_mode_uses_genre_mount(tmp_path, monkeypatch):
    """SIDEQUEST_ASSET_BASE_URL=local must rewrite hero_image to the
    /genre/* static mount (matches audio_cue + portrait pipelines)."""
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    client = _make_app_with_cover_poi(tmp_path, cover_poi="town_square")
    world = client.get("/api/genres").json()["spaghetti_western"]["worlds"][0]
    assert world["hero_image"] == (
        "/genre/spaghetti_western/worlds/dust_and_lead/assets/poi/town_square.png"
    )


def test_list_genres_hero_image_null_when_cover_poi_absent(tmp_path, monkeypatch):
    """No cover_poi key in world.yaml → hero_image is null (placeholder).
    Existence-on-disk is no longer probed; only a missing cover_poi key
    yields null."""
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    client = _make_app_with_cover_poi(tmp_path, cover_poi=None)
    world = client.get("/api/genres").json()["spaghetti_western"]["worlds"][0]
    assert world["hero_image"] is None


def test_list_genres_empty_when_no_packs_dir(tmp_path):
    """GET /api/genres returns {} when no valid genre pack directories exist."""
    nonexistent = tmp_path / "no_such_dir"
    app = create_app(
        genre_pack_search_paths=[nonexistent],
        save_dir=tmp_path / "saves",
    )
    client = TestClient(app)
    data = client.get("/api/genres").json()
    assert data == {}


def test_list_genres_skips_bad_pack_yaml(tmp_path):
    """Broken pack.yaml is silently skipped (best-effort)."""
    packs_dir = tmp_path / "genre_packs"
    packs_dir.mkdir()
    _create_mock_genre_pack(packs_dir, "good_genre", "good_world")

    # Write a broken pack.yaml for a second genre
    bad_genre_dir = packs_dir / "broken_genre"
    bad_genre_dir.mkdir()
    (bad_genre_dir / "pack.yaml").write_text(
        "this: is: not: valid: yaml: [{{",
        encoding="utf-8",
    )

    saves_dir = tmp_path / "saves"
    saves_dir.mkdir()

    app = create_app(genre_pack_search_paths=[packs_dir], save_dir=saves_dir)
    client = TestClient(app)
    data = client.get("/api/genres").json()
    # Good genre is present, broken one is absent
    assert "good_genre" in data
    assert "broken_genre" not in data


def test_list_genres_axis_snapshot_format(tmp_path):
    """axis_snapshot is a dict of str → float."""
    client = _make_app(tmp_path)
    data = client.get("/api/genres").json()
    snapshot = data["spaghetti_western"]["worlds"][0]["axis_snapshot"]
    assert isinstance(snapshot, dict)
    for k, v in snapshot.items():
        assert isinstance(k, str)
        assert isinstance(v, (int, float))


# ---------------------------------------------------------------------------
# GET /api/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_empty(tmp_path):
    """GET /api/sessions returns empty sessions list (Phase 1 single-player)."""
    client = _make_app(tmp_path)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"sessions": []}


# ---------------------------------------------------------------------------
# GET /api/debug/state — GM dashboard State tab
#
# ADR-115 D7: /api/debug/state now reads sessions + snapshots from Postgres
# (PgForensicReader.list_saves + PgSaveRepository.load), not the SQLite
# save.db walk. These tests seed a migrated PG pool and assert the projection
# flows through the lifted endpoint. The empty-state assertion now lives in
# tests/server/test_rest_pg_forensic.py (it needs an isolated migrated_db
# pool — a bare TestClient would read whatever SIDEQUEST_DATABASE_URL points
# at).
# ---------------------------------------------------------------------------


def _pg_app(monkeypatch, migrated_db: str, tmp_path) -> tuple:
    """Bind the process-global pool to a migrated_db and return (client, pool)."""
    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    client = _make_app(tmp_path)
    return client, pool


def test_debug_state_projects_saved_game(monkeypatch, migrated_db, tmp_path):
    """A persisted GameSnapshot (in PG) shows up in the SessionStateView list."""
    from sidequest.game import db_pool
    from sidequest.game.npc_pool import NpcPoolMember
    from sidequest.game.persistence import GameMode
    from sidequest.game.pg import sessions as pg_sessions
    from sidequest.game.pg.save_repository import PgSaveRepository
    from sidequest.game.session import GameSnapshot, TurnManager

    client, pool = _pg_app(monkeypatch, migrated_db, tmp_path)
    try:
        slug = "dust-and-lead-2026-05-26"
        pg_sessions.ensure_session(
            pool, slug=slug, mode="solo", genre_slug="spaghetti_western", world_slug="dust_and_lead"
        )
        repo = PgSaveRepository.for_slug(
            pool,
            slug=slug,
            mode=GameMode.SOLO,
            genre_slug="spaghetti_western",
            world_slug="dust_and_lead",
        )
        snap = GameSnapshot(
            genre_slug="spaghetti_western",
            world_slug="dust_and_lead",
            discovered_regions=["Sangre River Ford", "Dust Town"],
            npc_pool=[
                NpcPoolMember(
                    name="El Paso",
                    pronouns="he/him",
                    role="sheriff",
                    drawn_from="world_authored",
                )
            ],
            turn_manager=TurnManager(interaction=3),
        )
        repo.save(snap)

        resp = client.get("/api/debug/state")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        view = next(v for v in body if v["session_key"] == slug)
        assert view["genre_slug"] == "spaghetti_western"
        assert view["world_slug"] == "dust_and_lead"
        assert view["current_location"] == ""
        assert "Sangre River Ford" in view["discovered_regions"]
        assert any(entry["name"] == "El Paso" for entry in view["npc_registry"]), (
            f"El Paso missing from /api/debug/state projection: {view['npc_registry']!r}"
        )
        assert view["player_count"] == 0
    finally:
        db_pool.close_pool()


def test_debug_state_with_character_does_not_500(monkeypatch, migrated_db, tmp_path):
    """A saved snapshot containing a Character must not 500 (Combatant-method
    resolution regression — playtest 2026-04-23), now through the PG read."""
    from sidequest.game import db_pool
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.persistence import GameMode
    from sidequest.game.pg import sessions as pg_sessions
    from sidequest.game.pg.save_repository import PgSaveRepository
    from sidequest.game.session import GameSnapshot, TurnManager

    client, pool = _pg_app(monkeypatch, migrated_db, tmp_path)
    try:
        slug = "dust-and-lead-char-2026-05-26"
        pg_sessions.ensure_session(
            pool, slug=slug, mode="solo", genre_slug="spaghetti_western", world_slug="dust_and_lead"
        )
        repo = PgSaveRepository.for_slug(
            pool,
            slug=slug,
            mode=GameMode.SOLO,
            genre_slug="spaghetti_western",
            world_slug="dust_and_lead",
        )
        char = Character(
            core=CreatureCore(
                name="El Paso",
                description="A weathered gunslinger",
                personality="quiet",
                inventory=Inventory(),
                level=4,
                xp=37,
            ),
            char_class="Gunslinger",
            race="Human",
            backstory="Rode in from the dust",
        )
        snap = GameSnapshot(
            genre_slug="spaghetti_western",
            world_slug="dust_and_lead",
            characters=[char],
            turn_manager=TurnManager(interaction=3),
        )
        snap.character_locations["El Paso"] = "Sangre River Ford"
        repo.save(snap)

        resp = client.get(f"/api/debug/state?session_key={slug}")
        assert resp.status_code == 200, f"500 regression — body: {resp.text}"
        body = resp.json()
        assert len(body) == 1
        view = body[0]
        assert view["player_count"] == 1
        player = view["players"][0]
        # Methods must be CALLED, not stringified as "<bound method ...>"
        assert player["character_name"] == "El Paso"
        assert player["character_level"] == 4
    finally:
        db_pool.close_pool()


def test_debug_state_sorts_newest_first_and_filters_by_session_key(
    monkeypatch, migrated_db, tmp_path
):
    """/api/debug/state sorts newest-first by last_activity_ts and filters by
    session_key (playtest 2026-04-24 default-[0]-pick regression). Now backed
    by PgForensicReader.list_saves' last_played DESC ordering rather than
    SQLite save-file mtime.
    """
    import time as _time

    from sidequest.game import db_pool
    from sidequest.game.persistence import GameMode
    from sidequest.game.pg import sessions as pg_sessions
    from sidequest.game.pg.save_repository import PgSaveRepository
    from sidequest.game.session import GameSnapshot, TurnManager

    client, pool = _pg_app(monkeypatch, migrated_db, tmp_path)
    try:

        def _seed(slug: str, world_slug: str, location: str) -> None:
            pg_sessions.ensure_session(
                pool,
                slug=slug,
                mode="solo",
                genre_slug="spaghetti_western",
                world_slug=world_slug,
            )
            repo = PgSaveRepository.for_slug(
                pool,
                slug=slug,
                mode=GameMode.SOLO,
                genre_slug="spaghetti_western",
                world_slug=world_slug,
            )
            repo.save(
                GameSnapshot(
                    genre_slug="spaghetti_western",
                    world_slug=world_slug,
                    location=location,
                    turn_manager=TurnManager(interaction=0),
                )
            )

        old_slug = "ghost-town-2026-04-22"
        new_slug = "flickering-reach-2026-04-24"
        _seed(old_slug, "ghost_town", "Graveyard")
        # ensure_session sets last_played on each call; the second seed's
        # last_played is strictly later than the first.
        _time.sleep(0.01)
        _seed(new_slug, "flickering_reach", "The Filtration Warren")

        resp = client.get("/api/debug/state")
        assert resp.status_code == 200
        body = resp.json()
        keys = [v["session_key"] for v in body]
        # newest-first: the later-seeded session leads.
        assert keys.index(new_slug) < keys.index(old_slug), (
            "debug_state must sort newest-last_played first so the dashboard's "
            f"default [0] pick lands on the active session; got {keys}"
        )

        # session_key filter narrows to exactly one entry.
        filtered = client.get(f"/api/debug/state?session_key={old_slug}").json()
        assert len(filtered) == 1
        assert filtered[0]["session_key"] == old_slug

        # Unknown session_key returns []; no 404.
        missing = client.get("/api/debug/state?session_key=does-not-exist")
        assert missing.status_code == 200
        assert missing.json() == []
    finally:
        db_pool.close_pool()


def test_cors_headers_present_for_dashboard(monkeypatch, migrated_db, tmp_path):
    """Dev UI on :5173 must receive CORS headers so the dashboard's
    cross-origin fetch('/api/debug/state') polls don't spam the console.

    ADR-115 D7: /api/debug/state reads the process-global PG pool, so this
    test binds a migrated_db pool (an empty session list still returns 200
    with CORS headers attached by middleware)."""
    from sidequest.game import db_pool

    client, _pool = _pg_app(monkeypatch, migrated_db, tmp_path)
    try:
        resp = client.get(
            "/api/debug/state",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
    finally:
        db_pool.close_pool()
