"""Story 45-23 — save/reload durability test for arc-promotion writes.

Per context-story-45-23.md AC6: drive a tier-promotion turn, persist
the snapshot + narrative rows, reload, and assert the arc-promotion
narrative entries are still present on the reloaded snapshot's
``narrative_log`` and in the durable narrative_log store.

Felix's bug was a missing call site so nothing reached durable storage
in the first place. This test guarantees that once the call site
exists, the in-snapshot arc rows survive the round-trip — the
``narrative_log`` field on ``GameSnapshot`` serializes its entries
(45-22 hardened the schema with required ``author`` and the
``entry_type`` field), and ``seed_lore_from_arc_promotion`` also calls
``repository.append_narrative`` so the durable narrative store carries
the rows for GM-panel replay.

ADR-115 D2 note: the authoritative narrative_log + snapshot now persist
to **Postgres** via ``db_pool.get_pool()``, not a SQLite save.db. The
``session_handler_factory`` builds a legacy single-player ``_SessionData``
over an in-memory SqliteSaveRepository, so this test rebinds both the
session repository (target of ``seed_lore_from_arc_promotion``'s
``append_narrative``) and the room store (target of the persistence
phase's ``room.save()``) to one shared ``PgSaveRepository`` keyed on a
fixed slug. Durability is then read back from that same PG repository —
``load()`` for the snapshot round-trip, ``recent_narrative()`` for the
durable narrative store — rather than reopening a SQLite file (which is
empty under D2). The arc-promotion content/tags/round shape the
assertions check is unchanged; only the read/write target moves to PG.

The ``lore_store`` durability is governed by the existing ADR-048
LoreStore persistence (separate concern); this test scopes to the
in-snapshot ``narrative_log`` durability that 45-23 introduces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.history_chapter import (
    ChapterNarrativeEntry,
    HistoryChapter,
)
from sidequest.game.persistence import GameMode
from sidequest.game.world_materialization import ARC_RECOMPUTE_INTERVAL
from tests.server.conftest import _build_turn_context_for_test

_GENRE = "caverns_and_claudes"
_WORLD = "sunken_keep"
# Fixed slug — collision-safe across parallel runs because _pg_isolation
# binds the process pool to a per-worker throwaway db that is TRUNCATEd
# (RESTART IDENTITY CASCADE) before each test, so this slug's session is
# the only one in the db when the durability reads run.
_SLUG = "arc-embedding-durability-fixture"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    ADR-115 D2: the snapshot + narrative_log persist to Postgres via
    db_pool.get_pool(). Seed (the rebound PgSaveRepository) and the
    durability reads must share one isolated database. Copied shape from
    test_turn_telemetry_wiring.py::_pg_isolation (HEAD).
    """
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _content_chapters() -> list[HistoryChapter]:
    """Three-tier chapters with content; the ``early`` chapter is the
    one that promotes on a Fresh→Early transition.
    """

    return [
        HistoryChapter(
            id="early",
            label="Early arc",
            narrative_log=[
                ChapterNarrativeEntry(
                    speaker="narrator",
                    text="The keep stirs after a year empty.",
                ),
                ChapterNarrativeEntry(
                    speaker="Rux",
                    text="Then we listen, and we descend.",
                ),
            ],
            lore=["The keep was abandoned in the Year of Black Salt."],
        ),
        HistoryChapter(id="mid", label="Mid arc"),
        HistoryChapter(id="veteran", label="Veteran arc"),
    ]


