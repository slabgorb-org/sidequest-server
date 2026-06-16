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
        (world_dir / "cartography.yaml").write_text(
            yaml.dump(cart_yaml), encoding="utf-8"
        )


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
    _create_mock_genre_pack(
        packs_dir, "tea_and_murder", "glenross", cartography=True
    )
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
# ---------------------------------------------------------------------------


def test_debug_state_empty_when_no_save_dir(tmp_path):
    """With no games/ subdir, the endpoint returns [] (not 404)."""
    client = _make_app(tmp_path)
    resp = client.get("/api/debug/state")
    assert resp.status_code == 200
    assert resp.json() == []


def test_debug_state_projects_saved_game(tmp_path):
    """A persisted GameSnapshot shows up in the SessionStateView list."""
    from datetime import date

    from sidequest.game.game_slug import generate_slug
    from sidequest.game.npc_pool import NpcPoolMember
    from sidequest.game.persistence import SqliteStore, db_path_for_slug
    from sidequest.game.session import GameSnapshot, TurnManager

    # _make_app sets save_dir = tmp_path / "saves"
    client = _make_app(tmp_path)
    save_dir = tmp_path / "saves"
    slug = generate_slug(world_slug="dust_and_lead", today=date.today())
    db = db_path_for_slug(save_dir, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    # Story 45-52: NpcRegistryEntry / GameSnapshot.npc_registry were dropped.
    # The /api/debug/state projection reads from ``snap.npc_pool`` (and
    # ``snap.npcs``) — surface El Paso in the pool directly. The legacy
    # ``npc_registry`` wire field name is preserved on the projection side
    # (rest.py) for dashboard back-compat; the in-memory snapshot stores
    # the cast member in ``npc_pool``.
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
    store.save(snap)
    store.close()

    resp = client.get("/api/debug/state")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    view = body[0]
    assert view["session_key"] == slug
    assert view["genre_slug"] == "spaghetti_western"
    assert view["world_slug"] == "dust_and_lead"
    # Wave 2B: pre-chargen snapshot has no per-character location, so the
    # party-frame projection is empty (no consensus).
    assert view["current_location"] == ""
    assert "Sangre River Ford" in view["discovered_regions"]
    assert any(entry["name"] == "El Paso" for entry in view["npc_registry"]), (
        f"El Paso missing from /api/debug/state projection: {view['npc_registry']!r}"
    )
    assert view["player_count"] == 0


def test_debug_state_with_character_does_not_500(tmp_path):
    """Regression for playtest 2026-04-23: a saved snapshot containing a
    Character must not throw 500 when the dashboard polls /api/debug/state.

    Character.name and Character.level are Combatant-equivalent methods (Rust
    port), not attributes. rest.py used to do ``int(getattr(char, "level", 1))``
    which gave it the bound method and crashed with
    ``TypeError: int() argument must be a string ... not 'method'``.

    This test creates a snapshot with a real Character and asserts the endpoint
    returns 200 with the resolved name/level — covering both the call gate and
    the wire path the previous test_debug_state_projects_saved_game (which had
    no characters in its snapshot) never exercised.
    """
    from datetime import date

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.game_slug import generate_slug
    from sidequest.game.persistence import SqliteStore, db_path_for_slug
    from sidequest.game.session import GameSnapshot, TurnManager

    client = _make_app(tmp_path)
    save_dir = tmp_path / "saves"
    slug = generate_slug(world_slug="dust_and_lead", today=date.today())
    db = db_path_for_slug(save_dir, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
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
    store.save(snap)
    store.close()

    resp = client.get("/api/debug/state")
    assert resp.status_code == 200, f"500 regression — body: {resp.text}"
    body = resp.json()
    assert len(body) == 1
    view = body[0]
    assert view["player_count"] == 1
    player = view["players"][0]
    # Methods must be CALLED, not stringified as "<bound method ...>"
    assert player["character_name"] == "El Paso"
    assert player["character_level"] == 4


def test_debug_state_sorts_newest_first_and_filters_by_session_key(tmp_path):
    """Regression for playtest 2026-04-24: /api/debug/state returned sessions
    in alphabetical (slug) order, so the dashboard's ``debugState[0]`` pick
    landed on the oldest save instead of the active one.

    Two saves are written with staggered mtimes; the newer one must appear
    first, and ``?session_key=<slug>`` must filter to exactly one entry.
    """
    import os
    import time as _time
    from datetime import date as _date

    from sidequest.game.game_slug import generate_slug
    from sidequest.game.persistence import SqliteStore, db_path_for_slug
    from sidequest.game.session import GameSnapshot, TurnManager

    client = _make_app(tmp_path)
    save_dir = tmp_path / "saves"

    def _write_snapshot(world_slug: str, day: _date, location: str) -> str:
        slug = generate_slug(world_slug=world_slug, today=day)
        db = db_path_for_slug(save_dir, slug)
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.initialize()
        snap = GameSnapshot(
            genre_slug="spaghetti_western",
            world_slug=world_slug,
            location=location,
            turn_manager=TurnManager(interaction=0),
        )
        store.save(snap)
        store.close()
        return slug

    # Older save — touch mtime well in the past so sort order is
    # unambiguous across filesystems with coarse mtime resolution.
    old_slug = _write_snapshot("ghost_town", _date(2026, 4, 22), "Graveyard")
    old_db = db_path_for_slug(save_dir, old_slug)
    past_ts = _time.time() - 3600
    os.utime(old_db, (past_ts, past_ts))

    # Newer save — leave its mtime at "now".
    new_slug = _write_snapshot(
        "flickering_reach",
        _date(2026, 4, 24),
        "The Filtration Warren",
    )

    resp = client.get("/api/debug/state")
    assert resp.status_code == 200
    body = resp.json()
    assert [v["session_key"] for v in body] == [new_slug, old_slug], (
        "debug_state must sort newest-mtime first so the dashboard's "
        "default [0] pick lands on the active session"
    )
    # last_activity_ts must be present and strictly ordered.
    assert body[0]["last_activity_ts"] > body[1]["last_activity_ts"]

    # session_key filter narrows to exactly one entry.
    filtered = client.get(f"/api/debug/state?session_key={old_slug}").json()
    assert len(filtered) == 1
    assert filtered[0]["session_key"] == old_slug

    # Unknown session_key returns []; no 404.
    missing = client.get("/api/debug/state?session_key=does-not-exist")
    assert missing.status_code == 200
    assert missing.json() == []


def test_cors_headers_present_for_dashboard(tmp_path):
    """Dev UI on :5173 must receive CORS headers so the dashboard's
    cross-origin fetch('/api/debug/state') polls don't spam the console."""
    client = _make_app(tmp_path)
    resp = client.get(
        "/api/debug/state",
        headers={"Origin": "http://localhost:5173"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
