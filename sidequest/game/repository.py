"""Persistence repository interface (ADR-115, Phase 0 → A7, B1).

Decouples save-store consumers from the concrete storage engine. The
``transaction()`` context manager is the unit-of-work seam: multiple
writes inside one ``with`` block commit atomically or roll back together,
replacing the prior ``*_in_transaction(conn=...)`` raw-connection passing.

Protocol growth log
-------------------
Slice 1a (initial):  append_event / read_events_since / latest_event_seq /
                     write_projection / read_projection_since / transaction()
A7 (this file):      Full typed surface — snapshots, narrative, scrapbook,
                     promotions, session lifecycle, write_telemetry.
B1 (this file):      DungeonRepository Protocol — dungeon persistence surface.

Compatibility note
------------------
``SqliteSaveRepository`` satisfies only the original Slice-1a surface.  It
is deleted in F1; the isinstance assertions in the existing test suite have
been updated to use ``PgSaveRepository`` for the full-surface check.  The
Slice-1a methods are still structurally satisfied by ``SqliteSaveRepository``
so all non-isinstance tests continue to pass against it.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sidequest.dungeon.persistence import (
        ComplicationThread,
        DungeonMutation,
        FrontierEdge,
    )
    from sidequest.dungeon.region_graph.model import Expansion, RegionGraph
    from sidequest.game.event_log import EventRow
    from sidequest.game.persistence import SavedSession
    from sidequest.game.pg.narrative import BackfillRow
    from sidequest.game.pg.promotions import PgLocationPromotionRow
    from sidequest.game.pg.sessions import GameRow
    from sidequest.game.projection.cache import CachedDecision
    from sidequest.game.projection_filter import FilterDecision
    from sidequest.game.session import GameSnapshot, NarrativeEntry
    from sidequest.game.world_save import WorldSave


@runtime_checkable
class SaveTransaction(Protocol):
    """A unit of work. Operations do NOT commit individually; the owning
    ``transaction()`` context manager commits on clean exit and rolls back
    on exception."""

    def append_event(self, *, kind: str, payload_json: str) -> EventRow: ...

    def write_projection(
        self, *, event_seq: int, player_id: str, decision: FilterDecision
    ) -> None: ...

    def write_telemetry(
        self,
        *,
        event_seq: int | None,
        round: int | None,
        ts: str,
        component: str,
        event_type: str,
        payload_json: str,
    ) -> None: ...


@runtime_checkable
class SaveRepository(Protocol):
    """Storage-engine-agnostic save store. Full typed surface (ADR-115 A7).

    SQLite today (Slice-1a subset only); Postgres via PgSaveRepository.
    """

    def transaction(self) -> AbstractContextManager[SaveTransaction]: ...

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(self, *, kind: str, payload_json: str) -> EventRow: ...

    def read_events_since(self, *, since_seq: int) -> list[EventRow]: ...

    def latest_event_seq(self) -> int: ...

    # ------------------------------------------------------------------
    # Projection cache
    # ------------------------------------------------------------------

    def write_projection(
        self, *, event_seq: int, player_id: str, decision: FilterDecision
    ) -> None: ...

    def read_projection_since(self, *, player_id: str, since_seq: int) -> list[CachedDecision]: ...

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def save(self, snapshot: GameSnapshot) -> None: ...

    def load(self) -> SavedSession | None: ...

    def init_session(self) -> None: ...

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def append_narrative(self, entry: NarrativeEntry) -> None: ...

    def max_narrative_round(self) -> int: ...

    def recent_narrative(self, limit: int) -> list[NarrativeEntry]: ...

    def generate_recap(self) -> str | None: ...

    def read_narration_backfill(self, *, player_id: str, limit: int) -> list[BackfillRow]: ...

    # ------------------------------------------------------------------
    # World save (hub state)
    # ------------------------------------------------------------------

    def load_world_save(self) -> WorldSave: ...

    def save_world_save(self, ws: WorldSave) -> None: ...

    # ------------------------------------------------------------------
    # Location promotions
    # ------------------------------------------------------------------

    def list_location_promotions(self, *, region_id: str) -> list[PgLocationPromotionRow]: ...

    def upsert_location_promotion(self, row: PgLocationPromotionRow) -> None: ...

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
    ) -> None: ...

    def update_scrapbook_image_url(self, *, turn_id: int, image_url: str) -> bool: ...

    def scrapbook_turn_ids(self, *, max_turn: int) -> set[int]: ...

    def scrapbook_image_url_map(self) -> dict[int, str]: ...

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def ensure_session(self, *, slug: str, mode: str, genre_slug: str, world_slug: str) -> int: ...

    def get_game(self, *, slug: str) -> GameRow | None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Dungeon persistence (ADR-115 B1)
# ---------------------------------------------------------------------------


@runtime_checkable
class DungeonTransaction(Protocol):
    """Mutating dungeon operations bound to one locked connection.

    Obtained via ``DungeonRepository.transaction()``.  All methods execute
    within the caller's transaction; the context manager commits on clean
    exit and rolls back on exception (including ``PersistError``).
    """

    def commit_expansion(
        self,
        expansion: Expansion,
        graph: RegionGraph,
        *,
        generator_version: str = ...,
        masks: Mapping[str, dict] | None = None,
    ) -> None: ...

    def put_frontier(self, fe: FrontierEdge) -> None: ...

    def record_mutation(self, region_id: str, kind: str, payload: dict) -> None: ...


@runtime_checkable
class DungeonRepository(Protocol):
    """Storage-engine-agnostic dungeon persistence.

    Concrete implementation: ``PgDungeonRepository`` (ADR-115 B1).
    D6 lifts the ``DungeonStore(conn)`` consumers to this Protocol.
    """

    def transaction(self) -> AbstractContextManager[DungeonTransaction]: ...

    # campaign_seed
    def get_campaign_seed(self) -> int | None: ...

    def set_campaign_seed(self, seed: int) -> None: ...

    # map / masks
    def commit_expansion(
        self,
        expansion: Expansion,
        graph: RegionGraph,
        *,
        generator_version: str = ...,
        masks: Mapping[str, dict] | None = None,
    ) -> None: ...

    def load_map(self, *, entrance_id: str) -> RegionGraph: ...

    def load_masks(self) -> dict[str, dict]: ...

    # frontier
    def put_frontier(self, fe: FrontierEdge) -> None: ...

    def load_frontier(self) -> list[FrontierEdge]: ...

    # mutations
    def record_mutation(self, region_id: str, kind: str, payload: dict) -> None: ...

    def load_mutations(self) -> list[DungeonMutation]: ...

    # complication ledger
    def open_thread(self, thread: ComplicationThread) -> None: ...

    def get_thread(self, thread_id: str) -> ComplicationThread: ...

    def open_threads(self) -> list[ComplicationThread]: ...

    def resolve_thread(self, thread_id: str) -> None: ...