def _bind_pg_repository(sd) -> object:
    """Rebind ``sd.repository`` AND the room store to one shared PG repo.

    Both production write paths exercised by this turn must land in the
    same Postgres session so a single ``load()`` / ``recent_narrative()``
    read sees them:

    - ``seed_lore_from_arc_promotion`` writes through ``sd.repository``
      (``append_narrative`` for the durable narrative store).
    - the persistence phase's ``room.save()`` persists the snapshot (with
      its in-memory ``narrative_log`` entries) through the room store.

    ``PgSaveRepository.for_slug`` is idempotent on the slug, so pointing
    both at one ``for_slug`` instance keeps them on a single session_id.
    Returns the bound repository for the durability reads.
    """
    from sidequest.game import db_pool
    from sidequest.game.pg.save_repository import PgSaveRepository

    repo = PgSaveRepository.for_slug(
        db_pool.get_pool(),
        slug=_SLUG,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    sd.repository = repo
    # Room owns the canonical snapshot; the persistence phase calls
    # ``room.save()`` (sd._room is set on the legacy single-player factory
    # path), so the room store must be the same PG repo for the snapshot
    # round-trip to carry the arc entries. The room is already bound, so
    # poke the private store slot directly (bind_world is one-shot).
    assert sd._room is not None, (
        "factory must set sd._room on the legacy single-player path — the "
        "persistence phase calls room.save() and the snapshot round-trip "
        "depends on it persisting through the shared PG repo."
    )
    sd._room._store = repo
    return repo


@pytest.mark.asyncio
async def test_arc_promotion_entries_survive_save_and_reload(
    session_handler_factory,
) -> None:
    """End-to-end durability — drive a Fresh→Early transition, persist the
    snapshot to Postgres (ADR-115 D2), reload, and assert the arc-
    promotion entries are still on the snapshot's narrative_log.
    """

    sd, handler = session_handler_factory(genre=_GENRE)
    repo = _bind_pg_repository(sd)
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Calm settles.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )

    sd.cached_history_chapters = _content_chapters()
    sd.snapshot.turn_manager.interaction = ARC_RECOMPUTE_INTERVAL - 1
    sd.snapshot.turn_manager.round = 10

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

    # The dispatch loop already persisted via ``room.save()`` inside the
    # persistence phase (websocket_session_handler.py). Loading back from
    # the same PG session exercises the snapshot round-trip without a
    # second save call — the assertion is on the persisted shape.
    saved = repo.load()
    assert saved is not None, (
        "PgSaveRepository returned no saved session — the dispatch path's "
        "room.save() did not commit, so the durability assertion below "
        "cannot run."
    )
    reloaded = saved.snapshot

    arc_entries = [e for e in reloaded.narrative_log if e.entry_type == "arc_promotion"]
    assert len(arc_entries) == 2, (
        "AC6 failure: arc-promotion entries did not survive save/reload. "
        "If this fails after the helper-implementation lands, the "
        "helper appended to a transient list (e.g. a local var) "
        "instead of ``snapshot.narrative_log`` — the round-trip then "
        "drops them. Reloaded narrative_log: "
        f"{[e.entry_type for e in reloaded.narrative_log]!r}"
    )
    contents = [e.content for e in arc_entries]
    assert any("keep stirs" in c for c in contents)
    assert any("descend" in c for c in contents)


@pytest.mark.asyncio
async def test_arc_promotion_entries_present_in_durable_narrative_log_table(
    session_handler_factory,
) -> None:
    """Belt-and-braces: ``repository.append_narrative`` writes rows to the
    durable narrative store independent of the snapshot JSON. Felix's
    narrator-state-summary path does not query that store directly, but
    Sebastien's GM panel does (via ``recent_narrative``) — so the
    persistence call must land rows that the panel can replay. Under
    ADR-115 D2 the durable store is the Postgres narrative_log table, read
    back via the same PG repository the turn wrote through.
    """

    sd, handler = session_handler_factory(genre=_GENRE)
    repo = _bind_pg_repository(sd)
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Calm settles.",
            is_degraded=False,
            agent_duration_ms=1,
        )
    )

    sd.cached_history_chapters = _content_chapters()
    sd.snapshot.turn_manager.interaction = ARC_RECOMPUTE_INTERVAL - 1
    sd.snapshot.turn_manager.round = 10

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I push deeper.", turn_context)

    # Pull a generous slice — the per-turn narration appends a few
    # entries (player + narrator) on top of the arc-promotion rows;
    # 20 is a comfortable upper bound.
    rows = repo.recent_narrative(limit=20)
    arc_rows = [r for r in rows if "keep stirs" in r.content or "descend" in r.content]
    assert len(arc_rows) == 2, (
        "Durable narrative_log store is missing arc-promotion rows — the "
        "helper did not call ``repository.append_narrative`` for the "
        "seeded entries. Without this call the GM panel's "
        "recent_narrative() replay drops the chapter content. "
        f"Got rows: {[r.content for r in rows]!r}"
    )
