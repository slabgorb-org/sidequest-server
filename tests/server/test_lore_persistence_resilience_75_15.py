"""Story 75-15 REWORK (round-trip 1) — error-isolation + resilience for the
lore persistence path. Driven by the Reviewer (Chrisjen) REJECT findings.

The first GREEN pass shipped the write-through + rehydrate but wired it in a way
that regresses the project's own error-isolation standard (75-1 review:
"wrap+log+OTEL-fail+continue" for post-turn side-effects; ADR-006 graceful
degradation; ADR-124 loud-skip folds). These tests pin the required behaviour:

1. A lore-persist failure must NOT suppress the durable narrative_log writes
   (lore is least-critical; it must be isolated from narrative persistence).
2. A lore-load failure on resume must DEGRADE (world-only reseed still runs,
   player reconnects) — not drop the connection.
3. `load_fragments` must loud-skip a single corrupt row, not abort the whole
   resume.
4. The idempotent `ON CONFLICT DO UPDATE` upsert contract is exercised
   (regression guard against a `DO NOTHING` swap).
5. The disconnect-save path actually persists (regression guard).

PG-isolation harness mirrors `test_scrapbook_entry_wiring.py` (ADR-115 D2).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.lore_store import LoreCategory, LoreFragment, LoreSource
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.protocol import GameMessage
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"

_NARRATOR_MARK = "water-stained shelves loom in the dark"
_INJECT_ID = "lore_char_creation_origin_0"
_INJECT_CONTENT = "Born in the drowned archive of Lyrre, the scholar fears still water."


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
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


def _pg_repo_for_slug(slug: str):
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _d, _s = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    return repo


def _seed_with_character(slug: str) -> None:
    core = CreatureCore(
        name="Lyrre", description="scholar", personality="cautious", inventory=Inventory()
    )
    char = Character(core=core, char_class="Scholar", race="Human", backstory="Archivist.")
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [char]
    _pg_repo_for_slug(slug).save(snap)


def _connect_msg(slug: str) -> GameMessage:
    return GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {"event": "connect", "game_slug": slug, "last_seen_seq": 0},
        }
    )


def _action_msg() -> GameMessage:
    return GameMessage.model_validate(
        {
            "type": "PLAYER_ACTION",
            "player_id": "alice",
            "payload": {"action": "I study the archive.", "round": 1},
        }
    )


def _fake_narration():
    from sidequest.agents.orchestrator import NarrationTurnResult

    return NarrationTurnResult(
        narration=f"The {_NARRATOR_MARK}.",
        location="Drowned Archive",
        is_degraded=False,
        agent_duration_ms=10,
    )


def _make_handler(tmp_path: Path, socket_id: str) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(registry=RoomRegistry(), socket_id=socket_id, out_queue=queue)
    return handler


def _inject(handler: WebSocketSessionHandler, frag: LoreFragment) -> None:
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None
    if frag.id not in sd.lore_store.fragments:
        sd.lore_store.add(frag)


def _creation_fragment() -> LoreFragment:
    return LoreFragment.new(
        id=_INJECT_ID,
        category=LoreCategory.Character,
        content=_INJECT_CONTENT,
        source=LoreSource.CharacterCreation,
    )


# ---------------------------------------------------------------------------
# [HIGH] Error isolation — a lore-persist failure must not eat the narrative log.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lore_persist_failure_does_not_drop_narrative_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `save_lore_fragments` raises mid-turn, the durable narrative_log writes
    for that turn must STILL land. Lore is the least-critical post-turn
    side-effect; it must be isolated from (and ordered after) narrative
    persistence.

    Pre-rework: `save_lore_fragments` runs before `append_narrative` inside the
    same broad try, so a lore exception skips the narrative appends — that turn
    vanishes from the narrative_log.
    """
    slug = "lore-isolation-turn"
    _seed_with_character(slug)
    handler = _make_handler(tmp_path, "sock-a")

    def _boom(*_a, **_k):
        raise RuntimeError("simulated lore-persist failure")

    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration()),
    ):
        await handler.handle_message(_connect_msg(slug))
        sd = handler._session_data  # type: ignore[attr-defined]
        assert sd is not None
        # Make the lore write-through fail on this turn.
        monkeypatch.setattr(sd.repository, "save_lore_fragments", _boom)
        await handler.handle_message(_action_msg())

    # The narrator's narrative_log entry for this turn MUST have persisted
    # despite the lore failure.
    entries = _pg_repo_for_slug(slug).recent_narrative(limit=20)
    contents = " ".join(getattr(e, "content", "") for e in entries).lower()
    assert _NARRATOR_MARK in contents, (
        "a lore-persist failure must NOT suppress the durable narrative_log "
        f"writes for the turn; recent_narrative held: {contents[:300]!r}"
    )


