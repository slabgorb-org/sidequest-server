"""Fixtures and shared scaffolding for integration tests.

Re-exports fixtures from ``tests.server.conftest`` so integration tests can
build a real ``WebSocketSessionHandler`` + ``_SessionData`` and drive
encounter engine paths without re-implementing the fixtures.

Also hosts the shared **OTEL watcher-wiring harness** (``watcher_setup`` +
``wait_for_state_transition``) consumed by the ``*_otel_wiring`` tests in this
directory. These tests all install a *local* ``TracerProvider`` +
``WatcherSpanProcessor``, monkeypatch ``spans_module.tracer`` so the production
helpers resolve to the test's tracer, subscribe a recording socket to the hub,
and poll the captured events for a typed ``state_transition`` — see story 71-34
(dedupe of the combat + beat-advance copies of this scaffolding).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests._helpers.doubles import FakeSocket

# session_handler_factory now lives in the tests/ root conftest (moved in 73-6,
# de-duplicated in 73-11) and is inherited by tests/integration/ automatically —
# no cross-module import needed. The fixtures below still live in
# tests/server/conftest (a sibling dir, not a parent), so they are re-exported here.
#
# Hermeticity tripod (story 123-2): tests/integration/ is a SIBLING of
# tests/server/, so it does NOT inherit the four AUTOUSE hermeticity legs that
# tests/server/conftest defines. Re-exporting an autouse fixture's NAME into this
# conftest re-registers it (with its autouse flag intact) for this directory tree —
# so the same `from ... import` block that already pulls plain fixtures is also the
# install point for the guards. Without this, resume-path integration tests reach the
# LIVE Anthropic SDK (real 401/billing on any keyed machine). The catch-all
# `_no_real_anthropic_sdk` leg is the floor requirement: any un-faked SDK
# construction fails LOUD here instead of billing. Tests that install their own fakes
# still shadow these guards via monkeypatch LIFO.
from tests.server.conftest import (  # noqa: F401
    _mock_claude_client,
    _no_real_anthropic_sdk,
    _stub_intent_router_factory,
    _stub_unseeded_objective_classifier,
    _watcher_hub_event_store_isolation,
    encounter_dispatch_helper,
    otel_capture,
    session_fixture,
    store_bound_to_hub,
    synthetic_two_dial_pack,
)


@pytest.fixture(autouse=True)
def _stub_dungeon_curate_client(monkeypatch):
    """Autouse guard (story 123-2): stub the dungeon curate LLM client at the
    ``session_integration`` import site.

    The four re-exported tripod legs cover the narrator, intent-router,
    objective-classifier, and catch-all SDK sites — but the procedural
    megadungeon attach path (``beneath_sunden``, ADR-106) has its OWN
    construction site: ``attach_dungeon_to_session`` calls
    ``build_llm_client(purpose="tool")`` eagerly to thread a curate client into
    ``materialize`` / ``register_lookahead_worker``. The ``caverns_and_claudes``
    resume integration tests hit that path on reconnect, so without this leg the
    catch-all ``_no_real_anthropic_sdk`` guard fires loud (correct — no billing —
    but the test cannot complete).

    ``tests/server/conftest`` does NOT make this autouse because the dungeon unit
    tests in ``tests/dungeon/`` each install ``_reflecting_sdk_client`` themselves;
    here we install it tree-wide so WS-driven integration tests that resume a
    dungeon world are hermetic. We reuse that same reflecting fake (it parses the
    curate prompt and echoes a well-formed verdict — never a network call). Patched
    at ``session_integration``'s import-time binding, not the factory module. Tests
    that want a different curate double install their own AFTER this (LIFO).
    """
    from tests.dungeon.test_materializer import _reflecting_sdk_client

    monkeypatch.setattr(
        "sidequest.dungeon.session_integration.build_llm_client",
        _reflecting_sdk_client,
    )


async def watcher_setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Bind the module hub to this loop, install a local ``TracerProvider``
    with the ``WatcherSpanProcessor``, subscribe a recording socket, and
    monkeypatch ``spans_module.tracer`` so production helpers resolve to it.

    Returns the list the recording socket appends every broadcast into —
    poll it with ``wait_for_state_transition``. Shared by the
    ``*_otel_wiring`` integration tests (story 71-34)."""
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    sock = FakeSocket()
    await watcher_hub.subscribe(sock)  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    return sock.events


async def wait_for_state_transition(
    captured: list[dict],
    predicate: Callable[[dict], bool],
    *,
    timeout_s: float = 1.0,
    describe: str = "matching predicate",
) -> dict:
    """Poll ``captured`` for a ``state_transition`` event satisfying
    ``predicate``. The hub broadcast hops through
    ``run_coroutine_threadsafe`` so tests must yield repeatedly until the
    queued coroutines drain. Raises ``AssertionError`` with an event
    summary on timeout. Shared by the ``*_otel_wiring`` tests (story 71-34)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        for evt in captured:
            if evt.get("event_type") == "state_transition" and predicate(evt):
                return evt
        await asyncio.sleep(0.01)
    summary = [
        (
            e.get("event_type"),
            e.get("component"),
            e.get("fields", {}).get("field"),
            sorted((e.get("fields") or {}).keys()),
        )
        for e in captured
    ]
    raise AssertionError(
        f"Expected a state_transition {describe} within {timeout_s}s; "
        f"captured {len(captured)} events: {summary}"
    )


def make_minimal_coyote_star_magic_state():
    """Build a minimum-valid MagicState for the coyote_star test world.

    S1 invariant (2026-05-04 split-brain cleanup): magic_state must be
    initialized before ``init_chassis_registry`` runs, because the chassis
    loader writes confrontations into ``snapshot.magic_state.confrontations``
    directly. Tests that previously called ``init_chassis_registry`` with
    a None ``magic_state`` need this helper.
    """
    from sidequest.magic.models import WorldKnowledge, WorldMagicConfig
    from sidequest.magic.state import MagicState

    return MagicState.from_config(
        WorldMagicConfig(
            world_slug="coyote_star",
            genre_slug="space_opera",
            allowed_sources=[],
            active_plugins=[],
            intensity=0.0,
            world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
            visibility={"primary": "feared", "local_register": "dismissed"},
            hard_limits=[],
            cost_types=[],
            ledger_bars=[],
            narrator_register="test",
        )
    )
