"""Postgres asset_ledger adapter (Story 65-2).

Links a save (``sessions`` row) to the runtime R2 artifacts it generated, so
the UI can rehydrate prior-turn imagery on resume. One row per R2 key (the PK).
The content sha256 is already inside ``r2_key`` — no md5/size columns.

Mirrors PgScrapbookStore: writes go through ``session_tx`` (per-session row
lock, commits on exit); reads use a plain pooled connection. Every query
carries a ``session_id = %s`` predicate (multi-tenant single table).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from psycopg_pool import ConnectionPool

from sidequest.game.pg._conn import session_tx

logger = logging.getLogger(__name__)


class PgAssetLedgerStore:
    """Postgres adapter for the per-session asset_ledger (Story 65-2)."""

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append(
        self,
        *,
        r2_key: str,
        asset_type: str,
        entity_ref: str,
        created_turn: int,
    ) -> None:
        """Upsert one ledger row keyed on ``r2_key`` (idempotent).

        Re-writing the same key updates the descriptive columns rather than
        inserting a duplicate or raising. ``created_at`` is written explicitly
        (the column has no server-side default), matching PgScrapbookStore.
        """
        now = datetime.now(tz=UTC).isoformat()
        with session_tx(self._pool, self._sid) as conn:
            conn.execute(
                """
                INSERT INTO asset_ledger
                    (r2_key, asset_type, entity_ref, created_turn, session_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (r2_key) DO UPDATE SET
                    asset_type   = EXCLUDED.asset_type,
                    entity_ref   = EXCLUDED.entity_ref,
                    created_turn = EXCLUDED.created_turn
                """,
                (r2_key, asset_type, entity_ref, created_turn, self._sid, now),
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_assets(self) -> list[dict]:
        """Return every ledger row for this session as plain dicts."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT r2_key, asset_type, entity_ref, created_turn, created_at
                  FROM asset_ledger
                 WHERE session_id = %s
                 ORDER BY created_turn, r2_key
                """,
                (self._sid,),
            ).fetchall()
        return [
            {
                "r2_key": r[0],
                "asset_type": r[1],
                "entity_ref": r[2],
                "created_turn": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]
