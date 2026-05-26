"""Shared pytest fixtures for sidequest-server server-layer tests.

Centralizes the Claude-client mock used by every dispatch test. Before
Story 2.3 Slice H the mock could be a bare ``AsyncMock()`` because
``_chargen_confirmation`` never invoked the orchestrator — the
narration path only fired on PLAYER_ACTION. Slice H routes an opening
turn through the orchestrator at confirmation, so every chargen test
now goes through the narrator pipeline and the mock has to return a
real :class:`ClaudeResponse` with non-empty text + session id.

Also installs a daemon guard: every test in this directory runs with
``DaemonClient`` replaced by an always-unavailable stub so that
narration-turn post-processing (lore embedding, image render) never
blocks on a real Unix-domain-socket round trip. Integration tests that
want to exercise the daemon path use an in-process fake
(``test_render_dispatch.py``'s asyncio Unix server,
``test_lore_rag_wiring.py``'s counting stub) — their
``monkeypatch.setattr`` call simply overrides the guard for that test.
No server test ever talks to the real ``/tmp/sidequest-renderer.sock``.

Also installs a genre-pack search-path guard: every test in this directory
resolves genre packs from ``tests/fixtures/packs/`` (the frozen fixture
pack at ``test_genre/`` with symlinks for each real genre slug) rather than
from ``sidequest-content/``. This makes the suite hermetic — no CI
dependency on the content submodule. Tests that construct
``WebSocketSessionHandler`` with an explicit ``genre_pack_search_paths``
argument (e.g. ``test_session_handler_slug_resumed.py``) bypass this
guard intentionally and must handle their own content-not-found skips.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.agents.claude_client import ClaudeResponse
from tests._helpers.session_room import room_for

if TYPE_CHECKING:
    from sidequest.game.persistence import GameMode

# Absolute path to the frozen fixture pack directory.
# Structure: tests/fixtures/packs/{test_genre,caverns_and_claudes,...} where
# every slug is a symlink → test_genre (mutant_wasteland frozen copy).
_FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def seed_slug_for_test(
    save_dir: Path,
    *,
    genre: str,
    world: str,
    slug: str = "test-slug",
    mode: GameMode | None = None,
) -> str:
    """Story 45-26: pre-populate a slug-keyed games-table row for tests.

    The legacy ``(genre, world, player_name)``-tuple connect path was
    deleted; tests that previously sent ``payload.genre`` /
    ``payload.world`` must now send ``payload.game_slug``. This helper
    creates the on-disk save directory and ``games`` row so the slug
    resolves on connect.

    Returns the slug to thread into the connect envelope.
    """
    from sidequest.game.persistence import (
        GameMode,
        SqliteStore,
        db_path_for_slug,
        upsert_game,
    )

    resolved_mode = mode if mode is not None else GameMode.SOLO

    db = db_path_for_slug(save_dir, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=resolved_mode,
        genre_slug=genre,
        world_slug=world,
    )
    store.close()
    return slug


def attach_default_room_context(handler) -> None:
    """Attach a fresh RoomRegistry + socket id + out queue to a test
    handler so the slug-connect branch can register the room.

    Story 45-26: the slug-connect path raises if ``attach_room_context``
    was not called (production: ws_endpoint calls it after accept()).
    Tests that drive ``handler.handle_message`` directly must do the
    same wiring; this helper is the equivalent of the WebSocket
    lifecycle hook.

    Idempotent: bails out if the handler already has the room
    registry attached, so callers can sprinkle it liberally.
    """
    import asyncio

    from sidequest.server.session_room import RoomRegistry

    if getattr(handler, "_room_registry", None) is not None:
        return
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="test-socket",
        out_queue=asyncio.Queue(),
    )


# ---------------------------------------------------------------------------
# Daemon guard — autouse. Prevents any server test from reaching the real
# /tmp/sidequest-renderer.sock, which would otherwise burn up to 180 s per
# embed()/render() call when the daemon is slow, warming, or dead.
# ---------------------------------------------------------------------------


class _UnavailableDaemonClient:
    """Stand-in for ``DaemonClient`` that never admits to being available.

    ``is_available()`` returns ``False``, matching the natural fail-fast
    branch already handled by ``session_handler._maybe_dispatch_render``
    and ``lore_embedding.{retrieve_lore_context,embed_pending_fragments}``.
    Any accidental call into ``embed()`` / ``render()`` raises loudly
    instead of hanging — that's the whole point of the guard.
    """

    socket_path = Path("/tmp/sq-test-daemon-not-used.sock")

    def is_available(self) -> bool:
        return False

    async def embed(self, text: str):  # noqa: ARG002
        raise RuntimeError(
            "DaemonClient.embed called in a server test without opting in "
            "(mark @pytest.mark.live_daemon or patch the symbol yourself)."
        )

    async def render(self, params):  # noqa: ARG002
        raise RuntimeError(
            "DaemonClient.render called in a server test without opting in "
            "(mark @pytest.mark.live_daemon or patch the symbol yourself)."
        )


@pytest.fixture(autouse=True)
def _mock_daemon_client(monkeypatch):
    """Autouse guard: replace ``DaemonClient`` with an always-unavailable
    stub everywhere the server code instantiates one inline.

    No test talks to the real daemon. Integration tests that want a
    daemon fake (``test_render_dispatch.py``, ``test_lore_rag_wiring.py``)
    install their own via ``monkeypatch.setattr`` — those patches shadow
    this one for the duration of that test and teardown unwinds in LIFO.
    """
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.DaemonClient",
        lambda *a, **kw: _UnavailableDaemonClient(),
    )
    monkeypatch.setattr(
        "sidequest.game.lore_embedding.DaemonClient",
        lambda *a, **kw: _UnavailableDaemonClient(),
    )


# ---------------------------------------------------------------------------
# Fixture pack search-path guard — autouse. Redirects all genre pack
# resolution to tests/fixtures/packs/ so the test suite never depends on
# sidequest-content being present on disk.
#
# Every genre slug used in tests (caverns_and_claudes, elemental_harmony,
# mutant_wasteland, spaghetti_western, space_opera, heavy_metal, low_fantasy,
# neon_dystopia) resolves via a symlink in that directory that points to the
# frozen test_genre/ pack (a stripped copy of mutant_wasteland).
#
# Tests that pass genre_pack_search_paths explicitly to
# WebSocketSessionHandler (e.g. test_session_handler_slug_resumed.py) are
# NOT affected — they construct their own loader with a fixed path and add
# their own pytest.skip guards for when sidequest-content is missing.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Genre-pack cache patch — replace GenreLoader.load() with a cached version
# for the entire test session. Real content packs (caverns_and_claudes,
# spaghetti_western, ...) load in ~100-200 ms each; tests that load many
# packs add up to 20+ s without this. Deep-copy on return so tests that
# mutate ``pack.scenarios`` / ``pack.worlds`` in-place never leak state
# into the next test.
# ---------------------------------------------------------------------------


def _install_genre_loader_cache_patch() -> None:
    import copy as _copy

    import sidequest.genre.loader as _genre_loader_mod

    _pack_cache: dict[str, object] = {}
    original_load = _genre_loader_mod.GenreLoader.load

    def _cached_load(self, code):  # noqa: ANN001
        # Include search_paths in the cache key to prevent cross-contamination
        # when tests load from different directories
        cache_key = (str(code), tuple(str(p) for p in self.search_paths))
        if cache_key not in _pack_cache:
            _pack_cache[cache_key] = original_load(self, code)
        return _copy.deepcopy(_pack_cache[cache_key])

    if getattr(_genre_loader_mod.GenreLoader.load, "_is_test_cache", False):
        return
    _cached_load._is_test_cache = True  # type: ignore[attr-defined]
    _genre_loader_mod.GenreLoader.load = _cached_load


_install_genre_loader_cache_patch()


@pytest.fixture(autouse=True)
def _watcher_hub_event_store_isolation():
    """Autouse guard: clear the watcher_hub ``_telemetry_sink`` binding
    between tests (ADR-115 D5 renamed ``_event_store`` → ``_telemetry_sink``).

    Several tests reach the slug-connect handler path, which calls
    ``bind_event_store(sink)`` on a TelemetrySink that lives only for the
    duration of that test. Without this fixture, the binding survives —
    the sink's pool gets closed by session teardown (or by the test going
    out of scope), but the global pointer in
    ``sidequest.telemetry.watcher_hub`` still references the dead handle.
    The next test that publishes a persistable event would then hit a
    closed-pool error (full-suite flake — passes in isolation).

    This fixture restores the pre-test binding state on teardown so each
    test starts with whatever binding it sets up itself (typically None).
    """
    from sidequest.telemetry import watcher_hub

    prior = watcher_hub._telemetry_sink
    yield
    watcher_hub._telemetry_sink = prior


@pytest.fixture(autouse=True)
def _fixture_pack_search_paths(monkeypatch):
    """Autouse guard: point DEFAULT_GENRE_PACK_SEARCH_PATHS at the frozen
    fixture pack directory so genre resolution never reaches sidequest-content.
    """
    monkeypatch.setattr(
        "sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [_FIXTURE_PACKS_DIR],
    )


@pytest.fixture(autouse=True)
def _reset_daemon_state_mirror():
    """Autouse guard (story 45-31): reset the process-wide daemon state
    mirror between tests so a force_unresponsive_for_test() call in one
    test does not leak its UNRESPONSIVE state into the next test's
    dispatch path. The mirror is a builtins-pinned singleton, so
    ordinary fixtures cannot scope it without explicit clearing."""
    from sidequest.daemon_client.state_mirror import get_mirror

    get_mirror().clear_for_test()
    yield
    get_mirror().clear_for_test()


@pytest.fixture(autouse=True)
def _default_archetype_hints(monkeypatch):
    """Autouse guard: defeat the chargen archetype gate (Story 45-6) for
    every server test that drives chargen confirmation without walking
    real hint-bearing scenes.

    Wraps ``CharacterBuilder.accumulated`` to stamp ``hero`` / ``tank``
    hints when a test left them None. ``hero`` / ``tank`` is valid in
    ``caverns_and_claudes/archetype_constraints.yaml`` and lets the
    archetype resolver succeed for packs WITH axes — the gate's
    OK_RESOLVED branch fires.

    For packs WITHOUT axes (``mutant_wasteland``, ``spaghetti_western``
    fixtures), this would normally trip the gate's
    ``raw_pair_unresolved`` branch because the hints become a raw pair
    with no resolver to consume them. Defended by also wrapping the
    archetype-gate handler to downgrade ``raw_pair_unresolved`` to a
    pass when the resolved_archetype is exactly the synthetic
    ``hero/tank`` we injected — that combination cannot occur from real
    chargen scenes on an axes-less pack.

    Tests in ``test_45_6_chargen_archetype_gate.py`` install their own
    ``_inject_hints`` wrapper that LIFO-shadows part 1 and explicitly
    sets hints to None for the blocked-branch scenarios; the
    ``raw_pair_unresolved`` test there sets hints to non-default values
    so the synthetic-pair downgrade does not fire.
    """
    from sidequest.game.builder import AccumulatedChoices, CharacterBuilder

    real_accumulated = CharacterBuilder.accumulated

    def fake_accumulated(self) -> AccumulatedChoices:  # noqa: ANN001
        acc = real_accumulated(self)
        if acc.jungian_hint is None and acc.rpg_role_hint is None:
            acc.jungian_hint = "hero"
            acc.rpg_role_hint = "tank"
        return acc

    fake_accumulated._is_default_archetype_hints_fake = True  # type: ignore[attr-defined]
    monkeypatch.setattr(CharacterBuilder, "accumulated", fake_accumulated)

    # Defend the axes-less-pack case: when WE stamped "hero/tank" onto a
    # Character whose pack lacks archetype axes, the builder writes
    # resolved_archetype="hero/tank" (raw pair). The gate would then
    # trip raw_pair_unresolved. Clear the synthetic raw pair before the
    # gate evaluates so the OK_NO_AXES branch fires. The downgrade only
    # fires when our `accumulated` wrapper is still on top — a test that
    # installs its own LIFO-shadowing `_inject_hints` (notably
    # test_pack_axisless_with_set_hints_blocks_with_raw_pair_unresolved)
    # replaces the function and the marker disappears, so the real gate
    # runs end-to-end on its own hints.
    # Import via session_handler back-compat re-export to avoid the
    # websocket_session_handler ↔ session_handler circular import that
    # only surfaces when nothing else has loaded the chain yet.
    from sidequest.server.session_handler import WebSocketSessionHandler

    real_gate = WebSocketSessionHandler._gate_archetype_resolution

    def fake_gate(self, character, sd, player_id, span):  # noqa: ANN001, ANN201
        ours = getattr(
            CharacterBuilder.accumulated,
            "_is_default_archetype_hints_fake",
            False,
        )
        pack = sd.genre_pack
        pack_has_axes = pack.base_archetypes is not None and pack.archetype_constraints is not None
        if (
            ours
            and not pack_has_axes
            and character.resolved_archetype == "hero/tank"
            and character.archetype_provenance is None
        ):
            character.resolved_archetype = None
        return real_gate(self, character, sd, player_id, span)

    monkeypatch.setattr(WebSocketSessionHandler, "_gate_archetype_resolution", fake_gate)


# ---------------------------------------------------------------------------
# ClaudeClient guard — autouse. Prevents any server test from spawning a
# real ``claude -p`` subprocess. Without this guard, every test that runs
# through ``_handle_player_action`` fires a real Claude subprocess for the
# narrator path, each with its own multi-second startup.
#
# The Intent Router producer (renamed from LocalDM by Story 59-2) does NOT
# need this guard — it lives behind the Anthropic SDK adapter, not behind
# ``claude -p``, and isn't on the live turn path until Story 59-4.
# ---------------------------------------------------------------------------


_FAKE_NARRATION_TEXT = (
    "The world takes shape around you. Light filters through the morning "
    "haze and the day begins.\n\n"
    "```game_patch\n{}\n```"
)


def _fake_dispatch_package_json(turn_id: str = "t-fake") -> str:
    """Minimum valid DispatchPackage JSON for the producer fake."""
    return '{"turn_id":"' + turn_id + '","per_player":[],"cross_player":[],"confidence_global":0.0}'


class _FakeClaudeClient:
    """In-process ``LlmClient`` that never spawns a subprocess.

    Accepts any ``__init__`` args because production code constructs it as
    a zero-arg factory (``self._client_factory = ClaudeClient``) but
    ``ClaudeClient.__init__`` also takes ``timeout``/``command_path``/etc.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self._session_id = "fake-session"

    async def send(self, prompt: str) -> ClaudeResponse:  # noqa: ARG002
        return ClaudeResponse(
            text=_FAKE_NARRATION_TEXT,
            session_id=self._session_id,
            input_tokens=10,
            output_tokens=10,
        )

    async def send_with_model(
        self,
        prompt: str,
        model: str,  # noqa: ARG002
    ) -> ClaudeResponse:
        return self._respond_for_model(model)

    async def send_with_session(
        self,
        prompt: str,  # noqa: ARG002
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,  # noqa: ARG002
        allowed_tools: list[str] | None = None,  # noqa: ARG002
        env_vars: dict[str, str] | None = None,  # noqa: ARG002
    ) -> ClaudeResponse:
        return self._respond_for_model(
            model,
            session_id=session_id or self._session_id,
        )

    def _respond_for_model(
        self,
        model: str,
        session_id: str | None = None,
    ) -> ClaudeResponse:
        text = _fake_dispatch_package_json() if model == "haiku" else _FAKE_NARRATION_TEXT
        return ClaudeResponse(
            text=text,
            session_id=session_id or self._session_id,
            input_tokens=10,
            output_tokens=10,
        )


