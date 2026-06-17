"""Story 75-15 (AC1/AC4/AC6) — creation-seed + accreted lore fragments must
survive resume.

Root cause (gulliver 2026-06-02, 36 turns): ``_SessionData.lore_store`` is an
in-memory ``field(default_factory=LoreStore)`` (``session_state.py:240``) that is
NEVER written through to Postgres. The ``lore_fragments`` table exists in the
schema (``alembic 0001``) and is cleared on reinit (``pg/sessions.py``), but NO
code writes or reads it. The only thing that survives a resume is the world-only
re-seed in ``handlers.connect`` (``_seed_world_lore_on_resume`` →
``slug_resume_reseed``), which restores ~3 world fragments. The 17 char-creation
fragments a fresh chargen seeded — and every runtime-accreted fragment 75-1's
``accrete_facts_to_lore`` minted into the same in-memory store — are lost on
resume. Symptom: every ``rag.lore_store_loaded`` on resume reports
``total_fragments=3 reason=slug_resume_reseed`` and the narrator runs on Claude's
own knowledge, not retrieval.

These tests assert the OBSERVABLE end-state contract through the real
seed→persist→resume path (CLAUDE.md "No Source-Text Wiring Tests"; AC6
"fixture-driven behavioral tests through the real path"):

1. Fragments present in a session's lore_store at save time are written through
   to the ``lore_fragments`` table.
2. A reconnecting handler re-hydrates them into its in-memory lore_store — the
   resumed total reflects what was seeded/accreted, not the world-only 3.

They are deliberately design-agnostic about HOW persistence is wired (a new pg
sub-store, lore_store joining the GameSnapshot, or a per-turn write-through). The
exact seam is a design decision flagged for the Architect/Dev — these tests pin
the behaviour, not the mechanism.

The PG-isolation + slug-bound-repo harness mirrors
``tests/server/test_scrapbook_entry_wiring.py`` exactly (ADR-115 D2).
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

# Distinctive ids/content so the assertions can't pass on world-reseed noise.
_CREATION_FRAG_ID = "lore_char_creation_origin_0"
_CREATION_CONTENT = "Born in the drowned archive of Lyrre, the scholar fears still water."
_ACCRETED_FRAG_ID = "lore_kf_gulliver_fact_42"
_ACCRETED_CONTENT = "The Brobdingnag custom-house weighs travellers by the ounce of their lies."


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    The seed, the live turn's save, and the reconnect handler must all share one
    isolated database so the slug-connect path reads the snapshot/characters the
    seed wrote. Mirrors ``test_scrapbook_entry_wiring._pg_isolation``.
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


def _pg_repo_for_slug(slug: str):
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    return repo


def _pg_lore_rows(slug: str) -> list[tuple]:
    """Read ``lore_fragments`` for ``slug``'s session straight from PG.

    Returns (id, category, source) tuples ordered by id. The per-worker db is
    TRUNCATEd per test, so only this session's rows are present.
    """
    from sidequest.game import db_pool

    repo = _pg_repo_for_slug(slug)
    with db_pool.get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, category, source FROM lore_fragments WHERE session_id = %s ORDER BY id",
            (repo.session_id,),
        ).fetchall()


def _seed_with_character(slug: str) -> None:
    """Persist a snapshot + character so the slug-connect path resumes into
    Playing (has_character=True) rather than dropping into chargen."""
    core = CreatureCore(
        name="Lyrre",
        description="A drowned-archive scholar",
        personality="Cautious",
        inventory=Inventory(),
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
            "payload": {"action": "I study the drowned archive.", "round": 1},
        }
    )


def _fake_narration_result():
    from sidequest.agents.orchestrator import NarrationTurnResult

    return NarrationTurnResult(
        narration="The water-stained shelves loom in the dark.",
        location="Drowned Archive",
        is_degraded=False,
        agent_duration_ms=10,
    )


def _make_handler(tmp_path: Path, socket_id: str):
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id=socket_id,
        out_queue=queue,
    )
    return handler


def _inject_fragment(handler: WebSocketSessionHandler, frag: LoreFragment) -> None:
    """Add a fragment to the live session's lore_store, idempotently.

    Simulates the post-chargen / post-accretion state: a creation-seed or
    runtime-accreted fragment already present in the in-memory store at the
    moment of a normal save.
    """
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None, "session must be connected before injecting fragments"
    if frag.id not in sd.lore_store.fragments:
        sd.lore_store.add(frag)


def _creation_fragment() -> LoreFragment:
    return LoreFragment.new(
        id=_CREATION_FRAG_ID,
        category=LoreCategory.Character,
        content=_CREATION_CONTENT,
        source=LoreSource.CharacterCreation,
    )


def _accreted_fragment() -> LoreFragment:
    return LoreFragment.new(
        id=_ACCRETED_FRAG_ID,
        category=LoreCategory.History,
        content=_ACCRETED_CONTENT,
        source=LoreSource.GameEvent,
    )


# ---------------------------------------------------------------------------
# AC1 / AC6 — write-through to the lore_fragments table.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creation_seed_fragment_persists_to_lore_fragments_table(
    tmp_path: Path,
) -> None:
    """A creation-seed fragment present in the lore_store at save time must be
    written through to the ``lore_fragments`` Postgres table.

    Pre-fix: the table has 0 rows for the session (no writer exists), so the
    fragment dies with the in-memory store on disconnect.
    """
    slug = "lore-persist-creation-seed"
    _seed_with_character(slug)
    handler = _make_handler(tmp_path, "sock-a")

    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler.handle_message(_connect_msg(slug))
        _inject_fragment(handler, _creation_fragment())
        await handler.handle_message(_action_msg())

    rows = _pg_lore_rows(slug)
    ids = {r[0] for r in rows}
    assert _CREATION_FRAG_ID in ids, (
        "creation-seed fragment must be written through to the lore_fragments "
        f"table on save; rows present: {sorted(ids)}"
    )
    persisted = next(r for r in rows if r[0] == _CREATION_FRAG_ID)
    assert persisted[2] == LoreSource.CharacterCreation, (
        f"persisted fragment must keep its source tag; got {persisted[2]!r}"
    )


@pytest.mark.asyncio
async def test_accreted_fragment_persists_to_lore_fragments_table(
    tmp_path: Path,
) -> None:
    """75-1 cross-check: ``accrete_facts_to_lore`` mints runtime facts into the
    SAME in-memory lore_store. Because that store is never persisted, accreted
    fragments are also lost on resume — 75-1's "persisted on save/load" AC was
    not actually delivered to Postgres. Assert a GameEvent-source fragment is
    written through too.
    """
    slug = "lore-persist-accreted"
    _seed_with_character(slug)
    handler = _make_handler(tmp_path, "sock-a")

    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler.handle_message(_connect_msg(slug))
        _inject_fragment(handler, _accreted_fragment())
        await handler.handle_message(_action_msg())

    ids = {r[0] for r in _pg_lore_rows(slug)}
    assert _ACCRETED_FRAG_ID in ids, (
        "runtime-accreted (GameEvent) fragment must be written through to "
        f"lore_fragments on save; rows present: {sorted(ids)}"
    )


# ---------------------------------------------------------------------------
# AC1 / AC4 — fragments re-hydrate on resume (no starve-to-world-only).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creation_and_accreted_fragments_survive_resume(
    tmp_path: Path,
) -> None:
    """The load-bearing behaviour: after a save, a fresh reconnect must
    re-hydrate the creation-seed AND accreted fragments into its in-memory
    lore_store — not starve to the world-only re-seed.

    Pre-fix: handler B's lore_store holds only the ``_seed_world_lore_on_resume``
    fragments; both distinctive ids below are absent (the gulliver
    ``total_fragments=3`` symptom).
    """
    slug = "lore-resume-survive"
    _seed_with_character(slug)

    handler_a = _make_handler(tmp_path, "sock-a")
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler_a.handle_message(_connect_msg(slug))
        _inject_fragment(handler_a, _creation_fragment())
        _inject_fragment(handler_a, _accreted_fragment())
        await handler_a.handle_message(_action_msg())

    # Fresh reconnect — new _SessionData, new in-memory lore_store.
    handler_b = _make_handler(tmp_path, "sock-b")
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler_b.handle_message(_connect_msg(slug))

    sd_b = handler_b._session_data  # type: ignore[attr-defined]
    assert sd_b is not None
    resumed_ids = set(sd_b.lore_store.fragments)
    assert _CREATION_FRAG_ID in resumed_ids, (
        "creation-seed fragment must re-hydrate into the resumed lore_store; "
        f"resumed ids: {sorted(resumed_ids)}"
    )
    assert _ACCRETED_FRAG_ID in resumed_ids, (
        "runtime-accreted fragment must re-hydrate into the resumed lore_store; "
        f"resumed ids: {sorted(resumed_ids)}"
    )


# ---------------------------------------------------------------------------
# AC5 — the resume telemetry must distinguish "re-hydrated persisted" from the
# world-only "slug_resume_reseed"-to-3, so the GM panel can tell a real resume
# from a starved one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_lore_store_loaded_reports_persisted_total_not_world_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On resume the ``lore_store_loaded`` watcher event must report a total
    that reflects the re-hydrated persisted fragments — strictly more than the
    world-only re-seed count — so the panel can see persisted-vs-reseeded.

    Pre-fix: the only ``lore_store_loaded`` on resume carries
    ``reason=slug_resume_reseed`` and ``total_fragments`` == the world-only
    count; the persisted creation-seed/accreted fragments never raise it.
    """
    import sidequest.handlers.connect as connect_mod

    slug = "lore-resume-telemetry"
    _seed_with_character(slug)

    handler_a = _make_handler(tmp_path, "sock-a")
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler_a.handle_message(_connect_msg(slug))
        _inject_fragment(handler_a, _creation_fragment())
        _inject_fragment(handler_a, _accreted_fragment())
        await handler_a.handle_message(_action_msg())

    captured: list[tuple[str, dict]] = []

    def _capture(event_type, fields, **kwargs):
        captured.append((event_type, dict(fields)))

    # connect.py binds publish_event as the module-level name `_watcher_publish`
    # (connect.py:65), so patch the bound name in the consuming module — patching
    # watcher_hub.publish_event would not reach the already-bound reference.
    monkeypatch.setattr(connect_mod, "_watcher_publish", _capture)

    handler_b = _make_handler(tmp_path, "sock-b")
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler_b.handle_message(_connect_msg(slug))

    loaded = [f for et, f in captured if et == "lore_store_loaded"]
    assert loaded, (
        "resume must emit a lore_store_loaded watcher event so the GM panel "
        f"can prove lore loaded; captured event types: {[et for et, _ in captured]}"
    )
    total = max(int(f.get("total_fragments", 0)) for f in loaded)
    # 2 injected (creation + accreted) fragments must have re-hydrated on top of
    # the world-only re-seed, so the reported total clears the world-only floor.
    world_only = max(int(f.get("world_fragments_added", 0)) for f in loaded)
    assert total >= world_only + 2, (
        "resume lore_store_loaded total must reflect re-hydrated persisted "
        f"fragments (>= world_only+2); got total={total} world_only={world_only}"
    )
    # REWORK (Reviewer [TEST]): the >= world_only+2 check degenerates to ">= 2"
    # when the fixture world has 0 world fragments — any two fragments would
    # satisfy it regardless of rehydration. Independently falsify by asserting
    # the SPECIFIC persisted ids re-hydrated into the resumed store, AND that the
    # panel can see a non-trivial rehydrated count distinct from the world-only
    # reseed (persisted-vs-reseeded legibility, AC5).
    sd_b = handler_b._session_data  # type: ignore[attr-defined]
    assert sd_b is not None
    resumed_ids = set(sd_b.lore_store.fragments)
    assert {_CREATION_FRAG_ID, _ACCRETED_FRAG_ID} <= resumed_ids, (
        "the resume telemetry must reflect the specific re-hydrated persisted "
        f"fragments, not just a count; resumed ids: {sorted(resumed_ids)}"
    )
    rehydrated = max(int(f.get("rehydrated_fragments", -1)) for f in loaded)
    assert rehydrated >= 2, (
        "lore_store_loaded must surface a 'rehydrated_fragments' count (>=2 here) "
        "so the GM panel can distinguish persisted-vs-reseeded; got "
        f"rehydrated_fragments={rehydrated}"
    )
