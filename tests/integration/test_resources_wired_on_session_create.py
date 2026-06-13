"""Wiring gate — ADR-033 resource pools are wired on the REAL session-create path.

The behavior test in ``tests/game/test_resource_wiring.py`` proves
``wire_genre_resources`` works in isolation. These tests keep Dev honest
(CLAUDE.md "Verify Wiring, Not Just Existence" + "No Source-Text Wiring Tests"):
they drive a fresh character through the actual chargen-commit handler, and a
saved session through the actual reconnect/resume handler, asserting that the
shipping ``caverns_and_claudes`` pack's declared ``light`` resource pool is
populated on BOTH paths — not that a helper merely exists.

Two production seams call ``wire_genre_resources`` in ``connect.py``:
- the fresh-create branch (also exercised via the chargen-commit materialize),
- the resume/load branch, where a persisted snapshot is bound as canonical.

Removing either call makes the matching test fail.

``test_light_pool_wired_on_real_chargen_commit`` drives the chargen-commit path.
``test_light_pool_wired_and_current_preserved_on_resume`` walks chargen on one
handler, mutates ``light.current`` to a non-starting value, persists, then opens
a SECOND handler against the same slug to hit the resume branch — asserting the
pool is present AND its ``current`` survived the reload (the idempotent-upsert
contract a survival clock depends on).

Reuses the chargen-commit harness from ``test_chargen_dispatch`` and the PG
isolation fixture pattern from ``test_chargen_quest_seed_wiring`` (the create
wiring runs on the first-commit/materialize path; a fresh DB guarantees each run
is a first-commit, then an explicit shared slug drives the second-handler resume).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot, ResourcePool
from sidequest.protocol.messages import (
    CharacterCreationPayload,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry
from tests.server.conftest import mock_claude_client_factory as _mock_claude_client_factory
from tests.server.test_chargen_dispatch import (
    _connect,
    _send_chargen,
    _walk_to_confirmation,
    run,
)

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
_RESUME_GENRE = "caverns_and_claudes"
_RESUME_WORLD = "beneath_sunden"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, truncated per
    test, so each run is a first-commit (materialize) path rather than a
    slug-resume. Mirrors ``test_chargen_quest_seed_wiring::_pg_isolation``.
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


@pytest.fixture
def handler(tmp_path: Path) -> WebSocketSessionHandler:
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("content pack not found")
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=tmp_path,
    )


@pytest.mark.integration
def test_light_pool_wired_on_real_chargen_commit(
    handler: WebSocketSessionHandler,
) -> None:
    """The shipping caverns_and_claudes pack declares a `light` resource pool;
    driving the real chargen-commit path must leave it populated on the
    canonical snapshot."""

    async def body() -> None:
        await _connect(handler)
        await _walk_to_confirmation(handler, freeform_name="Rux")
        out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
        assert out, "confirmation must return at least the CHARACTER_CREATION{complete} message"

    run(body())

    sd = handler._session_data  # type: ignore[attr-defined]
    assert len(sd.snapshot.characters) == 1, "PC must be materialized on the snapshot"
    assert "light" in sd.snapshot.resources, (
        "caverns_and_claudes declares a `light` resource pool but it is absent "
        "from the snapshot after chargen commit — wire_genre_resources is not "
        "wired into the session-create path"
    )


# ---------------------------------------------------------------------------
# Resume / load path — wire_genre_resources must also fire when a persisted
# snapshot is bound as canonical (connect.py resume branch), idempotently.
# ---------------------------------------------------------------------------


def _persist_resume_snapshot(slug: str, light_pool: ResourcePool | None) -> None:
    """Persist a caverns_and_claudes snapshot with a seated PC to Postgres —
    the exact durable state a reconnect restores. Optionally pre-seed a
    ``light`` ResourcePool to exercise the upsert-preserve contract; pass
    ``None`` to simulate a save that predates the declared pool.
    """
    snap = GameSnapshot(
        genre_slug=_RESUME_GENRE,
        world_slug=_RESUME_WORLD,
        location="Ropefoot",
    )
    core = CreatureCore(
        name="Rux",
        description="A resuming delver",
        personality="dogged",
        inventory=Inventory(),
    )
    snap.characters = [
        Character(core=core, char_class="Delver", race="Human", backstory="Came back down")
    ]
    snap.player_seats["resume-pid"] = "Rux"
    if light_pool is not None:
        snap.resources["light"] = light_pool

    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.SOLO),
        genre_slug=_RESUME_GENRE,
        world_slug=_RESUME_WORLD,
    )
    repo.save(snap)


def _resume_handler(
    save_dir: Path, registry: RoomRegistry, socket_id: str
) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=save_dir,
    )
    handler.attach_room_context(registry=registry, socket_id=socket_id, out_queue=asyncio.Queue())
    return handler


async def _reconnect(handler: WebSocketSessionHandler, slug: str) -> None:
    await handler.handle_message(
        SessionEventMessage(
            type="SESSION_EVENT",
            player_id="resume-pid",
            payload=SessionEventPayload(event="connect", game_slug=slug, player_name="Rux"),
        )
    )


@pytest.mark.integration
def test_light_pool_wired_on_resume_for_save_predating_pool(tmp_path: Path) -> None:
    """A persisted save that predates the `light` pool (resources empty) must
    GAIN the pool on reconnect — only the resume-branch wire_genre_resources
    call can add it, since the deserializer leaves absent pools absent."""
    if not (CONTENT_ROOT / _RESUME_GENRE).is_dir():
        pytest.skip("content pack not found")
    slug = "resume-light-predates-pool"
    _persist_resume_snapshot(slug, light_pool=None)

    registry = RoomRegistry()  # fresh = post-reload, room rebuilt from Postgres
    handler = _resume_handler(tmp_path, registry, "sock-resume")
    run(_reconnect(handler, slug))

    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd.snapshot.characters, "reconnect must load the persisted character"
    assert "light" in sd.snapshot.resources, (
        "a resumed caverns_and_claudes save did not gain the declared `light` "
        "pool — wire_genre_resources is not wired into the resume/load path"
    )


@pytest.mark.integration
def test_light_current_preserved_on_resume(tmp_path: Path) -> None:
    """A mid-delve `light` value must survive a reload: the resume-branch
    wire_genre_resources upsert re-applies the pack declaration WITHOUT
    resetting `current` to its 0.0 starting value (the survival-clock
    contract)."""
    if not (CONTENT_ROOT / _RESUME_GENRE).is_dir():
        pytest.skip("content pack not found")
    slug = "resume-light-current-preserved"
    mid_delve = ResourcePool(
        name="light",
        label="Light",
        current=4.0,  # non-starting (declaration `starting` is 0.0)
        min=0.0,
        max=6.0,
        voluntary=False,
        decay_per_turn=0.0,
    )
    _persist_resume_snapshot(slug, light_pool=mid_delve)

    registry = RoomRegistry()
    handler = _resume_handler(tmp_path, registry, "sock-resume")
    run(_reconnect(handler, slug))

    sd = handler._session_data  # type: ignore[attr-defined]
    assert "light" in sd.snapshot.resources, "resumed snapshot lost the `light` pool"
    assert sd.snapshot.resources["light"].current == 4.0, (
        "resume-branch wire_genre_resources reset `light.current` to the "
        "starting value — the upsert must preserve mid-delve progress"
    )
