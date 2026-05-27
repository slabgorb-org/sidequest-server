"""D1 wiring test — _SessionData carries PgSaveRepository / PgDungeonRepository /
PgTelemetrySink (ADR-115 Task-Group D, Task D1).

Strategy: fixture-driven behaviour test (not source-text grep).  Constructs the
three Postgres repositories the same way the connect handler does and asserts
that they satisfy their respective Protocols.  Requires a migrated_db (Postgres
test database); skips when SIDEQUEST_TEST_DATABASE_URL is unset.

The test does NOT drive the full slug-connect path (that path needs a WebSocket
handler, RoomRegistry, genre packs, and a dozen other collaborators).  Instead it
invokes ``_build_pg_repos_for_slug`` — the minimal constructor helper extracted by
D1 from the connect handler — and asserts the three returned objects satisfy their
respective Repository/Sink Protocol contracts via ``isinstance``.

If the D1 constructor helper does not yet exist this test will fail (RED) until
the production implementation is in place.
"""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.pg.dungeon import PgDungeonRepository
from sidequest.game.pg.save_repository import PgSaveRepository
from sidequest.game.pg.telemetry import PgTelemetrySink
from sidequest.game.repository import DungeonRepository, SaveRepository, TelemetrySink
from sidequest.server.session_state import _SessionData


def _fresh_slug() -> str:
    return f"d1test_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def pg_pool(monkeypatch, migrated_db: str):
    """Open a ConnectionPool against the ephemeral migrated test DB."""
    # Normalise postgresql+psycopg:// → postgresql:// for db_pool.database_url
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    yield pool
    db_pool.close_pool()


def test_build_pg_repos_returns_protocol_conformant_objects(pg_pool):
    """_build_pg_repos_for_slug returns three objects that satisfy their protocols.

    Asserts:
      - repository    is a PgSaveRepository AND satisfies SaveRepository
      - dungeon_repo  is a PgDungeonRepository AND satisfies DungeonRepository
      - telemetry_sink is a PgTelemetrySink AND satisfies TelemetrySink
    """
    from sidequest.server.session_state import _build_pg_repos_for_slug

    slug = _fresh_slug()
    repository, dungeon_repo, telemetry_sink = _build_pg_repos_for_slug(
        pg_pool,
        slug=slug,
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )

    assert isinstance(repository, PgSaveRepository)
    assert isinstance(repository, SaveRepository), (
        "PgSaveRepository must satisfy SaveRepository Protocol"
    )
    assert isinstance(dungeon_repo, PgDungeonRepository)
    assert isinstance(dungeon_repo, DungeonRepository), (
        "PgDungeonRepository must satisfy DungeonRepository Protocol"
    )
    assert isinstance(telemetry_sink, PgTelemetrySink)
    assert isinstance(telemetry_sink, TelemetrySink), (
        "PgTelemetrySink must satisfy TelemetrySink Protocol"
    )


def test_pg_repos_share_session_id(pg_pool):
    """All three repositories are bound to the same session_id."""
    from sidequest.server.session_state import _build_pg_repos_for_slug

    slug = _fresh_slug()
    repository, dungeon_repo, telemetry_sink = _build_pg_repos_for_slug(
        pg_pool,
        slug=slug,
        mode="solo",
        genre_slug="test_genre",
        world_slug="test_world",
    )

    sid = repository.session_id  # public accessor added by D1
    assert isinstance(sid, int) and sid > 0, "session_id must be a positive int"
    assert dungeon_repo._sid == sid, "dungeon_repo must share session_id with save repo"
    assert telemetry_sink._sid == sid, "telemetry_sink must share session_id with save repo"


def test_session_data_has_repository_fields():
    """_SessionData exposes repository / dungeon_repository / telemetry_sink fields.

    This is a dataclass reflection check (not a source-text grep) — interrogates
    runtime types.  Fails RED until D1 adds the fields.
    """
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(_SessionData)}
    assert "repository" in field_names, "_SessionData must have a 'repository' field"
    assert "dungeon_repository" in field_names, "_SessionData must have a 'dungeon_repository' field"
    assert "telemetry_sink" in field_names, "_SessionData must have a 'telemetry_sink' field"
    assert "store" not in field_names, (
        "_SessionData must NOT have a 'store' field after D1 — "
        "store was replaced by repository/dungeon_repository/telemetry_sink"
    )
