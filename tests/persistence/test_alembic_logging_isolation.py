"""ADR-115 D8 issue (b): alembic migrations must not disable app loggers.

``alembic/env.py`` configures logging via ``logging.config.fileConfig``. That
function defaults to ``disable_existing_loggers=True``, which silently sets
``.disabled = True`` on every logger NOT named in ``alembic.ini`` — including
application loggers such as ``sidequest.dungeon.materializer``. Once disabled,
those loggers emit nothing, so pytest's ``caplog`` can no longer capture from
them for the remainder of the process. Because the ``migrated_db`` fixture runs
a migration, any suite that runs after ``tests/persistence`` inherited a
poisoned logging state (the materializer ERROR-capture test in
``tests/dungeon`` failed only when persistence ran first).

This is a behavior regression test: it runs a real migration and asserts a
representative app logger survives enabled.
"""

from __future__ import annotations

import logging

from alembic.config import Config

from alembic import command


def test_migration_does_not_disable_app_loggers(migrated_db: str) -> None:
    """A migration run must leave already-created app loggers enabled.

    Fails when ``alembic/env.py`` calls ``fileConfig`` without
    ``disable_existing_loggers=False``.
    """
    sentinel_name = "sidequest.dungeon.materializer"
    sentinel = logging.getLogger(sentinel_name)
    # Clean baseline: prove the assertion is meaningful (the migration, not a
    # prior poisoned state, is what we measure).
    sentinel.disabled = False

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option(
        "sqlalchemy.url",
        migrated_db
        if migrated_db.startswith("postgresql+psycopg://")
        else migrated_db.replace("postgresql://", "postgresql+psycopg://", 1),
    )
    # Idempotent (already at head) but still loads + execs env.py, which fires
    # fileConfig — the exact call path that poisons loggers in production runs.
    command.upgrade(cfg, "head")

    assert sentinel.disabled is False, (
        f"alembic migration disabled the {sentinel_name!r} logger "
        "(fileConfig disable_existing_loggers defaulted True) — caplog can no "
        "longer capture from it, contaminating later suites (ADR-115 D8 issue b)"
    )
