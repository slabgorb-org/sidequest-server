"""PgSaveRepository — composite Postgres save store (ADR-115 A7).

Composes the A2-A6 sub-stores behind the full ``SaveRepository`` Protocol
surface.  One object; all sub-stores share the same ``pool`` and ``session_id``.

``transaction()`` yields a ``PgSaveTransaction`` bound to a single locked
connection so that event + projection (+ telemetry) writes in one turn are
atomic.

``close()`` is a no-op: under a shared psycopg ConnectionPool the pool
lifecycle is application-level (wired in F2); closing the pool here would
kill it for all sessions sharing the process.

``read_narration_backfill`` Protocol signature uses ``list[BackfillRow]``
(the real A5 return type) rather than ``list[object]``.  Importing
``BackfillRow`` from ``sidequest.game.pg.narrative`` into ``repository.py``
is under ``TYPE_CHECKING`` so there is no runtime import cycle — both modules
live in the same ``sidequest.game`` tree.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from sidequest.game.event_log import EventRow
from sidequest.game.lore_store import LoreFragment, LoreStore
from sidequest.game.persistence import SavedSession
from sidequest.game.pg import sessions
from sidequest.game.pg._conn import session_tx
from sidequest.game.pg.asset_ledger import PgAssetLedgerStore
from sidequest.game.pg.events import PgEventStore, PgSaveTransaction
from sidequest.game.pg.lore import PgLoreStore
from sidequest.game.pg.narrative import BackfillRow, PgNarrativeStore
from sidequest.game.pg.promotions import PgLocationPromotionRow, PgPromotionStore
from sidequest.game.pg.scrapbook import PgScrapbookStore
from sidequest.game.pg.sessions import GameRow
from sidequest.game.pg.snapshot import PgSnapshotStore
from sidequest.game.projection.cache import CachedDecision
from sidequest.game.projection_filter import FilterDecision
from sidequest.game.session import GameSnapshot, NarrativeEntry
from sidequest.game.world_save import WorldSave


class PgSaveRepository:
    """Full-surface Postgres save repository.

    Composes:
      - PgEventStore       → events + projection_cache
      - PgSnapshotStore    → game_state + world_save
      - PgNarrativeStore   → narrative_log
      - PgScrapbookStore   → scrapbook_entries
      - PgPromotionStore   → location_promotions

    All sub-stores are bound to ``(pool, session_id)``; ``session_id`` is
    the integer PK of the ``sessions`` table row created by ``for_slug``.

    Instantiate via ``PgSaveRepository.for_slug(...)``; do not call
    ``__init__`` directly.
    """

    def __init__(self, pool: ConnectionPool, *, session_id: int) -> None:
        self._pool = pool
        self._sid = session_id

        self._events = PgEventStore(pool, session_id=session_id)
        self._snapshot = PgSnapshotStore(pool, session_id=session_id)
        self._narrative = PgNarrativeStore(pool, session_id=session_id)
        self._scrapbook = PgScrapbookStore(pool, session_id=session_id)
        self._asset_ledger = PgAssetLedgerStore(pool, session_id=session_id)
        self._promotions = PgPromotionStore(pool, session_id=session_id)
        self._lore = PgLoreStore(pool, session_id=session_id)

    @property
    def session_id(self) -> int:
        """Public accessor for the bound session_id (ADR-115 D1)."""
        return self._sid

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def for_slug(
        cls,
        pool: ConnectionPool,
        *,
        slug: str,
        mode: str,
        genre_slug: str,
        world_slug: str,
    ) -> PgSaveRepository:
        """Ensure the sessions row exists and return a repository bound to it.

        ``ensure_session`` is idempotent on ``session_slug`` — safe to call
        on every server startup for an existing session.
        """
        session_id = sessions.ensure_session(
            pool, slug=slug, mode=mode, genre_slug=genre_slug, world_slug=world_slug
        )
        return cls(pool, session_id=session_id)

    # ------------------------------------------------------------------
    # Unit-of-work
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[PgSaveTransaction]:
        """Yield a PgSaveTransaction bound to a single locked connection.

        Commits on clean exit; rolls back on exception (including RuntimeError).
        The per-session row lock (SELECT … FOR UPDATE on the sessions row) is
        taken by session_tx so concurrent writers on the same session are
        serialized without a process-level lock.
        """
        with session_tx(self._pool, self._sid) as conn:
            yield PgSaveTransaction(conn, self._sid)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(self, *, kind: str, payload_json: str) -> EventRow:
        return self._events.append_event(kind=kind, payload_json=payload_json)

    def read_events_since(self, *, since_seq: int) -> list[EventRow]:
        return self._events.read_events_since(since_seq=since_seq)

    def latest_event_seq(self) -> int:
        return self._events.latest_event_seq()

    # ------------------------------------------------------------------
    # Projection cache
    # ------------------------------------------------------------------

    def write_projection(self, *, event_seq: int, player_id: str, decision: FilterDecision) -> None:
        self._events.write_projection(event_seq=event_seq, player_id=player_id, decision=decision)

    def read_projection_since(self, *, player_id: str, since_seq: int) -> list[CachedDecision]:
        return self._events.read_projection_since(player_id=player_id, since_seq=since_seq)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def save(self, snapshot: GameSnapshot) -> None:
        self._snapshot.save_snapshot(snapshot)

    def load(self) -> SavedSession | None:
        return self._snapshot.load_snapshot()

    def init_session(self) -> None:
        sessions.init_session(self._pool, session_id=self._sid)

    # ------------------------------------------------------------------
    # Lore (Story 75-15) — RAG fragment write-through + re-hydrate
    # ------------------------------------------------------------------

    def save_lore_fragments(self, lore_store: LoreStore) -> int:
        """Write the in-memory lore_store's fragments through to Postgres.

        Idempotent upsert by id — fragments accrete, never evict. Returns the
        number of fragments written. Called from the per-turn + disconnect save
        so creation-seed and runtime-accreted fragments survive resume.
        """
        return self._lore.upsert_fragments(lore_store.fragments_iter())

    def load_lore_fragments(self) -> list[LoreFragment]:
        """Re-hydrate persisted lore fragments for this session.

        Returned fragments carry ``embedding_pending=True`` so the per-turn
        embed worker re-embeds them on the next turn.
        """
        return self._lore.load_fragments()

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def append_narrative(self, entry: NarrativeEntry) -> None:
        self._narrative.append_narrative(entry)

    def max_narrative_round(self) -> int:
        return self._narrative.max_narrative_round()

    def recent_narrative(self, limit: int) -> list[NarrativeEntry]:
        return self._narrative.recent_narrative(limit)

    def generate_recap(self) -> str | None:
        return self._narrative.generate_recap()

    def read_narration_backfill(self, *, player_id: str, limit: int) -> list[BackfillRow]:
        return self._narrative.read_narration_backfill(player_id=player_id, limit=limit)

    # ------------------------------------------------------------------
    # World save (hub state)
    # ------------------------------------------------------------------

    def load_world_save(self) -> WorldSave:
        return self._snapshot.load_world_save()

    def save_world_save(self, ws: WorldSave) -> None:
        self._snapshot.save_world_save(ws)

    # ------------------------------------------------------------------
    # Location promotions
    # ------------------------------------------------------------------

    def list_location_promotions(
        self, *, region_id: str | None = None, region_ids: list[str] | None = None
    ) -> list[PgLocationPromotionRow]:
        return self._promotions.list_location_promotions(region_id=region_id, region_ids=region_ids)

    def upsert_location_promotion(self, row: PgLocationPromotionRow) -> None:
        self._promotions.upsert_location_promotion(row)

    # ------------------------------------------------------------------
    # Scrapbook
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
        self._scrapbook.append_scrapbook_entry(
            turn_id=turn_id,
            scene_title=scene_title,
            scene_type=scene_type,
            location=location,
            image_url=image_url,
            narrative_excerpt=narrative_excerpt,
            world_facts=world_facts,
            npcs_present=npcs_present,
            render_status=render_status,
        )

    def update_scrapbook_image_url(self, *, turn_id: int, image_url: str) -> bool:
        return self._scrapbook.update_scrapbook_image_url(turn_id=turn_id, image_url=image_url)

    # ------------------------------------------------------------------
    # Asset ledger (Story 65-2) — runtime R2 artifacts per save
    # ------------------------------------------------------------------

    def append_asset_ledger(
        self,
        *,
        r2_key: str,
        asset_type: str,
        entity_ref: str,
        created_turn: int,
    ) -> None:
        self._asset_ledger.append(
            r2_key=r2_key,
            asset_type=asset_type,
            entity_ref=entity_ref,
            created_turn=created_turn,
        )

    def list_asset_ledger(self) -> list[dict]:
        return self._asset_ledger.list_assets()

    def scrapbook_turn_ids(self, *, max_turn: int) -> set[int]:
        return self._scrapbook.scrapbook_turn_ids(max_turn=max_turn)

    def scrapbook_image_url_map(self) -> dict[int, str]:
        return self._scrapbook.scrapbook_image_url_map()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def ensure_session(self, *, slug: str, mode: str, genre_slug: str, world_slug: str) -> int:
        """Idempotent: create or touch the sessions row for ``slug``; return its session_id.

        FOOTGUN — read before calling on a bound repo:
          (a) ``for_slug`` already ensured the session this repo is bound to, so
              calling this with the SAME slug is redundant.
          (b) This does NOT rebind the repo: ``self._sid`` is unchanged. Passing a
              DIFFERENT slug here ensures/touches ANOTHER session's row and returns
              ITS id — which this repo then ignores. The repo keeps operating on its
              original bound session. That mismatch is almost never what a caller wants.

        TODO(D2): reconsider whether ``ensure_session`` belongs on the bound instance
        at all. Its real consumer is the ``rest.py`` POST /api/games lift in D2; the
        correct shape (bound method vs classmethod vs module function) depends on how
        that consumer creates/looks-up sessions. Decide then, not now.
        """
        return sessions.ensure_session(
            self._pool, slug=slug, mode=mode, genre_slug=genre_slug, world_slug=world_slug
        )

    def get_game(self, *, slug: str) -> GameRow | None:
        """Return the GameRow for the given slug, or None."""
        return sessions.get_game(self._pool, slug=slug)

    def close(self) -> None:
        """No-op: the ConnectionPool lifecycle is application-level (F2).

        Callers that previously called SqliteStore.close() can call this
        without killing the shared pool.
        """
