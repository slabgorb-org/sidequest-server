"""Internal Postgres helpers: the per-session locked transaction (ADR-115)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool


@contextmanager
def session_tx(pool: ConnectionPool, session_id: int) -> Iterator[psycopg.Connection]:
    """Borrow a pooled connection, take the per-session row lock, yield it.

    Commits on clean exit, rolls back on exception. The row lock on the
    sessions row serializes same-session writers (replacing SAVE_WRITE_LOCK);
    different sessions lock different rows and never contend.
    """
    with pool.connection() as conn:  # psycopg commits on clean __exit__
        conn.execute("SELECT 1 FROM sessions WHERE session_id = %s FOR UPDATE", (session_id,))
        yield conn