@pytest.fixture(autouse=True)
def _mock_claude_client(monkeypatch):
    """Autouse guard: replace narrator-client construction at every import site.

    ``from sidequest.agents.claude_client import ClaudeClient`` creates a
    fresh per-module binding, so ``monkeypatch.setattr`` on the original
    module does NOT propagate to consumers. Patch the two sites that
    construct one inline:

    - ``orchestrator.ClaudeClient`` — Orchestrator's default narrator client
      (pre-61-9 legacy default; replaced by injection in production).
    - ``websocket_session_handler.build_llm_client`` — post-61-9 the factory
      default in ``WebSocketSessionHandler`` calls
      ``build_llm_client(purpose="narrator")``; redirect that to the fake
      so tests that omit ``claude_client_factory`` do not hit the real SDK
      (which would raise on missing ``ANTHROPIC_API_KEY``).

    Tests that want to inspect prompts install their own mock via
    ``monkeypatch.setattr`` / ``claude_client_factory=`` — those shadow
    this guard for the duration of that test and teardown unwinds in LIFO.
    """
    monkeypatch.setattr(
        "sidequest.agents.orchestrator.ClaudeClient",
        _FakeClaudeClient,
    )
    monkeypatch.setattr(
        "sidequest.server.websocket_session_handler.build_llm_client",
        lambda *, purpose="narrator": _FakeClaudeClient(),
    )


