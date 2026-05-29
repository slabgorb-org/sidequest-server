"""The behind-head schema guard is wired into the real server boot path (71-20).

Behavior-level wiring test (CLAUDE.md: No Source-Text Wiring Tests). We do NOT
grep app.py for the call site — we drive the REAL ``create_app()`` lifespan via
``with TestClient(...)`` (the same full-startup path
``test_app.py::test_db_pool_opens_and_closes_with_app`` exercises) against a
behind-head Postgres DB, and assert startup fails loud with
``SchemaBehindHeadError``. That proves two things at once:

  * AC#1 / AC#5 — the failure lands AT BOOT, not deferred to a mid-turn write.
  * The guard is actually reachable from the production startup path — not just a
    function that exists in isolation (the ADR-115 fail-loud contract is wired,
    not merely defined).

The head-current control proves the guard does not false-positive a good DB.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sidequest.game import db_pool
from sidequest.server.app import create_app


def test_startup_fails_loud_on_behind_head_db(monkeypatch, behind_head_db: str) -> None:
    """Booting against a behind-head DB raises ``SchemaBehindHeadError`` during
    lifespan startup — the server refuses to come up on a stale schema."""
    from sidequest.game.db_schema_check import SchemaBehindHeadError

    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", behind_head_db)
    db_pool.close_pool()  # deterministic start: no live pool

    try:
        with pytest.raises(SchemaBehindHeadError):
            # Entering the context fires the lifespan startup events; the schema
            # guard must abort boot here, not let the app reach a ready state.
            with TestClient(create_app()):
                pass
    finally:
        db_pool.close_pool()


def test_startup_succeeds_on_head_current_db(monkeypatch, migrated_db: str) -> None:
    """Control: a head-current DB boots clean and serves /health — the guard
    does not block a correctly-migrated schema."""
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()

    try:
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
    finally:
        db_pool.close_pool()
