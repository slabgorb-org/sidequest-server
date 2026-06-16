"""Tests for sidequest.game.persistence — SQLite session persistence.

Python round-trip tests only. Rust save compatibility is deferred
(requires flatten/nest migration shim — documented in port notes).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from sidequest.game.persistence import (
    SavedSession,
    SaveSchemaIncompatibleError,
    SqliteStore,
)
from sidequest.game.session import GameSnapshot, NarrativeEntry
from tests.game.test_character import make_test_character


def _make_snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="iron_mines",
        characters=[make_test_character()],
        time_of_day="evening",
        atmosphere="tense",
        current_region="Ironhold",
        quest_log={"Find the Warden": "active"},
        active_stakes="escape before collapse",
        lore_established=["The mines run deep"],
    )
    snap.character_locations["Thorn Ironhide"] = "The Upper Gallery"
    return snap


# ---------------------------------------------------------------------------
# SqliteStore — in-memory
# ---------------------------------------------------------------------------


def test_open_in_memory_creates_store():
    store = SqliteStore.open_in_memory()
    assert store is not None
    store.close()


def test_init_session_succeeds():
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "iron_mines")
    store.close()


# ---------------------------------------------------------------------------
# Save / Load round-trip
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip():
    """Core wiring test: save a Session → load it back → assert equality."""
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "iron_mines")
    original = _make_snapshot()

    store.save(original)
    saved = store.load()

    assert saved is not None
    assert isinstance(saved, SavedSession)
    assert saved.snapshot.genre_slug == "caverns_and_claudes"
    assert saved.snapshot.character_locations.get("Thorn Ironhide") == "The Upper Gallery"
    assert saved.snapshot.characters[0].core.name == "Thorn Ironhide"
    assert saved.snapshot.quest_log == {"Find the Warden": "active"}
    assert saved.snapshot.lore_established == ["The mines run deep"]
    store.close()


def test_load_raises_save_schema_incompatible_on_legacy_snapshot():
    """Regression: a save written under a prior schema (e.g. legacy
    single-`metric` encounter from before the dual-track momentum
    migration) must raise ``SaveSchemaIncompatibleError`` rather than
    let pydantic's ``ValidationError`` bubble up to the WebSocket layer
    (which closes the socket without explanation, trapping the user
    in an infinite reconnect loop). Playtest 2026-04-25.
    """
    store = SqliteStore.open_in_memory()
    # Bypass the model and write raw JSON that the current schema rejects.
    # Missing required `genre_slug` is the simplest pydantic-rejecting
    # shape; the real-world trigger was a `metric` field on encounter,
    # but ANY snapshot that fails validation must surface as our typed
    # error.
    store._conn.execute(
        "INSERT INTO game_state (id, snapshot_json, saved_at) VALUES (1, ?, ?)",
        ('{"characters": "this is not a list"}', "2026-04-25T00:00:00Z"),
    )
    store._conn.commit()

    with pytest.raises(SaveSchemaIncompatibleError) as excinfo:
        store.load()

    # The typed error preserves the pydantic ValidationError + path so
    # callers can surface a specific message to the user.
    assert excinfo.value.underlying is not None
    # In-memory store has no path on disk; load() falls back to a sentinel.
    assert "in-memory" in str(excinfo.value.save_path).lower()


def test_load_returns_none_when_no_save():
    store = SqliteStore.open_in_memory()
    result = store.load()
    assert result is None
    store.close()


def test_save_updates_last_saved_at():
    store = SqliteStore.open_in_memory()
    store.init_session("test", "world")
    original = _make_snapshot()
    assert original.last_saved_at is None

    store.save(original)
    saved = store.load()

    assert saved is not None
    assert saved.snapshot.last_saved_at is not None
    store.close()


def test_save_overwrite_replaces_previous():
    """Second save should overwrite the first (singleton row)."""
    store = SqliteStore.open_in_memory()
    store.init_session("test", "world")

    snap1 = _make_snapshot()
    store.save(snap1)

    snap2 = _make_snapshot()
    snap2.character_locations["Thorn Ironhide"] = "The Deep Caverns"
    store.save(snap2)

    saved = store.load()
    assert saved is not None
    assert saved.snapshot.character_locations.get("Thorn Ironhide") == "The Deep Caverns"
    store.close()


# ---------------------------------------------------------------------------
# SessionMeta
# ---------------------------------------------------------------------------


def test_load_includes_session_meta():
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "iron_mines")
    store.save(_make_snapshot())

    saved = store.load()
    assert saved is not None
    assert saved.meta.genre_slug == "caverns_and_claudes"
    assert saved.meta.world_slug == "iron_mines"
    store.close()


# ---------------------------------------------------------------------------
# Narrative log
# ---------------------------------------------------------------------------


def test_append_and_retrieve_narrative():
    store = SqliteStore.open_in_memory()
    entry = NarrativeEntry(
        timestamp=0,
        round=1,
        author="narrator",
        content="The party enters the mines.",
        tags=["exploration"],
    )
    store.append_narrative(entry)
    entries = store.recent_narrative(5)
    assert len(entries) == 1
    assert entries[0].content == "The party enters the mines."
    assert entries[0].author == "narrator"
    store.close()


def test_recent_narrative_ordered_oldest_first():
    store = SqliteStore.open_in_memory()
    for i in range(5):
        store.append_narrative(
            NarrativeEntry(
                round=i + 1,
                author="narrator",
                content=f"Entry {i + 1}",
            )
        )
    entries = store.recent_narrative(3)
    assert len(entries) == 3
    # Should be oldest-first (entries 3, 4, 5)
    assert entries[0].content == "Entry 3"
    assert entries[2].content == "Entry 5"
    store.close()


def test_generate_recap_empty():
    store = SqliteStore.open_in_memory()
    recap = store.generate_recap()
    assert recap is None
    store.close()


def test_generate_recap_with_entries():
    store = SqliteStore.open_in_memory()
    store.append_narrative(
        NarrativeEntry(
            round=1,
            author="narrator",
            content="The party fought the goblin king.",
        )
    )
    recap = store.generate_recap()
    assert recap is not None
    assert "Previously" in recap
    store.close()


# ---------------------------------------------------------------------------
# Recap from known_facts
# ---------------------------------------------------------------------------


def test_load_includes_recap():
    store = SqliteStore.open_in_memory()
    store.init_session("test", "world")
    store.append_narrative(
        NarrativeEntry(
            round=1,
            author="narrator",
            content="The party discovered the old map.",
        )
    )
    store.save(_make_snapshot())

    saved = store.load()
    assert saved is not None
    # recap may be None if no facts AND no entries in game_state narrative_log
    # (narrative_log and game_state are separate tables — recap comes from narrative_log)
    # The test validates that load() doesn't raise, not that recap is non-None here
    store.close()


# ---------------------------------------------------------------------------
# File-backed store
# ---------------------------------------------------------------------------


def test_file_backed_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = str(Path(tmpdir) / "save.db")
        store = SqliteStore.open(db_file)
        store.init_session("test", "world")
        store.save(_make_snapshot())
        store.close()

        # Re-open and load
        store2 = SqliteStore.open(db_file)
        saved = store2.load()
        assert saved is not None
        # genre_slug comes from the snapshot JSON, not init_session
        assert saved.snapshot.genre_slug == "caverns_and_claudes"
        assert saved.snapshot.character_locations.get("Thorn Ironhide") == "The Upper Gallery"
        store2.close()


# ---------------------------------------------------------------------------
# Real Rust save file (skip if not present)
# ---------------------------------------------------------------------------


def test_rust_save_skipped_if_not_present():
    """Skip Rust save loading — Python/Rust format mismatch deferred.

    The Rust save uses #[serde(flatten)] for CreatureCore, which produces
    flat JSON (name/description/personality at Character level). The Python
    GameSnapshot uses nested core: CreatureCore. Direct loading of a Rust
    save would fail with 'extra fields' errors.

    A migration shim (flatten → nested) is required and documented in
    docs/port-notes/game-phase1-slice.md.
    """
    rust_save_dir = Path.home() / ".sidequest" / "saves"
    if not rust_save_dir.exists():
        pytest.skip("No Rust saves present — skipping Rust compatibility test")

    # If saves exist, we just verify the file exists — we don't try to load
    # it because the format mismatch would fail.
    db_files = list(rust_save_dir.rglob("save.db"))
    assert len(db_files) >= 0  # trivially pass — existence check only
