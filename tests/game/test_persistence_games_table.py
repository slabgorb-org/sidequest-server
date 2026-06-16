from pathlib import Path

from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    get_game,
    upsert_game,
)


def test_game_mode_enum_values():
    assert GameMode.SOLO.value == "solo"
    assert GameMode.MULTIPLAYER.value == "multiplayer"


def test_db_path_for_slug_places_db_under_slug_dir(tmp_path: Path):
    p = db_path_for_slug(tmp_path, "2026-04-22-moldharrow-keep")
    assert p == tmp_path / "games" / "2026-04-22-moldharrow-keep" / "save.db"


def test_upsert_game_inserts_new_row(tmp_path: Path):
    db = db_path_for_slug(tmp_path, "2026-04-22-moldharrow-keep")
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug="2026-04-22-moldharrow-keep",
        mode=GameMode.MULTIPLAYER,
        genre_slug="low_fantasy",
        world_slug="moldharrow-keep",
    )
    row = get_game(store, "2026-04-22-moldharrow-keep")
    assert row is not None
    assert row.mode == GameMode.MULTIPLAYER
    assert row.genre_slug == "low_fantasy"
    assert row.world_slug == "moldharrow-keep"
    assert row.claude_session_id is None


def test_upsert_game_does_not_overwrite_mode_on_resume(tmp_path: Path):
    db = db_path_for_slug(tmp_path, "2026-04-22-moldharrow-keep")
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug="2026-04-22-moldharrow-keep",
        mode=GameMode.MULTIPLAYER,
        genre_slug="low_fantasy",
        world_slug="moldharrow-keep",
    )
    upsert_game(
        store,
        slug="2026-04-22-moldharrow-keep",
        mode=GameMode.SOLO,
        genre_slug="low_fantasy",
        world_slug="moldharrow-keep",
    )
    row = get_game(store, "2026-04-22-moldharrow-keep")
    assert row.mode == GameMode.MULTIPLAYER  # frozen at creation


def test_set_claude_session_id_persists(tmp_path: Path):
    db = db_path_for_slug(tmp_path, "2026-04-22-moldharrow-keep")
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug="2026-04-22-moldharrow-keep",
        mode=GameMode.SOLO,
        genre_slug="low_fantasy",
        world_slug="moldharrow-keep",
    )
    from sidequest.game.persistence import set_claude_session_id

    set_claude_session_id(store, "2026-04-22-moldharrow-keep", "claude-sess-abc123")
    row = get_game(store, "2026-04-22-moldharrow-keep")
    assert row.claude_session_id == "claude-sess-abc123"
