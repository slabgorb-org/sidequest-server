"""Wiring test — Beneath Sünden BETTER fix (the four-seam projection).

CLAUDE.md "Every Test Suite Needs a Wiring Test": this drives the REAL
production chain end to end, no mocks of the system under test (only the
LLM client is canned — exactly as the keystone prompt test does):

  attach_dungeon_to_session (real pack, real world dir, real materialize)
    -> DungeonStore.load_map -> RegionGraph
    -> project_region (seam 1)
    -> Orchestrator.build_narrator_prompt registers the YOU-ARE-HERE
       section with the REAL adjacent region ids (seam 1+2 — the
       constrained move vocabulary that stops geography improvisation)
    -> _project_current_region emits the dungeon.region_projection span
       (seam 4 — the GM-panel lie detector)
    -> _maybe_emit_dungeon_map emits a DUNGEON_MAP frame to the UI
       (seam 3 — cures "No map data yet")

The 2026-05-17 playtest proved the dungeon materializes but is orphaned
from its consumers; this test fails if any of the four wires is cut.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sidequest.dungeon import frontier_hook


def _real_pack() -> Any:
    from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader

    return GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS).load("caverns_and_claudes")


def _beneath_sunden_world_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
    )


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


@pytest.fixture(autouse=True)
def _restore_frontier_observers() -> Any:
    """attach registers a look-ahead observer; never leak it into the
    ~6500-test suite (the Task-6/7 wiring-test fixture pattern)."""
    before = list(frontier_hook._OBSERVERS)  # noqa: SLF001
    try:
        yield
    finally:
        frontier_hook._OBSERVERS[:] = before  # noqa: SLF001


class _CannedClient:
    """Minimal LlmClient — build_narrator_prompt does not call the LLM,
    but the Orchestrator constructor wants a client (keystone pattern)."""

    async def send(self, prompt: str, **_: Any) -> Any:
        from sidequest.agents.claude_client import ClaudeResponse

        return ClaudeResponse(text="ok", duration_ms=0)


class _FakeSessionData:
    """Duck-typed _SessionData — _project_current_region /
    _maybe_emit_dungeon_map read exactly genre_slug, world_slug, store,
    player_id. A full _SessionData needs a live WS handler; the seam
    contract is these four attributes, so this exercises the REAL
    functions against the REAL store."""

    def __init__(self, store: Any, *, genre: str, world: str) -> None:
        self.store = store
        self.genre_slug = genre
        self.world_slug = world
        self.player_id = "p1"
        # Playtest 2026-05-20 — cartography-aware SELF-HEAL reads
        # ``sd.genre_pack`` to detect "this is a valid cartography region,
        # not a phantom; don't heal it into the dungeon". Tests now
        # exercise that branch, so the duck-typed session needs the
        # pack handle. Loaded lazily because the real pack is heavy.
        self._genre_pack_cache: Any = None

    @property
    def genre_pack(self) -> Any:
        if self._genre_pack_cache is None:
            self._genre_pack_cache = _real_pack()
        return self._genre_pack_cache


async def _attach(store: Any, snap: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from sidequest.dungeon import session_integration
    from tests.dungeon.test_materializer import _reflecting_sdk_client

    monkeypatch.setattr(session_integration, "build_llm_client", _reflecting_sdk_client)
    return await session_integration.attach_dungeon_to_session(
        store=store,
        snapshot=snap,
        genre_pack=_real_pack(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        world_dir=_beneath_sunden_world_dir(),
    )


async def test_projection_reaches_narrator_prompt_with_real_move_vocab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seam 1+2: a real materialized region is projected into the
    narrator prompt as a YOU-ARE-HERE section whose exit ids are REAL
    graph nodes — the constrained move vocabulary."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext
    from sidequest.dungeon import session_integration
    from sidequest.dungeon.persistence import DungeonStore
    from sidequest.dungeon.region_projection import project_region
    from sidequest.dungeon.themes import load_theme_palette
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot

    store = SqliteStore.open_in_memory()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    handle = None
    try:
        handle = await _attach(store, snap, monkeypatch)
        assert handle is not None
        # #314 seam: attach bound the entrance.
        assert snap.current_region == "entrance"

        graph = DungeonStore(store.connection()).load_map(entrance_id="entrance")
        world_dir = _beneath_sunden_world_dir()
        palette = load_theme_palette(world_dir.parent.parent)

        proj = project_region(graph, snap.current_region, palette)
        assert proj.region_id == "entrance"
        assert proj.flavor and proj.register
        assert proj.exits, "entrance must have at least one real exit"
        for e in proj.exits:
            assert e.to_region_id in graph.nodes, (
                f"projected exit {e.to_region_id!r} is not a real graph "
                "node — the move vocabulary would send the narrator to a "
                "phantom region"
            )

        orch = Orchestrator(client=_CannedClient())
        ctx = TurnContext(
            character_name="Carl",
            genre="caverns_and_claudes",
            turn_number=3,
            region_projection=proj,
        )
        prompt_text, _registry = await orch.build_narrator_prompt("look around", ctx)

        assert "YOU ARE HERE" in prompt_text
        assert "entrance" in prompt_text
        assert "MOVEMENT RULE" in prompt_text
        # The constrained move vocabulary: a REAL adjacent id is in-prompt
        # so the narrator's current_region patch targets a valid node.
        assert any(e.to_region_id in prompt_text for e in proj.exits), (
            "no real adjacent region id reached the narrator prompt"
        )
    finally:
        await session_integration.detach_dungeon_from_session(handle)


async def test_project_current_region_emits_span_and_skips_other_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seam 4: the per-turn _build_turn_context feed emits exactly one
    dungeon.region_projection span — outcome=projected for beneath_sunden,
    outcome=no_dungeon (observable, not silent) for any other world."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon import session_integration
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_helpers import _project_current_region
    from sidequest.telemetry.spans.dungeon_region_projection import (
        SPAN_DUNGEON_REGION_PROJECTION,
    )

    store = SqliteStore.open_in_memory()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    exporter, _provider, real_tracer = _otel_in_memory()
    original = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    handle = None
    try:
        handle = await _attach(store, snap, monkeypatch)
        sd = _FakeSessionData(store, genre="caverns_and_claudes", world="beneath_sunden")
        proj = _project_current_region(sd, snap)
        assert proj is not None and proj.region_id == "entrance"

        # Other world: clean OBSERVABLE no-op (not a silent skip).
        sd_other = _FakeSessionData(store, genre="space_opera", world="coyote_star")
        assert _project_current_region(sd_other, snap) is None

        spans = [
            s for s in exporter.get_finished_spans() if s.name == SPAN_DUNGEON_REGION_PROJECTION
        ]
        outcomes = {(s.attributes or {}).get("outcome") for s in spans}
        assert "projected" in outcomes
        assert "no_dungeon" in outcomes
        projected = next(s for s in spans if (s.attributes or {}).get("outcome") == "projected")
        assert (projected.attributes or {}).get("region_id") == "entrance"
        assert (projected.attributes or {}).get("exit_count", 0) >= 1
    finally:
        _spans_module.tracer = original  # type: ignore[method-assign]
        await session_integration.detach_dungeon_from_session(handle)


async def test_resumed_save_self_heals_blank_current_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live-game path: a RESUMED beneath_sunden save has a
    materialized dungeon but a blank current_region (the slug_resume
    connect branch never reached the #314 attach bind — OQ-1 2026-05-17).
    _project_current_region must self-heal at the per-turn seam: bind the
    graph entrance, project it, and flag the recovery in the span. Without
    this the narrator improvises geography on every resumed session."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon import session_integration
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_helpers import _project_current_region
    from sidequest.telemetry.spans.dungeon_region_projection import (
        SPAN_DUNGEON_REGION_PROJECTION,
    )

    store = SqliteStore.open_in_memory()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    exporter, _provider, real_tracer = _otel_in_memory()
    original = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    handle = None
    try:
        handle = await _attach(store, snap, monkeypatch)
        # Simulate the resume: dungeon stays materialized, position lost.
        snap.current_region = ""
        snap.discovered_regions = []

        sd = _FakeSessionData(store, genre="caverns_and_claudes", world="beneath_sunden")
        proj = _project_current_region(sd, snap)

        assert proj is not None, "self-heal failed — narrator would improvise"
        assert proj.region_id == "entrance"
        # The heal mutates the live snapshot so the frontier hook + UI
        # emit (same snapshot, later in the turn) see the bound entrance.
        assert snap.current_region == "entrance"
        assert "entrance" in snap.discovered_regions

        healed = [
            s
            for s in exporter.get_finished_spans()
            if s.name == SPAN_DUNGEON_REGION_PROJECTION
            and (s.attributes or {}).get("bound_entrance") is True
        ]
        assert healed, "no span flagged bound_entrance — heal is invisible to the GM panel"
    finally:
        _spans_module.tracer = original  # type: ignore[method-assign]
        await session_integration.detach_dungeon_from_session(handle)


async def test_phantom_current_region_self_heals_every_sequential_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OQ-1 2026-05-17 live regression (closes the keystone-test gap).

    A TRUE phantom — a name the narrator improvised that lives in
    neither ``cartography.regions`` nor the materialized procedural
    dungeon graph — must self-heal on EVERY sequential turn, not just
    turn 1. The per-turn projection heal is in-memory only (SQLite is
    the dungeon SSOT — never mirrored onto the persisted snapshot), so
    every turn reloads the same phantom. A single-turn keystone test
    passes while the live game fails on turn 2+.

    Playtest 2026-05-20 amendment: the original phantom value was
    ``"ropefoot"`` — but ropefoot is the surface waiting-camp listed in
    ``cartography.yaml`` as ``starting_region``. The cartography-aware
    heal in ``_project_current_region`` now correctly recognizes
    cartography regions and does NOT teleport the player into the
    dungeon (see ``test_cartography_region_is_not_self_healed``). Use a
    name that is neither in cartography nor the graph so this test
    keeps protecting the original disease.
    """
    import sidequest.telemetry.spans as _spans_module
    from sidequest.agents.orchestrator import Orchestrator, TurnContext
    from sidequest.dungeon import session_integration
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_helpers import _project_current_region
    from sidequest.telemetry.spans.dungeon_region_projection import (
        SPAN_DUNGEON_REGION_PROJECTION,
    )

    # A name no cartography region claims and no graph node uses — a
    # true narrator-improvised phantom (the kind ADR-106's constrained
    # move vocabulary is meant to suppress).
    phantom = "windswept_overlook_of_lost_names"

    store = SqliteStore.open_in_memory()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    exporter, _provider, real_tracer = _otel_in_memory()
    original = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    handle = None
    try:
        handle = await _attach(store, snap, monkeypatch)
        sd = _FakeSessionData(store, genre="caverns_and_claudes", world="beneath_sunden")
        orch = Orchestrator(client=_CannedClient())

        # Three sequential turns. Each turn re-stamps the persisted static
        # phantom (the in-memory heal is never persisted) — exactly the
        # live "FAILED ×12" condition. Turn 2+ is the regression a
        # single-turn test cannot see.
        for turn in (1, 2, 3):
            snap.current_region = phantom
            snap.discovered_regions = [phantom]

            proj = _project_current_region(sd, snap)

            assert proj is not None, (
                f"turn {turn}: phantom {phantom!r} self-heal failed — "
                "narrator would improvise geography this turn"
            )
            assert proj.region_id == "entrance", (
                f"turn {turn}: expected heal to graph entrance, got {proj.region_id!r}"
            )
            assert snap.current_region == "entrance", (
                f"turn {turn}: heal did not rebind the live snapshot — the "
                "frontier hook + UI emit later in the turn see the phantom"
            )

            ctx = TurnContext(
                character_name="Carl",
                genre="caverns_and_claudes",
                turn_number=turn,
                region_projection=proj,
            )
            prompt_text, _registry = await orch.build_narrator_prompt("look around", ctx)
            assert "YOU ARE HERE" in prompt_text and "entrance" in prompt_text, (
                f"turn {turn}: real geography did not reach the narrator "
                "prompt — improvisation would resume this turn"
            )

        # The GM panel (lie detector) must see the heal fire EVERY turn,
        # each recording the phantom it healed from.
        healed = [
            s
            for s in exporter.get_finished_spans()
            if s.name == SPAN_DUNGEON_REGION_PROJECTION
            and (s.attributes or {}).get("bound_entrance") is True
        ]
        assert len(healed) >= 3, (
            "expected a bound_entrance span on every sequential turn; "
            f"got {len(healed)} (turn 2+ heal is invisible / not firing)"
        )
        assert all((s.attributes or {}).get("healed_from") == phantom for s in healed), (
            "span must record the phantom healed-from for GM-panel forensics"
        )
    finally:
        _spans_module.tracer = original  # type: ignore[method-assign]
        await session_integration.detach_dungeon_from_session(handle)