@pytest.fixture(autouse=True)
def _stub_intent_router_factory(monkeypatch):
    """Autouse guard (Story 59-4, ADR-113): replace the production
    Intent Router factory with an in-process stub so the live wiring in
    ``_execute_narration_turn`` does NOT spawn a real Anthropic SDK
    client during tests.

    Mirrors the ``_mock_claude_client`` pattern above: tests MUST NOT
    spawn a real Claude client. The production factory
    (``intent_router_pass.build_intent_router_for_session``) constructs an
    ``AnthropicSdkClient`` which eagerly validates ``ANTHROPIC_API_KEY``,
    so without this guard every server test that exercises
    ``_handle_player_action`` would fail at router construction.

    The stub returns an ``IntentRouter`` whose ``decompose`` yields an
    empty ``DispatchPackage`` — no dispatches to engage, so the
    dispatch bank no-ops and the pre-narrator pass is effectively a
    pass-through. Tests that need to drive a non-empty router output
    install their own monkeypatch (which shadows this guard).
    """
    from sidequest.protocol.dispatch import DispatchPackage

    async def _empty_decompose(*, action: str, state_summary: object) -> DispatchPackage:  # noqa: ARG001
        return DispatchPackage(turn_id="test-stub", confidence_global=0.0)

    stub_router = MagicMock()
    stub_router.decompose = _empty_decompose

    monkeypatch.setattr(
        "sidequest.server.intent_router_pass.build_intent_router_for_session",
        lambda: stub_router,
    )


