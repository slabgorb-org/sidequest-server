from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from sidequest.dungeon import frontier_hook


def _real_pack() -> Any:
    """The REAL loaded caverns_and_claudes GenrePack (spec §14.C).

    No _attach_pack: the keystone genuinely proves a real session grows
    the dungeon. The 4 set-piece tropes are authored in the genre-level
    tropes.yaml (Plan 7 §14.A); the real pack resolves them. Provenance:
    handlers/connect.py:~400 loads genre_pack -> session_integration.py
    passes it as pack_tropes -> setpiece_attach.py resolves trope_id.
    """
    from sidequest.genre.loader import (
        DEFAULT_GENRE_PACK_SEARCH_PATHS,
        GenreLoader,
    )

    return GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS).load("caverns_and_claudes")


@pytest.fixture(autouse=True)
def _restore_frontier_observers() -> Any:
    """Prevent observer registration leaking into the ~6500-test suite
    (the Task-6/7 wiring-test fixture pattern)."""
    before = list(frontier_hook._OBSERVERS)
    try:
        yield
    finally:
        frontier_hook._OBSERVERS[:] = before


def _otel_in_memory() -> tuple[Any, Any, Any]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider, provider.get_tracer("test")


def _beneath_sunden_world_dir() -> Path:
    # tests/dungeon/<file> -> tests -> sidequest-server -> repo root;
    # sidequest-content is a SIBLING of sidequest-server (parents[3]),
    # matching the existing _BENEATH_SUNDEN_WORLD in test_materializer.py.
    return (
        Path(__file__).resolve().parents[3]
        / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
    )


async def test_session_lifecycle_registers_worker_and_dungeon_grows(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: str,
) -> None:
    """End-to-end: attach (fresh save) bootstraps expansion 0+1 and
    registers the worker; a REAL production region transition
    (snap.apply_world_patch -> frontier_hook.notify_region_transition)
    toward an unexpanded frontier edge materializes the next expansion;
    the frontier.region_transition span carries observers>=1 (the
    lie-detector signal flips off zero); detach unregisters cleanly."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon import session_integration
    from sidequest.game.session import GameSnapshot, WorldStatePatch
    from sidequest.telemetry.spans.dungeon_materialize import (
        SPAN_FRONTIER_REGION_TRANSITION,
    )
    from tests.dungeon.conftest import build_pg_dungeon_repo
    from tests.dungeon.test_materializer import _reflecting_sdk_client

    monkeypatch.setattr(session_integration, "build_llm_client", _reflecting_sdk_client)

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)
    game_slug = f"lifecycle_{uuid.uuid4().hex[:12]}"
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.current_region = "entrance"

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]

    handle = None
    try:
        handle = await session_integration.attach_dungeon_to_session(
            dungeon_repository=repo,
            game_slug=game_slug,
            snapshot=snap,
            genre_pack=_real_pack(),
            genre_slug="caverns_and_claudes",
            world_slug="beneath_sunden",
            world_dir=_beneath_sunden_world_dir(),
        )
        assert handle is not None
        # Two observers: lookahead worker + expansion-quest observer (Task 8).
        assert frontier_hook.registered_observer_count() == 2, (
            "the look-ahead worker and expansion-quest observer are not both "
            "registered for the live session (observers < 2 — the dungeon would "
            "not grow and quests would not project in a real game)"
        )

        before = {n.expansion_id for n in repo.load_map(entrance_id="entrance").nodes.values()}
        assert before == {0, 1}, f"bootstrap did not seed expansion 0+1; got {before}"

        target = repo.load_frontier()[0].from_region_id
        assert target != "entrance", (
            "frontier edge must leave entrance for the apply_world_patch != _prev_region guard to fire"
        )
        snap.current_region = "entrance"
        snap.apply_world_patch(WorldStatePatch(current_region=target))
        await handle.drain()

        after = {n.expansion_id for n in repo.load_map(entrance_id="entrance").nodes.values()}
        assert max(after) >= 2, (
            f"region crossing toward an unexpanded frontier edge did NOT "
            f"materialize the next expansion; expansions={sorted(after)}"
        )

        finished = exporter.get_finished_spans()
        rt = [s for s in finished if s.name == SPAN_FRONTIER_REGION_TRANSITION]
        assert rt, "no frontier.region_transition span emitted (producer not fired)"
        assert any((s.attributes or {}).get("observers", 0) >= 1 for s in rt), (
            "every frontier.region_transition span has observers=0 — the "
            "seam fired but the session never registered a consumer (the "
            "ADR-106 lie-detector signal: dungeon does not grow)"
        )

        # §10(f) — IDEMPOTENT re-attach (was: hard RuntimeError). The MP
        # deterministic URL means every join/reconnect re-enters attach on
        # the SAME live save; a hard raise crashed the connect (live
        # playtest 2026-05-17). A second attach for the same save must now
        # return the EXISTING handle and add NO second observer (the
        # §14.D save-keyed dedup still holds — no double-register, no
        # double-materialize; it just no longer crashes the join).
        second = await session_integration.attach_dungeon_to_session(
            dungeon_repository=repo,
            game_slug=game_slug,
            snapshot=GameSnapshot(
                genre_slug="caverns_and_claudes",
                world_slug="beneath_sunden",
            ),
            genre_pack=_real_pack(),
            genre_slug="caverns_and_claudes",
            world_slug="beneath_sunden",
            world_dir=_beneath_sunden_world_dir(),
        )
        assert second is handle, (
            "idempotent re-attach must return the SAME live handle so "
            "additional MP sockets share the one registered worker"
        )
        # Idempotent re-attach: count stays at 2 (no double-register).
        assert frontier_hook.registered_observer_count() == 2, (
            "the concurrent-same-save re-attach double-registered — the "
            "§14.D save-keyed dedup did not hold"
        )
    finally:
        await session_integration.detach_dungeon_from_session(handle)
        _spans_module.tracer = original_tracer_fn  # type: ignore[method-assign]

    assert frontier_hook.registered_observer_count() == 0, (
        "detach did not unregister the observer (registry leak across sessions)"
    )
