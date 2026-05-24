"""Story 61-9 wiring test — dungeon caller declares ``purpose="tool"``.

The dungeon ``attach_dungeon_to_session`` path constructs an LLM client
via ``build_llm_client()`` at ``session_integration.py:155``. Post-61-9
that call must pass ``purpose="tool"`` so the new narrator-vs-tool gate
can produce a context-aware error message AND so future code audits of
"who's a narrator caller" can rely on declared intent rather than module
location.

This file is paired with ``tests/agents/test_61_9_sdk_commitment.py``
which holds the rest of the 61-9 RED contract. Lives here because the
existing dungeon-attach fixture infrastructure (real GenrePack load,
beneath_sunden world dir, frontier-hook restore) is already wired in
``tests/dungeon/test_session_integration.py`` — TEA reuses it rather
than duplicating.

Reflection-only contract per ``sidequest-server/CLAUDE.md`` §"No
Source-Text Wiring Tests": this is a behavioral spy test that drives
real ``attach_dungeon_to_session`` through a monkey-patched factory and
captures the kwargs handed to ``build_llm_client``.
"""

from __future__ import annotations

import pytest

from sidequest.dungeon import frontier_hook


@pytest.fixture(autouse=True)
def _restore_frontier_observers():
    before = list(frontier_hook._OBSERVERS)
    try:
        yield
    finally:
        frontier_hook._OBSERVERS[:] = before


async def test_attach_dungeon_calls_build_llm_client_with_purpose_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``attach_dungeon_to_session`` must call
    ``build_llm_client(purpose="tool")`` — not the bare default which
    today maps to ``purpose="narrator"`` and would loud-fail under any
    non-SDK backend.

    The architect-recommended mechanism (story-context Architect §B) is a
    ``purpose: Literal["narrator", "tool"]`` keyword on
    ``build_llm_client``. Today the dungeon call site at
    ``session_integration.py:155`` is just ``build_llm_client()``;
    AC-3 requires the call to declare ``purpose="tool"``.

    Spy via ``monkeypatch.setattr`` on the module-level reference (the
    same pattern as the existing ``test_attach_seeds_and_registers...``
    test, which monkey-patches the same factory to a ``_reflecting_sdk_
    client``). Captures all kwargs handed to the factory across the
    bootstrap path and asserts at least one carries ``purpose="tool"``.
    """
    from sidequest.dungeon import session_integration

    # Import test fixtures from the existing session_integration test
    # module. Lives in the same package so this is a direct sibling
    # import, no path mangling required.
    from tests.dungeon.test_session_integration import (
        _beneath_sunden_world_dir,
        _real_pack,
        _snapshot,
        _sqlite_store,
    )
    from tests.dungeon.test_materializer import _reflecting_sdk_client

    captured_kwargs: list[dict[str, object]] = []

    def _spy(*args, **kwargs):
        # Swallow the kwargs (purpose=...) — the real factory takes them
        # post-AC-3 but the test fake here is a no-arg constructor.
        captured_kwargs.append(dict(kwargs))
        return _reflecting_sdk_client()

    monkeypatch.setattr(session_integration, "build_llm_client", _spy)

    handle = await session_integration.attach_dungeon_to_session(
        store=_sqlite_store(),
        snapshot=_snapshot(),
        genre_pack=_real_pack(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        world_dir=_beneath_sunden_world_dir(),
    )
    assert handle is not None, (
        "attach_dungeon_to_session must successfully attach for "
        "beneath_sunden — the fixture is the working keystone."
    )
    try:
        assert captured_kwargs, (
            "build_llm_client was never called during dungeon bootstrap — "
            "either the wiring is broken or the monkey-patch missed the "
            "module-level reference."
        )
        purposes = [kw.get("purpose") for kw in captured_kwargs]
        assert "tool" in purposes, (
            f"AC-3 wiring: dungeon bootstrap must call "
            f"build_llm_client(purpose='tool'). Captured kwargs across all "
            f"factory calls: {captured_kwargs!r}. Today's site at "
            f"session_integration.py:155 is `build_llm_client()` (bare); "
            f"Dev must update to `build_llm_client(purpose='tool')`."
        )
    finally:
        await session_integration.detach_dungeon_from_session(handle)
