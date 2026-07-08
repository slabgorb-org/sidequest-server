"""PgDungeonRepository — dungeon persistence over the shared Postgres pool (ADR-115 B1).

Ports every DungeonStore method to Postgres (no SQLite dependency).
The schema is Alembic-owned; ``ensure_schema`` is intentionally absent.

Key dialect translations vs. SQLite DungeonStore
-------------------------------------------------
- All queries are keyed by ``(session_id, …)`` — the SQLite store uses a
  single-tenant file with no session_id column.
- ``dungeon_meta`` PK is ``session_id`` (BIGINT), not ``id=1``.
- ``set_campaign_seed`` enforces write-once by ``get_campaign_seed()`` first
  (raising ``PersistError`` if already set), then a plain ``INSERT``; a
  concurrent-writer race that slips past the get is caught as
  ``psycopg.errors.UniqueViolation`` → ``PersistError``.
- ``commit_expansion`` uses ``executemany`` for batch node/edge inserts.
  ``psycopg.errors.UniqueViolation`` / ``IntegrityError`` → ``PersistError``
  (freeze violation, same as today).
- ``put_frontier`` SQLite ``INSERT OR REPLACE`` →
  ``INSERT … ON CONFLICT (session_id, frontier_edge_id) DO UPDATE SET …``.
- ``resolve_thread`` SQLite ``datetime('now')`` → Python ISO string param.
- Payload serialisation is byte-for-byte identical to DungeonStore:
  ``json.dumps(<model>.to_dict())`` for nodes, edges, frontier, mutations,
  and threads.  Mask BLOB is ``json.dumps(mask_dict).encode("utf-8")`` →
  BYTEA; decode path is ``.decode("utf-8")`` → ``json.loads``.

transaction() contract for D6
------------------------------
``transaction()`` yields a ``PgDungeonTransaction`` bound to ONE locked
connection (via ``session_tx``).  D6's materializer must replace its current
``conn = persistence._conn`` + ``conn.commit()`` / ``conn.rollback()`` pattern
with::

    with dungeon_repo.transaction() as tx:
        tx.commit_expansion(seed_exp, graph)
        tx.commit_expansion(expansion, graph, masks=masks)
        tx.record_mutation(region_id, kind, payload)
        tx.put_frontier(fe)
    # commits on clean exit; rolls back on PersistError

``PgDungeonTransaction`` exposes exactly the mutating methods materializer uses
inside one transaction boundary.  The non-transactional methods on the outer
repo (``load_map``, ``load_frontier``, ``get_campaign_seed``, etc.) borrow
a pooled connection each call — safe for concurrent reads.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
from psycopg_pool import ConnectionPool

from sidequest.dungeon.persistence import GENERATOR_VERSION as _DEFAULT_GENERATOR_VERSION
from sidequest.dungeon.persistence import (
    ComplicationThread,
    DungeonMutation,
    FrontierEdge,
)
from sidequest.dungeon.region_graph.model import (
    Expansion,
    RegionEdge,
    RegionGraph,
    RegionNode,
)
from sidequest.game.persistence import (
    DatabaseError,
    NotFoundError,
    PersistError,
    SerializationError,
)
from sidequest.game.pg._conn import session_tx
from sidequest.telemetry.spans.dungeon_persist import (
    dungeon_persist_commit_span,
    ledger_add_span,
    ledger_resolve_span,
    mask_load_span,
    mask_write_span,
)

# Per-site storage key (Track B, Story 164-1). Every dungeon table is keyed
# ``(session_id, site_id, …)``; a call that omits ``site_id`` lands under this
# legacy default, so Sünden's existing single-dungeon path is unchanged.
# Must match alembic 0003's ``_SITE``.
DEFAULT_SITE_ID = "frontier"

__all__ = ["DEFAULT_SITE_ID", "PgDungeonRepository", "PgDungeonTransaction"]


# ---------------------------------------------------------------------------
# Transaction (unit-of-work bound to one locked connection)
# ---------------------------------------------------------------------------


class PgDungeonTransaction:
    """Mutating dungeon operations bound to ONE locked Postgres connection.

    Do not instantiate directly; obtain via ``PgDungeonRepository.transaction()``.
    All methods execute but do NOT commit — the ``transaction()`` context manager
    commits on clean exit and rolls back on exception (including ``PersistError``).
    """

    def __init__(self, conn: psycopg.Connection, session_id: int) -> None:
        self._conn = conn
        self._sid = session_id

    def commit_expansion(
        self,
        expansion: Expansion,
        graph: RegionGraph,
        *,
        generator_version: str = _DEFAULT_GENERATOR_VERSION,
        masks: Mapping[str, dict] | None = None,
        site_id: str = DEFAULT_SITE_ID,
    ) -> None:
        """Persist one expansion's nodes + edges (no commit — caller owns boundary)."""
        with dungeon_persist_commit_span(
            expansion_id=expansion.expansion_id,
            regions=len(expansion.new_nodes),
            edges=len(expansion.new_edges),
            generator_version=generator_version,
        ):
            now = datetime.now(tz=UTC).isoformat()
            node_rows = []
            for node in expansion.new_nodes:
                live = graph.nodes.get(node.id)
                if live is None:
                    raise NotFoundError(
                        f"expansion region {node.id!r} is not in the graph "
                        f"(commit must run after attach_expansion)"
                    )
                mask_blob: bytes | None = None
                if masks is not None and live.id in masks:
                    try:
                        mask_blob = json.dumps(masks[live.id], sort_keys=True).encode("utf-8")
                    except (TypeError, ValueError) as exc:
                        raise PersistError(
                            f"mask for region {live.id!r} is not JSON-serialisable: {exc}"
                        ) from exc
                node_rows.append(
                    (
                        self._sid,
                        site_id,
                        live.id,
                        live.expansion_id,
                        live.depth_score,
                        generator_version,
                        json.dumps(live.to_dict()),
                        mask_blob,
                        now,
                    )
                )

            edge_rows = [
                (
                    self._sid,
                    site_id,
                    expansion.expansion_id,
                    edge.a,
                    edge.b,
                    edge.kind,
                    int(edge.hidden),
                    int(edge.shortcut),
                    json.dumps(edge.to_dict()),
                    now,
                )
                for edge in expansion.new_edges
            ]

            try:
                with self._conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO dungeon_map "
                        "(session_id, site_id, region_id, expansion_id, depth_score, generator_version, "
                        " payload, mask, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        node_rows,
                    )
                    cur.executemany(
                        "INSERT INTO dungeon_edge "
                        "(session_id, site_id, expansion_id, a, b, kind, hidden, shortcut, payload, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        edge_rows,
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise PersistError(
                    f"dungeon expansion {expansion.expansion_id} re-commit "
                    f"violates the freeze contract: {exc}"
                ) from exc
            except psycopg.errors.IntegrityError as exc:
                raise PersistError(
                    f"dungeon expansion {expansion.expansion_id} integrity error: {exc}"
                ) from exc
            except psycopg.Error as exc:
                raise DatabaseError(f"commit_expansion failed: {exc}") from exc

        if masks is not None:
            with mask_write_span(mask_rows=len(masks)):
                pass

    def put_frontier(self, fe: FrontierEdge, *, site_id: str = DEFAULT_SITE_ID) -> None:
        """Upsert a frontier edge (INSERT … ON CONFLICT … DO UPDATE)."""
        now = datetime.now(tz=UTC).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO dungeon_frontier "
                "(session_id, site_id, frontier_edge_id, from_region_id, heading, spawn_depth_score, "
                " payload, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (session_id, site_id, frontier_edge_id) DO UPDATE SET "
                "  from_region_id = excluded.from_region_id, "
                "  heading = excluded.heading, "
                "  spawn_depth_score = excluded.spawn_depth_score, "
                "  payload = excluded.payload",
                (
                    self._sid,
                    site_id,
                    fe.frontier_edge_id,
                    fe.from_region_id,
                    fe.heading,
                    fe.spawn_depth_score,
                    json.dumps(fe.to_dict()),
                    now,
                ),
            )
        except psycopg.Error as exc:
            raise DatabaseError(f"put_frontier failed: {exc}") from exc

    def record_mutation(
        self, region_id: str, kind: str, payload: dict, *, site_id: str = DEFAULT_SITE_ID
    ) -> None:
        """Append one mutation fact (append-only, never updated or deleted)."""
        now = datetime.now(tz=UTC).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO dungeon_mutation_overlay "
                "(session_id, site_id, region_id, kind, payload, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (self._sid, site_id, region_id, kind, json.dumps(payload), now),
            )
        except psycopg.Error as exc:
            raise DatabaseError(f"record_mutation failed: {exc}") from exc

    # ------------------------------------------------------------------
    # complication ledger (ADR-115 D6 atomicity fix)
    #
    # These mirror PgDungeonRepository.open_thread / open_threads byte-for-byte
    # (same SQL, same ledger.add span, same duplicate-thread_id loud raise) but
    # execute on the transaction's OWN locked connection (self._conn) WITHOUT
    # opening a fresh session_tx. attach_set_piece writes its threads through
    # these so a later commit_expansion PersistError rolls them back together
    # with the expansion (Plan-5 atomicity: NO orphan ledger).
    # ------------------------------------------------------------------

    def open_thread(self, thread: ComplicationThread, *, site_id: str = DEFAULT_SITE_ID) -> None:
        """Insert a new complication thread (status='open') on this txn's conn."""
        with ledger_add_span(
            thread_id=thread.thread_id,
            kind=thread.kind,
            origin_region_id=thread.origin_region_id,
        ):
            now = datetime.now(tz=UTC).isoformat()
            try:
                self._conn.execute(
                    "INSERT INTO dungeon_complication_ledger "
                    "(session_id, site_id, thread_id, origin_region_id, kind, status, "
                    " started_at_depth_score, payload, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        self._sid,
                        site_id,
                        thread.thread_id,
                        thread.origin_region_id,
                        thread.kind,
                        thread.status,
                        thread.started_at_depth_score,
                        json.dumps(thread.payload),
                        now,
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise PersistError(f"thread {thread.thread_id!r} already open: {exc}") from exc
            except psycopg.Error as exc:
                raise DatabaseError(f"open_thread failed: {exc}") from exc

    def open_threads(self, *, site_id: str = DEFAULT_SITE_ID) -> list[ComplicationThread]:
        """Return all status='open' threads (ORDER BY thread_id) on this txn's conn.

        Reads on ``self._conn`` so dedup/coalescence within one attach pass sees
        threads opened earlier in the SAME transaction (read-your-writes).
        """
        rows = self._conn.execute(
            "SELECT thread_id, origin_region_id, kind, status, "
            "started_at_depth_score, payload "
            "FROM dungeon_complication_ledger "
            "WHERE session_id = %s AND site_id = %s AND status = 'open' ORDER BY thread_id",
            (self._sid, site_id),
        ).fetchall()
        try:
            return [
                ComplicationThread(
                    thread_id=r[0],
                    origin_region_id=r[1],
                    kind=r[2],
                    status=r[3],
                    started_at_depth_score=r[4],
                    payload=json.loads(r[5]),
                )
                for r in rows
            ]
        except json.JSONDecodeError as exc:
            raise SerializationError(f"corrupt thread payload: {exc}") from exc


# ---------------------------------------------------------------------------
# Repository (outer facade; reads borrow from pool, writes via transaction)
# ---------------------------------------------------------------------------


class PgDungeonRepository:
    """Dungeon persistence over the shared Postgres pool (ADR-115 B1).

    Instantiate with a ``ConnectionPool`` and the integer ``session_id`` from
    the ``sessions`` table.  The ``session_id`` scopes every query so multiple
    active sessions share one pool without cross-contamination.

    Mutating methods (``set_campaign_seed``, ``commit_expansion``,
    ``put_frontier``, ``record_mutation``, ``open_thread``, ``resolve_thread``)
    each open and close their own ``session_tx`` transaction.  For D6's
    materializer, use ``transaction()`` to wrap multiple writes atomically.
    """

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

    # ------------------------------------------------------------------
    # Unit-of-work (D6 seam)
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[PgDungeonTransaction]:
        """Yield a ``PgDungeonTransaction`` bound to one locked connection.

        Commits on clean exit; rolls back on any exception (including
        ``PersistError``).  Mirrors the ``PgSaveRepository.transaction()``
        pattern so D6's materializer can do::

            with dungeon_repo.transaction() as tx:
                tx.commit_expansion(seed_exp, graph)
                tx.commit_expansion(expansion, graph, masks=masks)
                tx.record_mutation(region_id, "setpiece_state", payload)
                tx.put_frontier(fe)
        """
        with session_tx(self._pool, self._sid) as conn:
            yield PgDungeonTransaction(conn, self._sid)

    # ------------------------------------------------------------------
    # campaign_seed — write-once
    # ------------------------------------------------------------------

    def get_campaign_seed(self, *, site_id: str = DEFAULT_SITE_ID) -> int | None:
        """Return the persisted campaign seed, or ``None`` on a fresh session."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT campaign_seed FROM dungeon_meta WHERE session_id = %s AND site_id = %s",
                (self._sid, site_id),
            ).fetchone()
        return None if row is None else int(row[0])

    def set_campaign_seed(self, seed: int, *, site_id: str = DEFAULT_SITE_ID) -> None:
        """Persist the campaign seed exactly once per site (write-once).

        Raises ``PersistError`` on a second write for the same site — the seed
        is frozen with the dungeon (save-is-truth).  Does NOT autocommit (uses
        its own ``session_tx``).
        """
        # Check first (same pattern as DungeonStore — explicit get then insert)
        if self.get_campaign_seed(site_id=site_id) is not None:
            raise PersistError(
                "campaign_seed already set — it is write-once "
                "(save-is-truth); refusing to overwrite a frozen seed"
            )
        now = datetime.now(tz=UTC).isoformat()
        try:
            with session_tx(self._pool, self._sid) as conn:
                conn.execute(
                    "INSERT INTO dungeon_meta (session_id, site_id, campaign_seed, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (self._sid, site_id, seed, now),
                )
        except psycopg.errors.UniqueViolation as exc:
            # Race: another writer won between our get and insert
            raise PersistError(
                "campaign_seed already set — it is write-once "
                "(save-is-truth); refusing to overwrite a frozen seed"
            ) from exc
        except psycopg.Error as exc:
            raise DatabaseError(f"dungeon_meta write failed: {exc}") from exc

    # ------------------------------------------------------------------
    # commit_expansion (single-write path — delegates to a tx internally)
    # ------------------------------------------------------------------

    def commit_expansion(
        self,
        expansion: Expansion,
        graph: RegionGraph,
        *,
        generator_version: str = _DEFAULT_GENERATOR_VERSION,
        masks: Mapping[str, dict] | None = None,
        site_id: str = DEFAULT_SITE_ID,
    ) -> None:
        """Persist one expansion's regions + edges in their own transaction.

        For atomic multi-expansion writes (D6 materializer), use
        ``transaction()`` and call ``tx.commit_expansion()`` instead.
        """
        with session_tx(self._pool, self._sid) as conn:
            PgDungeonTransaction(conn, self._sid).commit_expansion(
                expansion,
                graph,
                generator_version=generator_version,
                masks=masks,
                site_id=site_id,
            )

    # ------------------------------------------------------------------
    # load_map / load_masks
    # ------------------------------------------------------------------

    def load_map(self, *, entrance_id: str, site_id: str = DEFAULT_SITE_ID) -> RegionGraph:
        """Rebuild the full RegionGraph from dungeon_map + dungeon_edge for one site.

        Nodes are loaded first; RegionGraph.add_edge validates endpoints loudly.
        """
        with self._pool.connection() as conn:
            node_rows = conn.execute(
                "SELECT payload FROM dungeon_map WHERE session_id = %s AND site_id = %s",
                (self._sid, site_id),
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT payload FROM dungeon_edge "
                "WHERE session_id = %s AND site_id = %s ORDER BY edge_id",
                (self._sid, site_id),
            ).fetchall()

        g = RegionGraph(entrance_id=entrance_id)
        try:
            for r in node_rows:
                g.add_node(RegionNode.from_dict(json.loads(r[0])))
            for r in edge_rows:
                g.add_edge(RegionEdge.from_dict(json.loads(r[0])))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise SerializationError(f"corrupt dungeon payload: {exc}") from exc
        return g

    def load_masks(self, *, site_id: str = DEFAULT_SITE_ID) -> dict[str, dict]:
        """Return persisted region masks as ``{region_id: mask_dict}`` for one site.

        Rows whose ``mask BYTEA`` is NULL are omitted — absence means "no mask
        known", not "empty mask".  A fresh session returns ``{}``.  A corrupted
        BLOB raises ``SerializationError`` (No Silent Fallbacks).
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT region_id, mask FROM dungeon_map "
                "WHERE session_id = %s AND site_id = %s AND mask IS NOT NULL ORDER BY region_id",
                (self._sid, site_id),
            ).fetchall()

        masks: dict[str, dict] = {}
        try:
            for r in rows:
                masks[r[0]] = json.loads(r[1].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            raise SerializationError(f"corrupt dungeon mask BLOB: {exc}") from exc

        with mask_load_span(mask_rows=len(masks)):
            pass
        return masks

    # ------------------------------------------------------------------
    # put_frontier / load_frontier
    # ------------------------------------------------------------------

    def put_frontier(self, fe: FrontierEdge, *, site_id: str = DEFAULT_SITE_ID) -> None:
        """Upsert a frontier edge (INSERT … ON CONFLICT … DO UPDATE)."""
        with session_tx(self._pool, self._sid) as conn:
            PgDungeonTransaction(conn, self._sid).put_frontier(fe, site_id=site_id)

    def load_frontier(self, *, site_id: str = DEFAULT_SITE_ID) -> list[FrontierEdge]:
        """Return all frontier edges ordered by ``frontier_edge_id`` for one site."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM dungeon_frontier "
                "WHERE session_id = %s AND site_id = %s ORDER BY frontier_edge_id",
                (self._sid, site_id),
            ).fetchall()
        try:
            return [FrontierEdge.from_dict(json.loads(r[0])) for r in rows]
        except (json.JSONDecodeError, KeyError) as exc:
            raise SerializationError(f"corrupt frontier payload: {exc}") from exc

    # ------------------------------------------------------------------
    # record_mutation / load_mutations
    # ------------------------------------------------------------------

    def record_mutation(
        self, region_id: str, kind: str, payload: dict, *, site_id: str = DEFAULT_SITE_ID
    ) -> None:
        """Append one mutation fact in its own transaction."""
        with session_tx(self._pool, self._sid) as conn:
            PgDungeonTransaction(conn, self._sid).record_mutation(
                region_id, kind, payload, site_id=site_id
            )

    def load_mutations(self, *, site_id: str = DEFAULT_SITE_ID) -> list[DungeonMutation]:
        """Return all mutations ordered by ``mutation_id`` (append-only replay order)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT region_id, kind, payload FROM dungeon_mutation_overlay "
                "WHERE session_id = %s AND site_id = %s ORDER BY mutation_id",
                (self._sid, site_id),
            ).fetchall()
        try:
            return [
                DungeonMutation(
                    region_id=r[0],
                    kind=r[1],
                    payload=json.loads(r[2]),
                )
                for r in rows
            ]
        except json.JSONDecodeError as exc:
            raise SerializationError(f"corrupt mutation payload: {exc}") from exc

    # ------------------------------------------------------------------
    # open_thread / get_thread / open_threads / resolve_thread
    # ------------------------------------------------------------------

    def open_thread(self, thread: ComplicationThread, *, site_id: str = DEFAULT_SITE_ID) -> None:
        """Insert a new complication thread (status='open')."""
        with ledger_add_span(
            thread_id=thread.thread_id,
            kind=thread.kind,
            origin_region_id=thread.origin_region_id,
        ):
            now = datetime.now(tz=UTC).isoformat()
            try:
                with session_tx(self._pool, self._sid) as conn:
                    conn.execute(
                        "INSERT INTO dungeon_complication_ledger "
                        "(session_id, site_id, thread_id, origin_region_id, kind, status, "
                        " started_at_depth_score, payload, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            self._sid,
                            site_id,
                            thread.thread_id,
                            thread.origin_region_id,
                            thread.kind,
                            thread.status,
                            thread.started_at_depth_score,
                            json.dumps(thread.payload),
                            now,
                        ),
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise PersistError(f"thread {thread.thread_id!r} already open: {exc}") from exc
            except psycopg.Error as exc:
                raise DatabaseError(f"open_thread failed: {exc}") from exc

    def get_thread(self, thread_id: str, *, site_id: str = DEFAULT_SITE_ID) -> ComplicationThread:
        """Return the thread for ``thread_id``, raising ``NotFoundError`` if absent."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT thread_id, origin_region_id, kind, status, "
                "started_at_depth_score, payload "
                "FROM dungeon_complication_ledger "
                "WHERE session_id = %s AND site_id = %s AND thread_id = %s",
                (self._sid, site_id, thread_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"complication thread {thread_id!r} not found")
        try:
            return ComplicationThread(
                thread_id=row[0],
                origin_region_id=row[1],
                kind=row[2],
                status=row[3],
                started_at_depth_score=row[4],
                payload=json.loads(row[5]),
            )
        except json.JSONDecodeError as exc:
            raise SerializationError(f"corrupt thread payload: {exc}") from exc

    def resolve_thread(self, thread_id: str, *, site_id: str = DEFAULT_SITE_ID) -> None:
        """Mark a thread resolved; raises ``NotFoundError`` if ``thread_id`` is unknown."""
        with ledger_resolve_span(thread_id=thread_id):
            resolved_at = datetime.now(tz=UTC).isoformat()
            try:
                with session_tx(self._pool, self._sid) as conn:
                    result = conn.execute(
                        "UPDATE dungeon_complication_ledger "
                        "SET status = 'resolved', resolved_at = %s "
                        "WHERE session_id = %s AND site_id = %s AND thread_id = %s",
                        (resolved_at, self._sid, site_id, thread_id),
                    )
                    if result.rowcount == 0:
                        raise NotFoundError(
                            f"cannot resolve unknown complication thread {thread_id!r}"
                        )
            # NotFoundError is a sidequest PersistError, not a psycopg.Error, so it
            # propagates naturally past the psycopg.Error handler below.
            except psycopg.Error as exc:
                raise DatabaseError(f"resolve_thread failed: {exc}") from exc

    def open_threads(self, *, site_id: str = DEFAULT_SITE_ID) -> list[ComplicationThread]:
        """Return all threads with ``status='open'`` ordered by ``thread_id`` for one site."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT thread_id, origin_region_id, kind, status, "
                "started_at_depth_score, payload "
                "FROM dungeon_complication_ledger "
                "WHERE session_id = %s AND site_id = %s AND status = 'open' ORDER BY thread_id",
                (self._sid, site_id),
            ).fetchall()
        try:
            return [
                ComplicationThread(
                    thread_id=r[0],
                    origin_region_id=r[1],
                    kind=r[2],
                    status=r[3],
                    started_at_depth_score=r[4],
                    payload=json.loads(r[5]),
                )
                for r in rows
            ]
        except json.JSONDecodeError as exc:
            raise SerializationError(f"corrupt thread payload: {exc}") from exc