def canned_claude_response(
    *,
    text: str | None = None,
    session_id: str = "test-session",
) -> ClaudeResponse:
    """Build a minimally-valid :class:`ClaudeResponse` for narration tests.

    The orchestrator's ``game_patch`` extraction regex runs on
    ``text``; an empty / missing fence block is fine — extraction
    falls back to ``{}`` and the narration pipeline completes. Tests
    that care about state deltas override ``text`` to include a
    ```game_patch``` fence.
    """
    return ClaudeResponse(
        text=text
        or (
            "The world takes shape around you. Light filters through the "
            "morning haze and the day begins.\n\n"
            "```game_patch\n{}\n```"
        ),
        session_id=session_id,
        input_tokens=100,
        output_tokens=60,
    )


def make_mock_claude_client(
    *,
    text: str | None = None,
    session_id: str = "test-session",
) -> MagicMock:
    """Return a Claude client mock with both ``send_with_session`` and
    ``send_stateless`` wired to yield a canned :class:`ClaudeResponse`.

    Post-ADR-098 the narrator uses ``send_stateless``; ``send_with_session``
    is preserved here for backward compatibility with tests that still
    drive that API.

    Tests that want to inspect the prompt sent to Claude can access either
    ``mock.send_stateless`` or ``mock.send_with_session`` (both are
    :class:`AsyncMock`) and inspect ``call_args`` after invocation.
    """
    mock = MagicMock()
    response = canned_claude_response(text=text, session_id=session_id)
    mock.send_with_session = AsyncMock(return_value=response)
    mock.send_stateless = AsyncMock(return_value=response)
    return mock


