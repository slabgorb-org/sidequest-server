"""Postgres location_promotions adapter (ADR-115 A6).

Faithful port of:
  - SqliteStore.list_location_promotions   (sidequest/game/persistence.py:735)
  - SqliteStore.upsert_location_promotion  (sidequest/game/persistence.py:765)

Design decision — save_id vs session_id:
  The SQLite ``location_promotions`` table is keyed by ``(save_id, region_id,
  entity_id)`` where ``save_id`` is the file-path-derived slug of the .db
  file.  The Postgres table is keyed by ``(session_id, region_id, entity_id)``
  where ``session_id`` is the INTEGER primary key of the ``sessions`` table.

  ``LocationPromotionRow`` (in persistence.py) carries ``save_id`` because the
  SQLite importer path (Task-Group E) will read OLD SQLite rows via
  ``SqliteStore`` and must preserve that field.  We do NOT modify that
  dataclass — it is the SQLite contract.

  Instead we introduce ``PgLocationPromotionRow``, a minimal dataclass that
  omits ``save_id`` and carries no other extras.  This is the type used by:
    - PgPromotionStore (this module)
    - D2 consumer-lift: location_resolver.py + location_view.py must be
      updated to pass PgLocationPromotionRow (not LocationPromotionRow) when
      talking to the Postgres backend.  They must STOP passing save_id to the
      upsert call.  The D2 contract is:

        from sidequest.game.pg.promotions import PgLocationPromotionRow, PgPromotionStore
        # Build row WITHOUT save_id; pass to store.upsert_location_promotion(row)
        # Read via store.list_location_promotions(region_id=...) → list[PgLocationPromotionRow]

  The TG-E SQLite→Postgres importer bridges the two by reading
  ``LocationPromotionRow`` (has save_id) from SQLite and constructing a
  ``PgLocationPromotionRow`` (no save_id) for the INSERT.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from sidequest.game.pg._conn import session_tx


@dataclass
class PgLocationPromotionRow:
    """Location promotion row for the Postgres backend.

    Mirrors ``LocationPromotionRow`` from ``persistence.py`` exactly EXCEPT
    that ``save_id`` is absent — the Postgres table is keyed by the integer
    ``session_id`` held by ``PgPromotionStore``, not by the file-path slug.

    D2 contract: location_resolver.py and location_view.py must use THIS type
    (not ``LocationPromotionRow``) when writing to / reading from the Postgres
    backend.
    """

    region_id: str
    entity_id: str
    provenance: str  # 'yes_and_promoted' | 'yes_and_minted'
    label: str
    promoted_at_turn: int
    promoted_canon: str
    new_tier: str  # 'yes_and' in v1
    new_binding_kind: str | None
    new_binding_ref: str | None


class PgPromotionStore:
    """Postgres adapter for location_promotions (ADR-115 A6).

    Bound to one session; all queries carry a ``session_id = %s`` predicate.
    Writes run inside ``session_tx`` (per-session row lock, commits on exit).
    Reads use a plain pooled connection (lock-free).
    """

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

    def list_location_promotions(
        self, *, region_id: str | None = None, region_ids: list[str] | None = None
    ) -> list[PgLocationPromotionRow]:
        """Return promotion rows for one region (``region_id``) or several
        (``region_ids``) in a **single** query.

        Exactly one selector must be supplied — passing both, or neither, raises
        (No Silent Fallbacks). Both paths route through ``region_id = ANY(%s)`` so
        the batched read (Story 76-11, one round-trip for all discovered regions)
        and the single-region read (``location_view`` / ``location_resolver``)
        share one SQL shape. Ordered by ``promoted_at_turn ASC, entity_id ASC`` —
        mirrors ``SqliteStore.list_location_promotions`` ordering exactly; the
        batched consumer regroups by ``region_id`` itself.
        """
        if region_id is not None and region_ids is not None:
            raise ValueError("pass region_id OR region_ids, not both")
        if region_ids is not None:
            targets = list(region_ids)
        elif region_id is not None:
            targets = [region_id]
        else:
            raise ValueError("list_location_promotions requires region_id or region_ids")
        if not targets:
            return []
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT region_id, entity_id, provenance, label,
                       promoted_at_turn, promoted_canon, new_tier,
                       new_binding_kind, new_binding_ref
                  FROM location_promotions
                 WHERE session_id = %s
                   AND region_id = ANY(%s)
                 ORDER BY promoted_at_turn ASC, entity_id ASC
                """,
                (self._sid, targets),
            ).fetchall()
        return [
            PgLocationPromotionRow(
                region_id=row[0],
                entity_id=row[1],
                provenance=row[2],
                label=row[3],
                promoted_at_turn=row[4],
                promoted_canon=row[5],
                new_tier=row[6],
                new_binding_kind=row[7],
                new_binding_ref=row[8],
            )
            for row in rows
        ]

    def upsert_location_promotion(self, row: PgLocationPromotionRow) -> None:
        """Insert or update one promotion row.

        Conflict key is ``(session_id, region_id, entity_id)`` — mirrors
        ``SqliteStore.upsert_location_promotion``'s ``(save_id, region_id,
        entity_id)`` semantics with ``save_id`` replaced by ``session_id``.

        Re-engagement of the same entity updates all mutable columns in place
        rather than minting a duplicate row (ADR-109 §4.3, AC-3 in story 54-6).
        """
        with session_tx(self._pool, self._sid) as conn:
            conn.execute(
                """
                INSERT INTO location_promotions
                    (session_id, region_id, entity_id, provenance, label,
                     promoted_at_turn, promoted_canon, new_tier,
                     new_binding_kind, new_binding_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id, region_id, entity_id) DO UPDATE SET
                    provenance       = excluded.provenance,
                    label            = excluded.label,
                    promoted_at_turn = excluded.promoted_at_turn,
                    promoted_canon   = excluded.promoted_canon,
                    new_tier         = excluded.new_tier,
                    new_binding_kind = excluded.new_binding_kind,
                    new_binding_ref  = excluded.new_binding_ref
                """,
                (
                    self._sid,
                    row.region_id,
                    row.entity_id,
                    row.provenance,
                    row.label,
                    row.promoted_at_turn,
                    row.promoted_canon,
                    row.new_tier,
                    row.new_binding_kind,
                    row.new_binding_ref,
                ),
            )
