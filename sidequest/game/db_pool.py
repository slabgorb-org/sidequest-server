"""Process-global Postgres connection pool (ADR-115).

The single source of Postgres connections. Lazily opened from
db_config.database_url(). No Silent Fallbacks: an unset URL raises via the
resolver. Connections are borrowed inside `with get_pool().connection()`
scopes and never escape them or cross threads.
"""

from __future__ import annotations

import threading

from psycopg_pool import ConnectionPool

from sidequest.game.db_config import database_url

_POOL: ConnectionPool | None = None
_LOCK = threading.Lock()


def get_pool() -> ConnectionPool:
    """Return the process-global pool, opening it on first use."""
    global _POOL
    if _POOL is None:
        with _LOCK:
            if _POOL is None:
                _POOL = ConnectionPool(
                    conninfo=database_url(),
                    min_size=1,
                    max_size=16,
                    open=True,
                    name="sidequest-save",
                )
    return _POOL


def close_pool() -> None:
    """Close and discard the pool (shutdown / test reset)."""
    global _POOL
    with _LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None