def mock_claude_client_factory(
    *,
    text: str | None = None,
    session_id: str = "test-session",
):
    """Factory suitable for ``WebSocketSessionHandler(claude_client_factory=...)``."""
    client = make_mock_claude_client(text=text, session_id=session_id)
    return lambda: client


@pytest.fixture
def session_handler_factory(tmp_path):
    """Return a factory callable with two calling conventions.

    **Single-player (legacy):** ``factory(genre="caverns_and_claudes")``
    Returns ``(sd, handler)`` — a minimal ``_SessionData`` +
    ``WebSocketSessionHandler`` suitable for unit-testing
    ``_execute_narration_turn`` without a real WebSocket or LLM call. The
    test is responsible for overriding
    ``sd.orchestrator.run_narration_turn`` with an ``AsyncMock``.

    **Multiplayer (ADR-036):**
    ``factory(slug=..., mode=GameMode.MULTIPLAYER, seat_players=[...], active_player=(...))``
    Returns ``(handler, sd, room)`` — a fully wired multi-player setup where
    ``handler._room`` is a ``SessionRoom`` with the given players seated.

    Task 11 (story 3.4): used by test_confrontation_dispatch_wiring.py.
    Task 16 (story 3.4): snapshot now includes a Character named "Rux" so
    XP-award tests can inspect ``sd.snapshot.characters[0].core.xp``.
    Task 3 (ADR-036): extended with multiplayer calling convention.
    """

    import sidequest.genre.loader as _genre_loader_mod
    from sidequest.agents.orchestrator import Orchestrator
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.game.sqlite_repository import SqliteSaveRepository
    from sidequest.genre.loader import GenreLoader
    from sidequest.server.session_handler import (
        WebSocketSessionHandler,
        _SessionData,
        _State,
    )
    from sidequest.server.session_room import SessionRoom

    def _make(
        genre: str = "caverns_and_claudes",
        *,
        slug: str | None = None,
        mode: GameMode | None = None,
        seat_players: list[tuple[str, str]] | None = None,
        active_player: tuple[str, str] | None = None,
        existing_room: SessionRoom | None = None,
    ):
        # Read DEFAULT_GENRE_PACK_SEARCH_PATHS from the module at call-time so
        # that the _fixture_pack_search_paths monkeypatch is visible here.
        pack = GenreLoader(_genre_loader_mod.DEFAULT_GENRE_PACK_SEARCH_PATHS).load(genre)
        snap = GameSnapshot(genre_slug=genre)
        core = CreatureCore(
            name="Rux",
            description="A stoic fighter",
            personality="stoic",
            inventory=Inventory(),
        )
        char = Character(
            core=core,
            char_class="Fighter",
            race="Human",
            backstory="A wandering fighter",
        )
        snap.characters.append(char)
        store = SqliteStore.open_in_memory()
        orch = MagicMock(spec=Orchestrator)

        # Determine player identity — defaults to legacy single-player "Rux".
        if active_player is not None:
            active_pid, active_name = active_player
        else:
            active_pid, active_name = "player-1", "Rux"

        sd = _SessionData(
            genre_slug=genre,
            world_slug="",
            player_name=active_name,
            player_id=active_pid,
            snapshot=snap,
            repository=SqliteSaveRepository(store),
            dungeon_repository=MagicMock(),
            telemetry_sink=MagicMock(),
            genre_pack=pack,
            orchestrator=orch,
        )
        handler = WebSocketSessionHandler(save_dir=tmp_path)
        handler._session_data = sd
        # Task E.2 wiring: every turn flowing through this handler will hit
        # ``_apply_narration_result_to_snapshot`` which requires
        # ``sd._room``. The MP path below replaces this with the seated
        # SessionRoom; the legacy single-player path falls through with
        # this binding intact.
        sd._room = room_for(snap, slug=genre)

        # ---- Multiplayer room wiring (ADR-036 Task 3) ----
        if slug is not None and mode is not None and seat_players is not None:
            # Force handler into Playing state so _handle_player_action reaches
            # the barrier logic (MP tests start post-chargen). Only done here —
            # not for the legacy single-player path — so tests that probe
            # pre-connect guard behaviour (e.g. test_dice_throw_returns_error_when_not_playing)
            # still see AwaitingConnect.
            handler._state = _State.Playing

            if existing_room is not None:
                # Share the existing room — reuse its snapshot + repository so
                # the TurnManager barrier state is shared across handlers.
                # Post-D2 SessionRoom.store is a SaveRepository, not a raw
                # SqliteStore — pass it straight through as repository=.
                room = existing_room
                snap = room.snapshot
                shared_repository = room.store
                # Rebuild _SessionData against the shared snapshot/repository.
                if active_player is not None:
                    active_pid, active_name = active_player
                else:
                    active_pid, active_name = "player-1", "Rux"
                sd = _SessionData(
                    genre_slug=genre,
                    world_slug="",
                    player_name=active_name,
                    player_id=active_pid,
                    snapshot=snap,
                    repository=shared_repository,
                    dungeon_repository=MagicMock(),
                    telemetry_sink=MagicMock(),
                    genre_pack=sd.genre_pack,
                    orchestrator=sd.orchestrator,
                )
                sd.repository.save = MagicMock()
                sd.repository.append_narrative = MagicMock()
                sd._room = room
                handler._session_data = sd
                handler._room = room
                return handler, sd, room

            # In MP mode, add a Character to the snapshot for each seat so
            # that _resolve_acting_character_name can match by slot name.
            # The legacy "Rux" character added above stays for compatibility
            # but we also add one per seated player.
            existing_names = {c.core.name for c in snap.characters}
            for _pid, character_slot in seat_players:
                if character_slot not in existing_names:
                    mp_core = CreatureCore(
                        name=character_slot,
                        description=f"{character_slot} the adventurer",
                        personality="bold",
                        inventory=Inventory(),
                    )
                    mp_char = Character(
                        core=mp_core,
                        char_class="Fighter",
                        race="Human",
                        backstory="A wandering adventurer",
                    )
                    snap.characters.append(mp_char)
                    existing_names.add(character_slot)

            room = SessionRoom(slug=slug, mode=mode)
            # Bind a snapshot + repository so the room is fully initialised.
            # bind_world expects a SaveRepository (post-D2); the primary
            # construction above already wrapped this SQLite store in one,
            # so reuse the same repository instance the session_data holds.
            room.bind_world(snapshot=snap, store=sd.repository)
            # Connect and seat every player. The fixture's intent is a
            # post-chargen "in-game" room, so each peer is promoted to
            # PLAYING — this is what existing barrier tests assume and
            # what Story 45-2 made explicit. Tests that need a CHARGEN /
            # ABANDONED scenario override `_seated[pid].state` directly.
            for i, (pid, character_slot) in enumerate(seat_players):
                room.connect(pid, socket_id=f"sock-{i}")
                room.seat(pid, character_slot=character_slot)
                room.transition_to_playing(pid)
                # Wave 2B (story 45-48): party_location() reads
                # snapshot.player_seats; mirror the room's seat map so
                # the per-character location accessors work in tests.
                snap.player_seats[pid] = character_slot
            handler._room = room
            sd._room = room
            # Silence broadcast so tests don't need a real WebSocket.
            room.broadcast = MagicMock()  # type: ignore[method-assign]
            # Silence repository side-effects. Production calls
            # sd.repository.save / append_narrative (post-D1); these patches
            # also fill the two methods SqliteSaveRepository does not define.
            sd.repository.save = MagicMock()
            sd.repository.append_narrative = MagicMock()
            return handler, sd, room

        # Legacy return: (sd, handler).
        return sd, handler

    return _make


