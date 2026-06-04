"""Postgres lore_fragments adapter (Story 75-15).

Write-through + re-hydrate for the per-session RAG lore store. Closes the
gulliver 2026-06-02 starve: ``_SessionData.lore_store`` was an in-memory
``field(default_factory=LoreStore)`` that nothing ever persisted, so every
resume world-reseeded to ~3 fragments and the 17 creation-seed fragments (plus
every runtime-accreted fragment from ``lore_accretion.accrete_facts_to_lore``)
were lost. The ``lore_fragments`` table existed in ``alembic 0001`` and was only
ever DELETE'd on reinit — there was no writer or reader.

Bound to one session; every query carries a ``session_id = %s`` predicate.
Writes run inside ``session_tx`` (per-session row lock); reads use a plain
pooled connection. Mirrors the ``PgScrapbookStore`` / ``PgNarrativeStore``
conventions.

Embedding vectors are NOT persisted (the table has no embedding column). A
re-hydrated fragment comes back with ``embedding_pending=True`` so the existing
per-turn embed worker re-embeds it on the next turn — correct per Story 75-15
AC1 (fragments survive + re-hydrate; re-embedding on resume is acceptable).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from sidequest.game.lore_store import LoreFragment
from sidequest.game.pg._conn import session_tx

logger = logging.getLogger(__name__)


class PgLoreStore:
    """Postgres adapter for ``lore_fragments`` (Story 75-15).

    Bound to one session. ``upsert_fragments`` is idempotent by
    ``(session_id, id)`` — fragments accrete and are never evicted
    (Diamonds-and-Coal / Living World), so the write is an upsert, never a
    delete-and-replace.
    """

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_fragments(self, fragments: Iterable[LoreFragment]) -> int:
        """Write each fragment through to ``lore_fragments`` (upsert by id).

        Returns the number of fragments written. ``created_at`` is set on
        first insert and preserved on update (the ``ON CONFLICT`` clause does
        not touch it). The embedding vector is intentionally not persisted —
        the column does not exist and re-embedding on resume is cheap and
        correct.
        """
        rows = list(fragments)
        if not rows:
            return 0
        now = datetime.now(tz=UTC).isoformat()
        with session_tx(self._pool, self._sid) as conn:
            for frag in rows:
                conn.execute(
                    """
                    INSERT INTO lore_fragments
                        (session_id, id, category, content, source,
                         turn_created, metadata_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, id) DO UPDATE SET
                        category = excluded.category,
                        content = excluded.content,
                        source = excluded.source,
                        turn_created = excluded.turn_created,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        self._sid,
                        frag.id,
                        frag.category,
                        frag.content,
                        frag.source,
                        frag.turn_created,
                        json.dumps(frag.metadata),
                        now,
                    ),
                )
        return len(rows)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def load_fragments(self) -> list[LoreFragment]:
        """Re-hydrate all persisted fragments for this session.

        Returned fragments carry ``embedding_pending=True`` (no embedding is
        persisted), so the per-turn embed worker re-embeds them. Ordered by id
        for deterministic re-seeding.
        """
        with self._pool.connection() as conn:
            db_rows = conn.execute(
                """
                SELECT id, category, content, source, turn_created, metadata_json
                  FROM lore_fragments
                 WHERE session_id = %s
                 ORDER BY id
                """,
                (self._sid,),
            ).fetchall()
        fragments: list[LoreFragment] = []
        for row in db_rows:
            frag_id, category, content, source, turn_created, metadata_json = row
            try:
                metadata = json.loads(metadata_json) if metadata_json else {}
            except (TypeError, json.JSONDecodeError) as exc:
                # No silent fallback: a corrupt metadata blob is a real data
                # bug — log it loudly, then keep the fragment (its content is
                # what matters for retrieval) with empty metadata.
                logger.warning(
                    "lore.load_fragment_metadata_corrupt session_id=%s id=%s error=%s",
                    self._sid,
                    frag_id,
                    exc,
                )
                metadata = {}
            try:
                fragments.append(
                    LoreFragment.new(
                        id=frag_id,
                        category=category,
                        content=content,
                        source=source,
                        turn_created=turn_created,
                        metadata={str(k): str(v) for k, v in metadata.items()},
                    )
                )
            except (ValueError, ValidationError) as exc:
                # Loud-skip a corrupt row (e.g. blank content the LoreFragment
                # validator rejects). One bad row must NOT abort the whole resume
                # — re-hydrate the rest (ADR-124 loud-skip fold). The valid
                # fragments are what matter for retrieval grounding.
                logger.error(
                    "lore.load_fragment_corrupt session_id=%s id=%s error=%s — skipping row",
                    self._sid,
                    frag_id,
                    exc,
                )
        return fragments
