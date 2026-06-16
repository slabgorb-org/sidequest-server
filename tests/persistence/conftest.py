"""Ephemeral real-Postgres fixtures for the persistence suite (ADR-115).

The ``migrated_db`` and ``pg_conn`` fixtures were lifted to the tests/ root
conftest in ADR-115 D5 so the few non-persistence suites that drive real PG
(server/test_save_write_lock.py) can reuse them. This package inherits them
from there — no per-package redefinition (one definition per fixture).

Strategy (in tests/conftest.py): a session-scoped fixture creates a
uniquely-named database (per pytest-xdist worker), runs `alembic upgrade head`,
yields its conninfo URL, and DROPs it on teardown. No SQLite dual-path. If
SIDEQUEST_TEST_DATABASE_URL is unset, the suite SKIPS with a loud reason
(local devs run `just pg-up`; CI sets it from `services: postgres`).
"""

from __future__ import annotations