# ---------------------------------------------------------------------------
# Group B Task 10 — session_fixture helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session_fixture():
    """Return ``(sd, handler)`` — a minimal in-memory _SessionData + its handler.

    ``sd.orchestrator`` is a ``MagicMock`` — tests that exercise the narrator
    path override ``run_narration_turn`` via ``patch.object``.

    The handler is a :class:`WebSocketSessionHandler` wired to a stub
    save directory; its ``_session_data`` attribute is set to ``sd`` so
    ``_execute_narration_turn`` can be called directly without going through
    the full connect handshake.
    """
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.session_handler import (
        WebSocketSessionHandler,
        _SessionData,
    )

    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        turn_manager=TurnManager(interaction=1),
    )
    snap.character_locations["TestHero"] = "Main Hall"
    snap.player_seats["player:TestHero"] = "TestHero"
    _mock_repo = MagicMock()
    _mock_repo.save = MagicMock()
    _mock_repo.append_narrative = MagicMock()
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        player_name="TestHero",
        player_id="player:TestHero",
        snapshot=snap,
        repository=_mock_repo,
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
    )
    # Task E.2 wiring: ``_apply_narration_result_to_snapshot`` (called by
    # ``_execute_narration_turn``) now requires ``room=sd._room``. The
    # production slug-connect path always populates ``sd._room``; tests
    # that drive a turn through this fixture must too. Bind a fresh
    # SessionRoom over the fixture's snapshot so the front-door scene-end
    # call site has a real Session to dispatch into.
    sd._room = room_for(snap, slug="sunken_keep")

    handler = WebSocketSessionHandler(save_dir=Path("/tmp/sq-test-never-used"))
    handler._session_data = sd
    return sd, handler


