"""Postgres scrapbook_entries adapter (ADR-115 A6).

Faithful port of the scrapbook read/write paths from:
  - sidequest/server/emitters.py  (persist_scrapbook_entry, update_scrapbook_image_url)
  - sidequest/game/scrapbook_coverage.py  (scrapbook_turn_ids predicate)
  - sidequest/handlers/connect.py  (scrapbook_image_url_map SELECT)

SQLite differences resolved here:
  - No ``rowid``: update_scrapbook_image_url uses a CTE on the identity ``id``
    column (BIGINT GENERATED ALWAYS AS IDENTITY) to target the most-recent
    NULL-image row, matching the ``rowid DESC LIMIT 1`` semantics exactly.
  - No SAVE_WRITE_LOCK: replaced by ``session_tx`` per-session row lock.
  - ``session_id`` predicate added to every query (SQLite had one file per
    session; Postgres is multi-tenant within a single table).
  - ``created_at`` written explicitly as ISO-8601 TEXT (the column has no
    server-side default — the SQLite emitter relied on the schema default
    not being needed because it passed the value; Postgres schema also
    declares NOT NULL with no DEFAULT, so we must write it).

Serialization:
  - ``world_facts``: ``json.dumps(list(payload.world_facts))`` — mirrors emitters.py:49.
  - ``npcs_present``: ``json.dumps([{"name": ..., "role": ..., "disposition": ...}])``
    — mirrors emitters.py:43-48.
  Both are stored as TEXT (JSON string) in the column.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from psycopg_pool import ConnectionPool

from sidequest.game.pg._conn import session_tx

logger = logging.getLogger(__name__)


class PgScrapbookStore:
    """Postgres adapter for scrapbook_entries (ADR-115 A6).

    Bound to one session; all queries carry a ``session_id = %s`` predicate.
    Writes run inside ``session_tx`` (per-session row lock, commits on exit).
    Reads use a plain pooled connection (lock-free, consistent within the
    connection's transaction).
    """

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append_scrapbook_entry(
        self,
        *,
        turn_id: int,
        scene_title: str | None,
        scene_type: str | None,
        location: str,
        image_url: str | None,
        narrative_excerpt: str,
        world_facts: list,
        npcs_present: list,
        render_status: str,
    ) -> None:
        """Insert one scrapbook row.

        Field order and serialization mirror ``emitters.persist_scrapbook_entry``
        exactly:
          - world_facts  → json.dumps(list(world_facts))  (emitters.py:49)
          - npcs_present → json.dumps([{name,role,disposition}])  (emitters.py:43-48)
          - created_at   → ISO-8601 UTC now (explicit; column has no server DEFAULT)
        """
        facts_json = json.dumps(list(world_facts))
        npcs_json = json.dumps(
            [
                {"name": ref["name"], "role": ref["role"], "disposition": ref["disposition"]}
                if isinstance(ref, dict)
                else {"name": ref.name, "role": ref.role, "disposition": ref.disposition}
                for ref in npcs_present
            ]
        )
        now = datetime.now(tz=UTC).isoformat()

        with session_tx(self._pool, self._sid) as conn:
            conn.execute(
                """
                INSERT INTO scrapbook_entries
                    (session_id, turn_id, scene_title, scene_type, location, image_url,
                     narrative_excerpt, world_facts, npcs_present, render_status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._sid,
                    turn_id,
                    scene_title,
                    scene_type,
                    location,
                    image_url,
                    narrative_excerpt,
                    facts_json,
                    npcs_json,
                    render_status,
                    now,
                ),
            )

    def update_scrapbook_image_url(self, *, turn_id: int, image_url: str) -> bool:
        """Backfill ``image_url`` on the most-recent NULL-image row for ``turn_id``.

        Returns ``True`` when a row was updated, ``False`` when no matching
        NULL-image row exists for this session + turn_id.

        SQLite used ``rowid DESC LIMIT 1``; Postgres uses the identity ``id``
        column (BIGINT GENERATED ALWAYS AS IDENTITY) in a correlated subquery
        — same semantics (most recently inserted row first).

        Runs inside ``session_tx`` (it is a write).
        """
        with session_tx(self._pool, self._sid) as conn:
            cur = conn.execute(
                """
                UPDATE scrapbook_entries
                   SET image_url = %s
                 WHERE id = (
                     SELECT id FROM scrapbook_entries
                      WHERE session_id = %s
                        AND turn_id = %s
                        AND image_url IS NULL
                      ORDER BY id DESC
                      LIMIT 1
                 )
                """,
                (image_url, self._sid, turn_id),
            )
            return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def scrapbook_turn_ids(self, *, max_turn: int) -> set[int]:
        """Return distinct turn_ids with at least one scrapbook entry.

        Predicate mirrors ``scrapbook_coverage.py:89-92``:
          ``WHERE turn_id >= 1 AND turn_id <= max_turn``

        Rows with turn_id <= 0 are noise (test fixture artifacts or
        pre-lockstep stragglers) and excluded exactly as in the SQLite path.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT turn_id
                  FROM scrapbook_entries
                 WHERE session_id = %s
                   AND turn_id >= 1
                   AND turn_id <= %s
                """,
                (self._sid, max_turn),
            ).fetchall()
        return {int(r[0]) for r in rows}

    def scrapbook_image_url_map(self) -> dict[int, str]:
        """Return ``{turn_id: image_url}`` for all non-NULL image_url rows.

        Mirrors ``connect.py:1108-1114``:
          ``SELECT turn_id, image_url FROM scrapbook_entries WHERE image_url IS NOT NULL``
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT turn_id, image_url
                  FROM scrapbook_entries
                 WHERE session_id = %s
                   AND image_url IS NOT NULL
                """,
                (self._sid,),
            ).fetchall()
        result: dict[int, str] = {}
        for turn_id, url in rows:
            if isinstance(turn_id, int) and isinstance(url, str) and url:
                result[turn_id] = url
        return result
