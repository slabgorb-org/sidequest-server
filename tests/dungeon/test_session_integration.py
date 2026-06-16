from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sidequest.dungeon import frontier_hook


@pytest.fixture(autouse=True)
def _restore_frontier_observers() -> Any:
    before = list(frontier_hook._OBSERVERS)
    try:
        yield
    finally:
        frontier_hook._OBSERVERS[:] = before


def _sqlite_store() -> Any:
    from sidequest.game.persistence import SqliteStore

    return SqliteStore.open_in_memory()


def _beneath_sunden_world_dir() -> Path:
    # tests/dungeon/<file> -> tests -> sidequest-server -> repo root;
    # sidequest-content is a SIBLING of sidequest-server (parents[3]),
    # matching the existing _BENEATH_SUNDEN_WORLD in test_materializer.py.
    return (
        Path(__file__).resolve().parents[3]
        / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
    )


def _snapshot() -> Any:
    from sidequest.game.session import GameSnapshot

    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def _real_pack() -> Any:
    """The REAL loaded caverns_and_claudes GenrePack — NOT _attach_pack.

    Spec §14.C: the keystone must prove a real session grows the dungeon.
    The materializer resolves set-piece trope_id against this pack's
    genre-level .tropes (verified: handlers/connect.py:~400 loads
    genre_pack -> session_integration.py:106/114 passes it as
    pack_tropes -> setpiece_attach.py:411-413 resolves trope_id).
    After Plan 7 §14.A the 4 set-piece tropes
    are authored in genre_packs/caverns_and_claudes/tropes.yaml, so the
    real pack resolves them with no fabrication.
    """
    from sidequest.genre.loader import (
        DEFAULT_GENRE_PACK_SEARCH_PATHS,
        GenreLoader,
    )

    return GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS).load("caverns_and_claudes")


async def test_non_beneath_sunden_is_a_clean_noop() -> None:
    from sidequest.dungeon.session_integration import attach_dungeon_to_session

    handle = await attach_dungeon_to_session(
        store=_sqlite_store(),
        snapshot=_snapshot(),
        genre_pack=object(),
        genre_slug="space_opera",
        world_slug="some_world",
        world_dir=Path("/nonexistent"),
    )
    assert handle is None
    assert frontier_hook.registered_observer_count() == 0


async def test_detach_is_null_safe() -> None:
    from sidequest.dungeon.session_integration import detach_dungeon_from_session

    await detach_dungeon_from_session(None)  # must not raise


async def test_attach_seeds_and_registers_then_detach_unregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.dungeon import session_integration
    from sidequest.dungeon.persistence import DungeonStore
    from tests.dungeon.test_materializer import _reflecting_sdk_client

    monkeypatch.setattr(session_integration, "build_llm_client", _reflecting_sdk_client)

    store = _sqlite_store()
    snap = _snapshot()
    handle = await session_integration.attach_dungeon_to_session(
        store=store,
        snapshot=snap,
        genre_pack=_real_pack(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        world_dir=_beneath_sunden_world_dir(),
    )
    assert handle is not None
    assert frontier_hook.registered_observer_count() == 1

    ds = DungeonStore(store.connection())
    assert ds.get_campaign_seed() is not None
    nodes = ds.load_map(entrance_id="entrance").nodes
    assert "entrance" in nodes and nodes["entrance"].expansion_id == 0
    assert any(n.expansion_id == 1 for n in nodes.values())

    await session_integration.detach_dungeon_from_session(handle)
    assert frontier_hook.registered_observer_count() == 0


async def test_attach_is_idempotent_reuses_persisted_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.dungeon import session_integration
    from sidequest.dungeon.persistence import DungeonStore
    from tests.dungeon.test_materializer import _reflecting_sdk_client

    monkeypatch.setattr(session_integration, "build_llm_client", _reflecting_sdk_client)
    store = _sqlite_store()
    kw = dict(
        store=store,
        snapshot=_snapshot(),
        genre_pack=_real_pack(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        world_dir=_beneath_sunden_world_dir(),
    )
    h1 = await session_integration.attach_dungeon_to_session(**kw)
    await session_integration.detach_dungeon_from_session(h1)
    seed1 = DungeonStore(store.connection()).get_campaign_seed()
    map1 = sorted(DungeonStore(store.connection()).load_map(entrance_id="entrance").nodes)

    h2 = await session_integration.attach_dungeon_to_session(**dict(kw, snapshot=_snapshot()))
    await session_integration.detach_dungeon_from_session(h2)
    seed2 = DungeonStore(store.connection()).get_campaign_seed()
    map2 = sorted(DungeonStore(store.connection()).load_map(entrance_id="entrance").nodes)

    assert seed1 == seed2, "reopen must reuse the frozen campaign_seed"
    assert map1 == map2, "reopen must NOT re-seed (idempotent bootstrap)"


async def test_concurrent_attach_same_save_is_idempotent_then_reattaches_after_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§14.D: a second attach for an already-attached save is IDEMPOTENT
    — it returns the EXISTING handle and adds NO second observer (no
    double-register, no double-materialize). Was a hard RuntimeError, but
    the MP deterministic URL means every join/reconnect re-enters attach
    on the one shared save; raising crashed the connect (live playtest
    2026-05-17). After detach, a fresh attach for that save succeeds
    (sequential reopen is unaffected)."""
    from sidequest.dungeon import session_integration
    from tests.dungeon.test_materializer import _reflecting_sdk_client

    monkeypatch.setattr(session_integration, "build_llm_client", _reflecting_sdk_client)
    store = _sqlite_store()
    kw = dict(
        store=store,
        snapshot=_snapshot(),
        genre_pack=_real_pack(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        world_dir=_beneath_sunden_world_dir(),
    )
    h1 = await session_integration.attach_dungeon_to_session(**kw)
    assert h1 is not None
    assert frontier_hook.registered_observer_count() == 1

    h_re = await session_integration.attach_dungeon_to_session(**dict(kw, snapshot=_snapshot()))
    assert h_re is h1, (
        "idempotent re-attach must return the SAME live handle so "
        "additional MP sockets share the one registered worker"
    )
    assert frontier_hook.registered_observer_count() == 1

    await session_integration.detach_dungeon_from_session(h1)
    assert frontier_hook.registered_observer_count() == 0

    h2 = await session_integration.attach_dungeon_to_session(**dict(kw, snapshot=_snapshot()))
    assert h2 is not None
    assert frontier_hook.registered_observer_count() == 1
    await session_integration.detach_dungeon_from_session(h2)
    assert frontier_hook.registered_observer_count() == 0