async def test_cartography_region_is_not_self_healed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playtest 2026-05-20 regression — beneath_sunden's surface
    ``ropefoot`` waiting-camp is a deliberately-authored CARTOGRAPHY
    region (cartography.yaml ``starting_region``), NOT a phantom and
    NOT a node of the procedural dungeon graph (the dungeon lives
    underground, below ``the_dropmouth``). The pre-fix heal treated it
    as a phantom and teleported the player into the dungeon
    ``entrance`` every turn — destroying the surface narrative anchor
    AND mutating the persisted snapshot.

    Correct behavior: per-turn projection returns ``None`` (no
    procedural projection on the surface), does NOT mutate the
    snapshot, and the span carries ``outcome=cartography_region`` so
    the GM panel sees the turn ran intentionally without dungeon
    geography.
    """
    import sidequest.telemetry.spans as _spans_module
    from sidequest.dungeon import session_integration
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_helpers import _project_current_region
    from sidequest.telemetry.spans.dungeon_region_projection import (
        SPAN_DUNGEON_REGION_PROJECTION,
    )

    store = SqliteStore.open_in_memory()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    exporter, _provider, real_tracer = _otel_in_memory()
    original = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    handle = None
    try:
        handle = await _attach(store, snap, monkeypatch)
        sd = _FakeSessionData(store, genre="caverns_and_claudes", world="beneath_sunden")

        # The surface waiting camp — a real cartography region, never a
        # graph node. Two turns to prove the per-turn behavior is
        # stable (not just turn-1).
        snap.current_region = "ropefoot"
        snap.discovered_regions = ["ropefoot"]

        for turn in (1, 2):
            proj = _project_current_region(sd, snap)

            assert proj is None, (
                f"turn {turn}: surface cartography region projected as "
                "if it were a dungeon node — the narrator would receive "
                "underground 'YOU ARE HERE' geography for a surface scene"
            )
            assert snap.current_region == "ropefoot", (
                f"turn {turn}: cartography region was rewritten to a "
                f"dungeon node {snap.current_region!r} — the player has "
                "been teleported underground against narrative intent"
            )

        cart_spans = [
            s
            for s in exporter.get_finished_spans()
            if s.name == SPAN_DUNGEON_REGION_PROJECTION
            and (s.attributes or {}).get("outcome") == "cartography_region"
        ]
        assert len(cart_spans) >= 2, (
            "GM panel cannot distinguish 'on the surface, no dungeon "
            "projection by design' from 'projection failed' — both "
            "would look like silent skips. Expected 2 "
            f"outcome=cartography_region spans; got {len(cart_spans)}"
        )
    finally:
        _spans_module.tracer = original  # type: ignore[method-assign]
        await session_integration.detach_dungeon_from_session(handle)


async def test_dungeon_map_frame_is_emitted_to_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seam 3: _maybe_emit_dungeon_map projects the live graph to a
    DUNGEON_MAP frame in MapState shape — curing 'No map data yet'."""
    from sidequest.dungeon import session_integration
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.protocol.messages import DungeonMapMessage
    from sidequest.server.websocket_session_handler import _maybe_emit_dungeon_map

    store = SqliteStore.open_in_memory()
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    handle = None
    try:
        handle = await _attach(store, snap, monkeypatch)
        captured: list[tuple[Any, str]] = []

        def _emit(msg: Any, kind: str) -> None:
            captured.append((msg, kind))

        sd = _FakeSessionData(store, genre="caverns_and_claudes", world="beneath_sunden")
        _maybe_emit_dungeon_map(None, sd=sd, snapshot=snap, emit_fn=_emit)

        dmaps = [m for m, k in captured if k == "DUNGEON_MAP"]
        assert len(dmaps) == 1
        msg = dmaps[0]
        assert isinstance(msg, DungeonMapMessage)
        assert msg.payload.current_location == "entrance"
        assert msg.payload.explored, "no discovered regions projected"
        entrance = next(loc for loc in msg.payload.explored if loc.id == "entrance")
        assert entrance.is_current_room is True
        assert entrance.room_type == "entrance"
        for loc in msg.payload.explored:
            for ex in loc.room_exits:
                assert ex.target, "exit target must be a real region id"

        # Other world: clean no-op (no frame emitted).
        captured.clear()
        sd_other = _FakeSessionData(store, genre="space_opera", world="coyote_star")
        _maybe_emit_dungeon_map(None, sd=sd_other, snapshot=snap, emit_fn=_emit)
        assert not captured
    finally:
        await session_integration.detach_dungeon_from_session(handle)