# ---------------------------------------------------------------------------
# [HIGH] Graceful degradation — a lore-load failure on resume must not drop the
# connection (ADR-006).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_degrades_when_lore_load_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `load_lore_fragments` raises on resume, the connect must still succeed
    and degrade to the world-only reseed — never drop the player.

    Pre-rework: the bare `load_lore_fragments()` call in connect.py propagates,
    aborting `ConnectHandler.handle` and dropping the reconnecting player.
    """
    slug = "lore-resume-degrade"
    _seed_with_character(slug)

    def _boom(self):  # noqa: ANN001
        raise RuntimeError("simulated lore-load failure")

    monkeypatch.setattr(
        "sidequest.game.pg.save_repository.PgSaveRepository.load_lore_fragments",
        _boom,
    )

    handler = _make_handler(tmp_path, "sock-b")
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration()),
    ):
        # Must NOT raise — graceful degradation.
        await handler.handle_message(_connect_msg(slug))

    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None, "session must be established despite the lore-load failure"
    # World-only reseed still ran, so the store is non-empty (degraded, not dead).
    assert len(sd.lore_store) > 0, (
        "resume must degrade to the world-only reseed when lore load fails, "
        "not leave an empty store / drop the connection"
    )


# ---------------------------------------------------------------------------
# [MEDIUM] load_fragments must loud-skip a corrupt row, not abort the resume.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_fragments_loud_skips_blank_content_row(tmp_path: Path) -> None:
    """A single corrupt row (blank content — which LoreFragment.new rejects) must
    be loud-skipped on load; the valid fragments must still re-hydrate.

    Pre-rework: `load_fragments` reconstructs each row via `LoreFragment.new`,
    which raises on blank content, aborting the whole load (and, via the bare
    connect call, the whole resume).
    """
    from datetime import UTC, datetime

    from sidequest.game import db_pool

    slug = "lore-load-corrupt-row"
    _seed_with_character(slug)
    repo = _pg_repo_for_slug(slug)
    sid = repo.session_id

    # Persist one valid fragment through the normal path.
    store_valid = LoreFragment.new(
        id="valid_frag",
        category=LoreCategory.History,
        content="A perfectly good fragment.",
        source=LoreSource.GameEvent,
    )
    from sidequest.game.lore_store import LoreStore

    ls = LoreStore()
    ls.add(store_valid)
    repo.save_lore_fragments(ls)

    # Inject a corrupt (blank-content) row directly, bypassing validation.
    now = datetime.now(tz=UTC).isoformat()
    with db_pool.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO lore_fragments
                (session_id, id, category, content, source, turn_created, metadata_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (sid, "corrupt_frag", LoreCategory.History, "", LoreSource.GameEvent, None, "{}", now),
        )

    loaded = repo.load_lore_fragments()
    ids = {f.id for f in loaded}
    assert "valid_frag" in ids, (
        "load_fragments must return the valid fragment even when a sibling row "
        f"is corrupt; got {sorted(ids)}"
    )
    assert "corrupt_frag" not in ids, "the blank-content row must be loud-skipped, not reconstructed"


# ---------------------------------------------------------------------------
# [HIGH] Idempotent upsert contract — regression guard for ON CONFLICT DO UPDATE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_updates_content(tmp_path: Path) -> None:
    """Re-writing the same fragment id must UPDATE in place (one row, new
    content), not insert a duplicate or no-op. Guards against a DO NOTHING swap.
    """
    from sidequest.game import db_pool
    from sidequest.game.lore_store import LoreStore

    slug = "lore-upsert-idempotent"
    _seed_with_character(slug)
    repo = _pg_repo_for_slug(slug)
    sid = repo.session_id

    v1 = LoreStore()
    v1.add(
        LoreFragment.new(
            id="dupe", category=LoreCategory.History, content="first", source=LoreSource.GameEvent
        )
    )
    repo.save_lore_fragments(v1)

    with db_pool.get_pool().connection() as conn:
        created_at_1 = conn.execute(
            "SELECT created_at FROM lore_fragments WHERE session_id=%s AND id=%s", (sid, "dupe")
        ).fetchone()[0]

    v2 = LoreStore()
    v2.add(
        LoreFragment.new(
            id="dupe", category=LoreCategory.History, content="second", source=LoreSource.GameEvent
        )
    )
    repo.save_lore_fragments(v2)

    with db_pool.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT content, created_at FROM lore_fragments WHERE session_id=%s AND id=%s",
            (sid, "dupe"),
        ).fetchall()

    assert len(rows) == 1, f"upsert must keep exactly one row per id, got {len(rows)}"
    assert rows[0][0] == "second", "upsert must UPDATE content (ON CONFLICT DO UPDATE), not DO NOTHING"
    assert rows[0][1] == created_at_1, "created_at must be preserved across upsert (not reset)"


# ---------------------------------------------------------------------------
# Regression guard — the disconnect-save path persists lore.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_persists_lore_fragments(tmp_path: Path) -> None:
    """`cleanup()` must persist the in-memory lore_store — the disconnect path
    the file docstring names. Guards against the disconnect write-through being
    removed."""
    slug = "lore-disconnect-persist"
    _seed_with_character(slug)
    handler = _make_handler(tmp_path, "sock-a")

    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration()),
    ):
        await handler.handle_message(_connect_msg(slug))
        _inject(handler, _creation_fragment())
        await handler.cleanup()

    from sidequest.game import db_pool

    with db_pool.get_pool().connection() as conn:
        ids = {
            r[0]
            for r in conn.execute(
                "SELECT id FROM lore_fragments WHERE session_id=%s",
                (_pg_repo_for_slug(slug).session_id,),
            ).fetchall()
        }
    assert _INJECT_ID in ids, (
        f"cleanup()/disconnect must persist the in-memory lore_store; rows: {sorted(ids)}"
    )