def _build_turn_context_for_test(sd):
    """Build a minimal :class:`TurnContext` from session state.

    Mirrors the shape that ``_build_turn_context`` produces so that
    ``_execute_narration_turn`` receives a plausible context object.
    """
    from sidequest.agents.orchestrator import TurnContext

    return TurnContext(
        state_summary="(test state summary)",
        genre=sd.genre_slug,
        character_name=sd.player_name,
        current_location=(sd.snapshot.party_location(perspective=sd.player_name) or "Unknown"),
        npc_pool=list(getattr(sd.snapshot, "npc_pool", [])),
    )


def _make_minimal_narration_turn_result(narration: str = "ok"):
    """Construct a :class:`NarrationTurnResult` with minimum required fields."""
    from sidequest.agents.orchestrator import NarrationTurnResult

    return NarrationTurnResult(
        narration=narration,
        is_degraded=False,
        agent_duration_ms=1,
    )


# ---------------------------------------------------------------------------
# Dual-track momentum fixtures (Task 11)
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_two_dial_pack():
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ),
            BeatDef.model_validate(
                {
                    "id": "defend",
                    "label": "Defend",
                    "kind": "brace",
                    "base": 1,
                    "stat_check": "CON",
                }
            ),
            BeatDef.model_validate(
                {
                    "id": "flee",
                    "label": "Flee",
                    "kind": "push",
                    "base": 1,
                    "stat_check": "DEX",
                }
            ),
            BeatDef.model_validate(
                {
                    "id": "feint",
                    "label": "Feint",
                    "kind": "angle",
                    "target_tag": "Off-Balance",
                    "stat_check": "DEX",
                }
            ),
        ],
    )
    # GenrePack requires many fields; use MagicMock for everything except
    # rules so the encounter engine can look up confrontation defs without
    # loading a full pack from disk.
    from unittest.mock import MagicMock

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    return pack


@pytest.fixture
def dual_dial_test_setup(synthetic_two_dial_pack):
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    class _Setup:
        def __init__(self, encounter, pack):
            self.encounter = encounter
            self.pack = pack

        def run_dice_throw(self, *, beat_id, faces, modifier):
            payload = DiceThrowPayload(
                request_id="r1",
                throw_params=ThrowParams(
                    velocity=(0, 0, 0),
                    angular=(0, 0, 0),
                    position=(0, 0),
                ),
                face=faces,
                beat_id=beat_id,
            )
            from sidequest.game.session import GameSnapshot
            from sidequest.game.turn import TurnManager

            snapshot = GameSnapshot(
                genre_slug="test",
                world_slug="test",
                turn_manager=TurnManager(),
            )
            return dispatch_dice_throw(
                payload=payload,
                rolling_player_id="p1",
                character_name="Sam",
                character_stats={"STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
                encounter=self.encounter,
                pack=self.pack,
                genre_slug="test",
                session_id="s1",
                round_number=1,
                room_broadcast=None,
                snapshot=snapshot,
            )

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="Sam", role="combatant", side="player")],
    )
    return _Setup(encounter=enc, pack=synthetic_two_dial_pack)


@pytest.fixture
def snapshot_with_pack(synthetic_two_dial_pack):
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    return snap, synthetic_two_dial_pack


@pytest.fixture
def character_named_sam():
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name="Sam",
        description="A scrappy survivor",
        personality="gritty",
        inventory=Inventory(),
    )
    return Character(
        core=core,
        char_class="Rogue",
        race="Human",
        backstory="A wandering survivor.",
    )


