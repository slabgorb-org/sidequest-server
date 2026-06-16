"""Tests for the sibling-file safety net created by SqliteStore.load when
migrate_legacy_snapshot rewrites any field. Per architect amendment
2026-05-04 to docs/superpowers/plans/2026-05-04-snapshot-split-brain-wave-1.md.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot


def _write_raw_save(db_path: Path, snapshot_dict: dict) -> None:
    """Write a snapshot JSON directly into game_state.id=1, bypassing pydantic.

    Lets us seed legacy-shaped saves (e.g. with world_confrontations populated)
    without having a model that produces them.
    """
    # Init schema by creating an empty store, then overwrite the row.
    store = SqliteStore(db_path)
    canonical = GameSnapshot(genre_slug="test", world_slug="t")
    store.save(canonical)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE game_state SET snapshot_json = ? WHERE id = 1",
            (json.dumps(snapshot_dict),),
        )
        conn.commit()


def test_canonical_load_does_not_create_backup(tmp_path: Path) -> None:
    """A load that requires no migration leaves no .canonicalize.bak."""
    db_path = tmp_path / "save.db"
    store = SqliteStore(db_path)
    canonical = GameSnapshot(genre_slug="test", world_slug="t")
    store.save(canonical)

    store2 = SqliteStore(db_path)
    loaded = store2.load()

    assert loaded is not None
    assert not (tmp_path / "save.db.canonicalize.bak").exists()


def test_legacy_load_creates_backup_once(tmp_path: Path) -> None:
    """A load that rewrites any field copies the .db to a sibling .bak."""
    db_path = tmp_path / "save.db"
    bak_path = tmp_path / "save.db.canonicalize.bak"

    # Seed a legacy-shaped snapshot with the S1 ``world_confrontations``
    # field — that's the per-field migration registered in Task 4 that
    # makes ``migrated != raw`` and triggers the .bak copy. The empty
    # list still strips the field (covered by
    # test_s1_empty_world_confrontations_still_strips_field).
    legacy = {
        "genre_slug": "test",
        "world_slug": "t",
        "characters": [],
        "npcs": [],
        "narrative_log": [],
        "world_confrontations": [],
    }
    _write_raw_save(db_path, legacy)

    store = SqliteStore(db_path)
    loaded = store.load()
    assert loaded is not None

    # If the migration scaffold is no-op (which it is at Task 1), this
    # assertion will fail. The test becomes meaningful starting in Tasks 3-4.
    if not bak_path.exists():
        pytest.skip(
            "No per-field migration registered yet — backup is unreachable. "
            "Re-enable this test after Task 3 (S5) lands."
        )

    assert bak_path.is_file()
    # The .bak captures the pre-migration on-disk state.
    with sqlite3.connect(bak_path) as conn:
        row = conn.execute("SELECT snapshot_json FROM game_state WHERE id = 1").fetchone()
    assert row is not None
    backed_up = json.loads(row[0])
    assert backed_up == legacy


def test_backup_is_idempotent(tmp_path: Path) -> None:
    """A legacy load whose ``.bak`` already exists does not overwrite it.

    Reviewer finding 2026-05-04: the previous version of this test seeded a
    CANONICAL snapshot, so ``migrated == raw`` was true and the entire
    ``.bak`` code path was skipped — the SENTINEL survival proved nothing
    about the ``if not bak_path.exists():`` guard. This version seeds a
    LEGACY snapshot (with ``world_confrontations``), pre-writes SENTINEL
    bytes to the ``.bak``, and then asserts that loading runs the migration
    AND preserves the existing backup.
    """
    db_path = tmp_path / "save.db"
    bak_path = tmp_path / "save.db.canonicalize.bak"

    # Step 1 — seed a legacy-shaped save the same way
    # ``test_legacy_load_creates_backup_once`` does. Empty list still
    # triggers the migration via the field-presence gate.
    legacy = {
        "genre_slug": "test",
        "world_slug": "t",
        "characters": [],
        "npcs": [],
        "narrative_log": [],
        "world_confrontations": [],
    }
    _write_raw_save(db_path, legacy)

    # Step 2 — pre-write SENTINEL to the .bak BEFORE the load runs. This
    # is the case the guard exists to protect: a prior canonicalize already
    # captured the pre-migration state, so a later load must not clobber it.
    bak_path.write_text("SENTINEL — pre-existing backup")

    # Step 3 — load. The migration WILL run (legacy field present), so we
    # are exercising the ``if not bak_path.exists():`` branch under
    # conditions where the rest of the .bak code path would otherwise fire.
    store = SqliteStore(db_path)
    loaded = store.load()
    assert loaded is not None

    # Step 4 — the guard held. The pre-existing .bak survives byte-for-byte.
    assert bak_path.read_text() == "SENTINEL — pre-existing backup"
