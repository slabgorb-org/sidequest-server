"""Postgres narrative_log adapter (ADR-115 A5).

Faithful port of the narrative methods from SqliteStore
(sidequest.game.persistence):

  - append_narrative(entry)
  - max_narrative_round() -> int
  - recent_narrative(limit) -> list[NarrativeEntry]
  - generate_recap() -> str | None
  - read_narration_backfill(*, player_id, limit) -> list[BackfillRow]

Tags serialization matches SqliteStore.append_narrative exactly:
  ``json.dumps(entry.tags)`` on write, ``json.loads(tags_json)`` on read
  (with fallback to ``[]`` on decode error).

The Postgres ``narrative_log.id`` is BIGINT GENERATED ALWAYS AS IDENTITY
and is never reset per session, so it preserves strict insertion order
within a session the same way SQLite's AUTOINCREMENT does.  We rely on
it for the DESC/ASC ordering in recent_narrative and read_narration_backfill
rather than created_at (which has TEXT ISO resolution and could tie on a
fast machine).

The ``read_narration_backfill`` method extracts the four SQL reads that
``views.backfill_last_narration_block`` performs against the SQLite
``store._conn``, translating them to Postgres:
  - ``?`` → ``%s``
  - every WHERE gains ``session_id = %s``
  - ``projection_cache`` lookup gains ``session_id = %s``

Return contract (D3 wiring note)
---------------------------------
``read_narration_backfill`` returns ``list[BackfillRow]`` (a dataclass),
NOT the final ``list[object]`` that views.py delivers today.  The final
assembly step — calling ``_build_message_for_kind(kind, payload_json, seq)``
on each row — stays in views.py.  When D3 wires this in, views.py's
``backfill_last_narration_block`` should:

  1. Call ``pg_narrative_store.read_narration_backfill(player_id=..., limit=...)``
     to get ``list[BackfillRow]``.
  2. For each row, call ``_build_message_for_kind(kind=row.kind,
     payload_json=row.payload_json, seq=row.seq)`` and collect non-None results.
  3. Return that assembled ``list[object]``.

The ``_build_message_for_kind`` import and call remain in views.py —
the adapter is pure-SQL, not protocol-aware.

``generate_recap`` note
------------------------
``_generate_recap`` in persistence.py requires (entries, character_names,
party_location, known_facts).  A narrative-only store does not have access
to character names, party location, or known facts — those live on
``GameSnapshot``.  This matches ``SqliteStore.generate_recap`` exactly: that
method also omits those args and instead calls a simplified local recap that
just bulleted the entries.  We mirror it faithfully here: use the local
fallback recap (entries only).  The richer ``_generate_recap`` call with
character data happens in ``PgSnapshotStore.load_snapshot`` (A4), which does
have the snapshot in hand — same split as the SQLite side.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg_pool import ConnectionPool

from sidequest.game.pg._conn import session_tx
from sidequest.game.session import NarrativeEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BackfillRow — typed result from read_narration_backfill
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackfillRow:
    """One row returned by :meth:`PgNarrativeStore.read_narration_backfill`.

    ``seq``         — event sequence number (from ``events.seq``).
    ``kind``        — event kind (``'NARRATION'`` or ``'CHAPTER_MARKER'``).
    ``payload_json``— the player-projected payload from ``projection_cache``.
                      Only rows with ``include=True`` and non-NULL payload
                      are included; callers do not need to filter.

    D3 wiring: pass each row to ``_build_message_for_kind(kind, payload_json, seq)``
    in views.py to obtain the final protocol message objects.
    """

    seq: int
    kind: str
    payload_json: str


# ---------------------------------------------------------------------------
# PgNarrativeStore
# ---------------------------------------------------------------------------


class PgNarrativeStore:
    """Postgres adapter for the ``narrative_log`` table (ADR-115 A5).

    All writes run inside ``session_tx`` (per-session row lock, commit on
    clean exit) — the same serialisation guarantee as SAVE_WRITE_LOCK on
    the SQLite side, but scoped to one session.

    Reads use bare pooled connections (no row lock needed for reads).
    """

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._session_id = session_id

    # ------------------------------------------------------------------
    # append_narrative
    # ------------------------------------------------------------------

    def append_narrative(self, entry: NarrativeEntry) -> None:
        """Append a narrative entry to ``narrative_log``.

        Mirrors ``SqliteStore.append_narrative`` exactly:
          - ``tags`` serialized as ``json.dumps(entry.tags)``
          - ``created_at`` = ISO-8601 UTC now
          - Write inside ``session_tx``
        """
        tags_json = json.dumps(entry.tags)
        created_at = datetime.now(tz=UTC).isoformat()
        with session_tx(self._pool, self._session_id) as conn:
            conn.execute(
                """
                INSERT INTO narrative_log
                    (session_id, round_number, author, content, tags, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    self._session_id,
                    entry.round,
                    entry.author,
                    entry.content,
                    tags_json,
                    created_at,
                ),
            )

    # ------------------------------------------------------------------
    # max_narrative_round
    # ------------------------------------------------------------------

    def max_narrative_round(self) -> int:
        """Return MAX(round_number) from narrative_log, or 0 when empty.

        Mirrors ``SqliteStore.max_narrative_round``.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(round_number), 0) FROM narrative_log WHERE session_id = %s",
                (self._session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # recent_narrative
    # ------------------------------------------------------------------

    def recent_narrative(self, limit: int) -> list[NarrativeEntry]:
        """Return the ``limit`` most-recent entries, oldest-first.

        Mirrors ``SqliteStore.recent_narrative`` and
        ``PgSnapshotStore._recent_narrative`` exactly — the sub-query
        selects the top-N by descending ``id`` then sorts ascending so
        the caller sees oldest-to-newest.

        Tags: ``json.loads(tags_json)`` with empty-list fallback on
        decode error, matching SqliteStore exactly.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT round_number, author, content, tags
                FROM (
                    SELECT id, round_number, author, content, tags
                    FROM narrative_log
                    WHERE session_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) sub
                ORDER BY id ASC
                """,
                (self._session_id, limit),
            ).fetchall()

        entries: list[NarrativeEntry] = []
        for row in rows:
            tags_json = row[3] or "[]"
            try:
                tags = json.loads(tags_json)
            except Exception:
                tags = []
            # narrative_log stores only round/author/content/tags — so
            # encounter_tags/speaker/entry_type take their model defaults here
            # (mirrors SqliteStore.recent_narrative).
            entries.append(
                NarrativeEntry(
                    timestamp=0,
                    round=row[0],
                    author=row[1],
                    content=row[2],
                    tags=tags,
                )
            )
        return entries

    # ------------------------------------------------------------------
    # generate_recap
    # ------------------------------------------------------------------

    def generate_recap(self) -> str | None:
        """Generate a simplified 'Previously On...' recap from recent entries.

        Mirrors ``SqliteStore.generate_recap`` — uses only the narrative
        entries (no character names / party_location / known_facts) because
        those require a loaded GameSnapshot.  The richer
        ``_generate_recap(entries, character_names, location, known_facts)``
        call lives in ``PgSnapshotStore.load_snapshot`` (A4), which has the
        snapshot in hand — same split as the SQLite side.
        """
        entries = self.recent_narrative(3)
        if not entries:
            return None
        recap = "## Previously On…\n\n"
        for entry in entries:
            content = entry.content
            if len(content) > 200:
                content = content[:200] + "..."
            recap += f"- {content}\n"
        return recap

    # ------------------------------------------------------------------
    # read_narration_backfill
    # ------------------------------------------------------------------

    def read_narration_backfill(self, *, player_id: str, limit: int) -> list[BackfillRow]:
        """Fetch the last ``limit`` NARRATIONs and interleaved CHAPTER_MARKERs
        from the event log, filtered through the projection cache for this player.

        Translates the SQL reads in ``views.backfill_last_narration_block``
        to Postgres (``?``→``%s``, ``session_id = %s`` added to every WHERE).
        Reads 1 (oldest NARRATION seq) and 2 (the CHAPTER_MARKER /
        COALESCE(MAX(seq)…) marker subquery) keep their verbatim shape; the
        prior Read-3 event fetch + per-row Read-4 projection lookup are
        collapsed into ONE inner JOIN (N+1 avoidance on the reconnect hot path).

        Returns ``list[BackfillRow]`` in seq-ascending order (chapter markers
        before their narration).  Only rows where ``projection_cache.include=1``
        and ``payload_json IS NOT NULL`` are included — callers do not need to
        filter.  Returns an empty list when no narrations exist or ``limit <= 0``.

        D3 wiring contract
        ------------------
        In ``views.backfill_last_narration_block``, replace the four
        ``store._conn.execute(...)`` reads and the ``_cached_payload``
        inner function with a single call to this method, then iterate
        the returned ``BackfillRow`` list, calling
        ``_build_message_for_kind(kind=row.kind, payload_json=row.payload_json,
        seq=row.seq)`` on each row and collecting non-None results.  The
        ``_build_message_for_kind`` import and the ``None`` skip remain in
        views.py — the adapter is protocol-layer-agnostic.
        """
        if limit <= 0:
            return []

        with self._pool.connection() as conn:
            # ----------------------------------------------------------------
            # Read 1: find the Nth-most-recent NARRATION seq (oldest in window)
            # ----------------------------------------------------------------
            narration_seq_rows = conn.execute(
                "SELECT seq FROM events "
                "WHERE session_id = %s AND kind = 'NARRATION' "
                "ORDER BY seq DESC LIMIT %s",
                (self._session_id, limit),
            ).fetchall()

        if not narration_seq_rows:
            return []

        oldest_narration_seq = int(narration_seq_rows[-1][0])

        with self._pool.connection() as conn:
            # ----------------------------------------------------------------
            # Read 2: CHAPTER_MARKER immediately before the oldest NARRATION in
            # the window, not crossing an even-earlier NARRATION.
            # Mirrors the COALESCE(MAX(seq)…) subquery from views.py verbatim.
            # ----------------------------------------------------------------
            chapter_row = conn.execute(
                "SELECT seq FROM events "
                "WHERE session_id = %s AND kind = 'CHAPTER_MARKER' AND seq < %s "
                "  AND seq > COALESCE("
                "    (SELECT MAX(seq) FROM events "
                "     WHERE session_id = %s AND kind = 'NARRATION' AND seq < %s),"
                "    0"
                "  ) "
                "ORDER BY seq DESC LIMIT 1",
                (
                    self._session_id,
                    oldest_narration_seq,
                    self._session_id,
                    oldest_narration_seq,
                ),
            ).fetchone()

        lower_bound = oldest_narration_seq
        if chapter_row is not None:
            lower_bound = int(chapter_row[0])

        with self._pool.connection() as conn:
            # ----------------------------------------------------------------
            # Reads 3+4 collapsed into ONE query (N+1 avoidance — this fires on
            # every browser reconnect once D3 wires it into views.backfill).
            #
            # The INNER JOIN on projection_cache (PK (session_id, event_seq,
            # player_id) → at most one row per event) plus the include=1 /
            # payload_json IS NOT NULL filter is semantically identical to the
            # prior Read-3 fetch + per-row Read-4 lookup-and-skip loop:
            #   - events with no cache row for this player → dropped by INNER JOIN
            #   - cache rows with include=0 → dropped by pc.include = 1
            #   - cache rows with NULL payload → dropped by IS NOT NULL
            # ``include`` is INTEGER 1/0 (A3 write encoding), so pc.include = 1.
            # ----------------------------------------------------------------
            rows = conn.execute(
                "SELECT e.seq, e.kind, pc.payload_json "
                "FROM events e "
                "JOIN projection_cache pc "
                "  ON pc.session_id = e.session_id "
                " AND pc.player_id = %s "
                " AND pc.event_seq = e.seq "
                "WHERE e.session_id = %s "
                "  AND e.kind IN ('NARRATION', 'CHAPTER_MARKER') "
                "  AND e.seq >= %s "
                "  AND pc.include = 1 "
                "  AND pc.payload_json IS NOT NULL "
                "ORDER BY e.seq ASC",
                (player_id, self._session_id, lower_bound),
            ).fetchall()

        return [
            BackfillRow(seq=int(seq_raw), kind=str(kind), payload_json=str(payload_json))
            for seq_raw, kind, payload_json in rows
        ]
