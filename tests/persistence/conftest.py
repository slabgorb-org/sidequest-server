"""Ephemeral real-Postgres fixtures for the persistence suite (ADR-115).

Strategy: a session-scoped fixture creates a uniquely-named database
(per pytest-xdist worker), runs `alembic upgrade head` into it, yields its
conninfo URL, and DROPs it on teardown. No SQLite dual-path. If
SIDEQUEST_TEST_DATABASE_URL is unset, the suite SKIPS with a loud reason
(local devs run `just pg-up`; CI sets it from `services: postgres`).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from alembic.config import Config

from alembic import command

_ADMIN_ENV = "SIDEQUEST_TEST_DATABASE_URL"


def _admin_conninfo() -> str:
    url = os.environ.get(_ADMIN_ENV)
    if not url:
        pytest.skip(
            f"{_ADMIN_ENV} unset — start local Postgres with `just pg-up` and export "
            f"{_ADMIN_ENV}=postgresql://$USER@localhost:5432/sidequest_test, or run in CI."
        )
    return url


def _swap_dbname(conninfo: str, dbname: str) -> str:
    """Return ``conninfo`` with its path (database name) replaced by ``dbname``."""
    head, _, _tail = conninfo.partition("?")
    base, _slash, _olddb = head.rpartition("/")
    rebuilt = f"{base}/{dbname}"
    if _tail:
        rebuilt = f"{rebuilt}?{_tail}"
    return rebuilt


@pytest.fixture(scope="session")
def migrated_db(worker_id: str) -> Iterator[str]:
    """A freshly-migrated throwaway Postgres database; conninfo URL yielded.

    ``worker_id`` is injected by pytest-xdist ("gw0", "gw1", ... or "master"
    when serial); it namespaces the db so parallel workers do not collide.
    """
    admin = _admin_conninfo()
    db_name = f"sq_test_{worker_id}_{uuid.uuid4().hex[:8]}"

    # CREATE/DROP DATABASE cannot run inside a transaction block.
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    target = _swap_dbname(admin, db_name)
    try:
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        # Alembic uses the +psycopg SQLAlchemy form of the target URL.
        cfg.set_main_option(
            "sqlalchemy.url",
            target
            if target.startswith("postgresql+psycopg://")
            else target.replace("postgresql://", "postgresql+psycopg://", 1),
        )
        command.upgrade(cfg, "head")
        yield target
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


@pytest.fixture
def pg_conn(migrated_db: str) -> Iterator[psycopg.Connection]:
    """A connection to the migrated db, wrapped in a transaction that always
    rolls back — per-test isolation without re-running migrations."""
    with psycopg.connect(migrated_db) as conn:
        try:
            yield conn
        finally:
            conn.rollback()