# ---------------------------------------------------------------------------
# Task 20 — store_bound_to_hub + encounter_dispatch_helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_bound_to_hub(synthetic_two_dial_pack):
    """Open an in-memory SqliteStore, bind it to the watcher hub, yield
    (store, snapshot, pack).  Unbinds on teardown so other tests see no
    leftover binding.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.telemetry.watcher_hub import bind_event_store

    store = SqliteStore.open_in_memory()
    bind_event_store(store)

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    core = CreatureCore(
        name="Sam",
        description="A scrappy survivor",
        personality="gritty",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Rogue",
        race="Human",
        backstory="A wandering survivor.",
    )
    snap.characters.append(char)

    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )

    try:
        yield store, snap, synthetic_two_dial_pack
    finally:
        bind_event_store(None)


@pytest.fixture
def encounter_dispatch_helper():
    """Helper that drives beats through the narration_apply path.

    Methods:
    - run_player_attack(snapshot, pack, beat_id, outcome) — apply one player beat
    - run_to_resolution(snapshot, pack, winner) — drive opponent beats until resolved
    """
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
    from sidequest.protocol.dice import RollOutcome
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    class _Helper:
        def run_player_attack(self, snapshot, pack, *, beat_id="attack", outcome="Success"):
            outcome_enum = RollOutcome(outcome)
            result = NarrationTurnResult(
                narration="Sam swings.",
                beat_selections=[
                    BeatSelection(
                        actor="Sam",
                        beat_id=beat_id,
                        outcome=outcome_enum,
                    )
                ],
                npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
            )
            # ``from_explicit_action=True`` simulates the dice-dispatch
            # path (the only legitimate route for PC beats post Playtest
            # 2026-04-26 [S2-BUG] SOUL-gate). Production session_handler
            # never sets this flag — it always treats narrator-extracted
            # beats as inferred and rejects PC-side selections.
            _apply_narration_result_to_snapshot(
                snapshot,
                result,
                "Sam",
                pack=pack,
                from_explicit_action=True,
                room=room_for(snapshot),
            )

        def run_to_resolution(self, snapshot, pack, *, winner="opponent"):
            """Drive opponent beats until a threshold is crossed.

            Repeatedly applies "attack" beats on the winning side until
            snapshot.encounter.resolved is True.
            """
            outcome_enum = RollOutcome.Success
            for _ in range(20):
                if snapshot.encounter is None or snapshot.encounter.resolved:
                    break
                actor_name = "Promo" if winner == "opponent" else "Sam"
                result = NarrationTurnResult(
                    narration=f"{actor_name} strikes.",
                    beat_selections=[
                        BeatSelection(
                            actor=actor_name,
                            beat_id="attack",
                            outcome=outcome_enum,
                        )
                    ],
                    npcs_present=[
                        NpcMention(
                            name="Promo",
                            side="opponent",
                            role="hostile",
                        )
                    ],
                )
                _apply_narration_result_to_snapshot(
                    snapshot,
                    result,
                    "Sam",
                    pack=pack,
                    room=room_for(snapshot),
                )

    return _Helper()


# ---------------------------------------------------------------------------
# OTEL span-capture fixture (shared across server-layer tests).
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture():
    """In-memory OTEL span exporter for span-assertion tests.

    Installs a ``SimpleSpanProcessor`` with an ``InMemorySpanExporter`` on
    the global ``TracerProvider`` so spans opened via ``Span.open`` (and
    any direct ``tracer.start_as_current_span`` call site) land in
    ``exporter.get_finished_spans()``.

    Yields the exporter; tears down the processor on exit so spans from
    one test never bleed into the next.

    **Provider reset (45-36 fix):** ``SimpleSpanProcessor.shutdown()``
    flushes pending spans but does NOT deregister the processor from the
    global ``TracerProvider``. Across a pytest session that uses this
    fixture N times, the provider accumulates N stale processors — they
    are shut-down so they no-op on emit, but they leak memory and any
    test that introspects the processor list sees ghosts. We clear the
    underlying ``_span_processors`` tuple before adding ours so each
    test starts from a clean slate.

    Replacing the ``TracerProvider`` itself does NOT work because
    production modules cache ``otel_trace.get_tracer(__name__)`` at
    import time (e.g., ``server/dispatch/chargen_loadout.py``) and a
    provider swap leaves those tracers bound to the old (now-orphaned)
    provider, which silently drops every span those modules emit.

    Reusable across stories — Story 45-10 (scrapbook coverage), Story 45-3
    (dice-throw momentum span), and any future server-layer test that
    asserts span emission. ``test_dice_throw_momentum_span.py`` still
    defines a local copy that shadows this fixture; it can be migrated in
    a follow-up.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)

    # Drop accumulated processors from prior invocations — see
    # "Provider reset (45-36 fix)" above.
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


@pytest.fixture
def otel_exporter():
    """Isolated in-memory OTEL exporter via ``_spans.tracer`` monkey-patch.

    Companion to :func:`otel_capture`. Both yield an
    ``InMemorySpanExporter`` for span-assertion tests, but the two patterns
    differ in scope:

    - ``otel_capture`` installs a processor on the *global* TracerProvider
      (via :func:`sidequest.telemetry.setup.init_tracer`). Use it when you
      want any span emitted anywhere during the test to land in the
      exporter — including those emitted by lazily-cached module-level
      tracers (``otel_trace.get_tracer(__name__)`` at import time).
    - ``otel_exporter`` builds a *fresh* TracerProvider and patches
      ``sidequest.telemetry.spans.tracer`` to return its tracer. Use it
      when you want strict per-test isolation and only care about spans
      emitted via :class:`~sidequest.telemetry.spans.span.Span.open`
      (which resolves the tracer lazily via the patched callable). Spans
      from modules that cache their tracer at import time will NOT be
      captured by this fixture.

    Use ``otel_capture`` by default; reach for ``otel_exporter`` when a
    test needs guaranteed isolation from prior fixture state.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry import spans as _spans

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = _spans.tracer
    _spans.tracer = lambda: provider.get_tracer("test")
    try:
        yield exporter
    finally:
        _spans.tracer = original


def span_attrs_by_name(
    exporter,
    span_name: str,
) -> list[dict]:
    """Return attribute dicts for every finished span with ``name == span_name``.

    Helper for span-assertion tests — extracts the ``attributes`` dict
    from each matching span in the exporter. Returns an empty list if no
    spans match. Used by Story 50-5 scenario clue intake tests and
    extensible to any future test that needs to assert span attribute
    payloads.
    """
    return [
        dict(span.attributes or {})
        for span in exporter.get_finished_spans()
        if span.name == span_name
    ]
