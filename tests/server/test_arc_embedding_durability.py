"""Story 45-23 — save/reload durability test for arc-promotion writes.

Per context-story-45-23.md AC6: drive a tier-promotion turn, persist
the snapshot via ``sd.store.save(...)``, reload, and assert the arc-
promotion narrative entries are still present on the reloaded
snapshot's ``narrative_log``.

Felix's bug was a missing call site so nothing reached durable storage
in the first place. This test guarantees that once the call site
exists, the in-snapshot arc rows survive the JSON round-trip — the
``narrative_log`` field on ``GameSnapshot`` already serializes its
entries (45-22 hardened the schema with required ``author`` and the
``entry_type`` field), so this is mostly a wiring assertion: the
helper appended to ``snapshot.narrative_log`` (not just to the
SqliteStore log table) so the saved snapshot carries the rows.

The ``lore_store`` durability is governed by the existing
ADR-048 LoreStore persistence (separate concern); this test scopes
to the in-snapshot ``narrative_log`` durability that 45-23 introduces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.history_chapter import (
    ChapterNarrativeEntry,
    HistoryChapter,
)
from sidequest.game.world_materialization import ARC_RECOMPUTE_INTERVAL
from tests.server.conftest import _build_turn_context_for_test


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


@pytest.mark.asyncio
async def test_arc_promotion_entries_survive_save_and_reload(
    session_handler_factory,
) -> None:
    """End-to-end durability — drive a Fresh→Early transition, save the
    snapshot via the real SqliteStore, reload, and assert the arc-
    promotion entries are still on the snapshot's narrative_log.
    """

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
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

    # The dispatch loop already persisted via ``sd.store.save`` inside
    # the persistence phase (websocket_session_handler.py:1624). Loading
    # back exercises the JSON round-trip without a second save call —
    # the assertion is on the persisted shape, not a re-save.
    saved = sd.store.load()
    assert saved is not None, (
        "SqliteStore returned no saved session — the dispatch path's "
        "save() did not commit, so the durability assertion below "
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
    """Belt-and-braces: ``sd.store.append_narrative`` writes rows to
    the SQLite ``narrative_log`` table independent of the snapshot
    JSON. Felix's narrator-state-summary path does not query that
    table directly, but Sebastien's GM panel does (via
    ``recent_narrative``) — so the persistence call must land rows
    that the panel can replay.
    """

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
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
    rows = sd.store.recent_narrative(limit=20)
    arc_rows = [r for r in rows if "keep stirs" in r.content or "descend" in r.content]
    assert len(arc_rows) == 2, (
        "Durable narrative_log SQL table is missing arc-promotion "
        "rows — the helper did not call ``sd.store.append_narrative`` "
        "for the seeded entries. Without this call the GM panel's "
        "recent_narrative() replay drops the chapter content. "
        f"Got rows: {[r.content for r in rows]!r}"
    )
