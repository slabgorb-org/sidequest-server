"""Shared MP harness for the ADR-107 aside-channel wiring test (story 50-25).

The plan's Task 5 note assumed a sibling handler test with a real
3-player MP-room fixture to factor from. There is none — every existing
handler test is ``MagicMock``-based. So this harness builds the real
objects from scratch and drives the **real**
``PlayerActionHandler.handle()``:

* real ``SessionRoom`` (MULTIPLAYER) + real ``GameSnapshot`` + real
  ``TurnManager`` + real ``SqliteStore`` + real loaded genre pack;
* the **aside** path runs 100% real (combat-strip, ``AsideResolver``,
  ``aside.resolve`` span, ``room.broadcast``) — it is the feature under
  test and nothing about it is mocked;
* only the **orthogonal narrator** is stubbed: ``_execute_narration_turn``
  records its invocation and performs the *real*
  ``turn_manager.record_interaction()`` the real narrator does after a
  barrier fires (so round-advance stays faithful), and lore retrieval
  returns "" — neither is what the out-of-band guarantee asserts;
* the live Anthropic call is replaced via the real ``build_aside_llm``
  factory seam (``fake_aside_llm``), and the OTEL tracer via the same
  ``sidequest.telemetry.setup.tracer`` monkeypatch the integration
  suite uses.

``submit`` returns the messages the table actually saw this call
(broadcast capture ∪ handler return).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import sidequest.agents.llm_factory as _llm_factory
import sidequest.telemetry.setup as _telemetry_setup
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.handlers.player_action import PlayerActionHandler
from sidequest.protocol.messages import PlayerActionMessage, PlayerActionPayload
from sidequest.server.session_handler import _SessionData, _State
from sidequest.server.session_room import SessionRoom

_CONTENT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
_GENRE = "caverns_and_claudes"
_WORLD = "test_world"


# --------------------------------------------------------------------------- #
# Fakes for the two orthogonal seams (narrator LLM is not under test).
# --------------------------------------------------------------------------- #


class _FakeAsideLlm:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def complete(self, *, system: str, user: str) -> str:
        return self._payload


def fake_aside_llm(payload: str) -> _FakeAsideLlm:
    """An ``AsideLLM`` returning fixed JSON — no live Anthropic call."""
    return _FakeAsideLlm(payload)


class _RaisingAsideLlm:
    """An ``AsideLLM`` whose ``complete()`` raises — simulates an LLM
    call failure/timeout. Spec §6: the handler must NOT crash; the aside
    degrades to a graceful resolver_error answer and no turn is lost."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def complete(self, *, system: str, user: str) -> str:
        raise self._exc


def raising_aside_llm(exc: BaseException | None = None) -> _RaisingAsideLlm:
    """An ``AsideLLM`` that fails the call (default: TimeoutError)."""
    return _RaisingAsideLlm(exc or TimeoutError("aside LLM timed out"))


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class _RecordingTracer:
    def __init__(self) -> None:
        self.span_names: list[str] = []

    @contextlib.contextmanager
    def start_as_current_span(self, name: str):
        self.span_names.append(name)
        yield _RecordingSpan()


class _StubSession:
    """Real-attribute stand-in for ``WebSocketSessionHandler``.

    Exposes exactly what ``PlayerActionHandler.handle`` reads. The aside
    branch returns long before the narrator; the non-aside legs hit the
    real barrier and then the stubbed narrator (which performs the real
    ``record_interaction`` the production narrator does post-dispatch).
    """

    def __init__(self, sd: _SessionData, room: SessionRoom, socket_id: str):
        self._state = _State.Playing
        self._session_data = sd
        self._room = room
        self._socket_id = socket_id
        self.narration_calls = 0

    async def _retrieve_lore_for_turn(self, sd: _SessionData, action: str) -> str:
        return ""

    async def _execute_narration_turn(
        self, sd: _SessionData, action: str, turn_context: Any
    ) -> list[Any]:
        # The real narrator advances the interaction/round counter after a
        # barrier-fired dispatch. Preserve that real side effect so the
        # round-advance assertion tests the real TurnManager; only the
        # LLM prose (orthogonal to out-of-band) is skipped.
        self.narration_calls += 1
        sd.snapshot.turn_manager.record_interaction()
        return []


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _character(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="a test delver",
            personality="curious",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
    )


class MpRoomHarness:
    def __init__(self, players: list[str], llm_aside: _FakeAsideLlm) -> None:
        self._names = list(players)
        self._pid = {n: f"player:{n}" for n in players}
        self._name_by_pid = {v: k for k, v in self._pid.items()}
        self._sid = {n: f"sock:{n}" for n in players}
        self._llm_aside = llm_aside
        self._tracer = _RecordingTracer()

        chars = [_character(n) for n in players]
        self._snap = GameSnapshot(
            genre_slug=_GENRE,
            world_slug=_WORLD,
            characters=chars,
            turn_manager=TurnManager(interaction=1),
        )
        # Spy on the real world-patch seam — the aside path must never
        # call it (structural "no world advance"). GameSnapshot is a
        # frozen pydantic model, so patch the class method (restored in
        # teardown) rather than the instance.
        self._world_patch_calls = 0
        self._orig_apply_world_patch = GameSnapshot.apply_world_patch

        def _counting_patch(snap_self: GameSnapshot, patch: Any) -> None:
            self._world_patch_calls += 1
            self._orig_apply_world_patch(snap_self, patch)

        GameSnapshot.apply_world_patch = _counting_patch  # type: ignore[method-assign]

        self._store = SqliteStore.open_in_memory()
        self._room = SessionRoom(slug=f"{_WORLD}-mp", mode=GameMode.MULTIPLAYER)
        self._room.bind_world(snapshot=self._snap, store=self._store)

        genre_pack = load_genre_pack(_CONTENT / _GENRE)
        self._sessions: dict[str, _StubSession] = {}
        for name, char in zip(players, chars, strict=True):
            pid, sid = self._pid[name], self._sid[name]
            self._room.seat(pid, character_slot=char.core.name)
            self._room.connect(pid, socket_id=sid)
            self._room.attach_outbound(sid, asyncio.Queue())
            # Barrier counts PLAYING peers only (Story 45-2). Chargen is
            # committed in this fixture — every seat is in-world.
            self._room.transition_to_playing(pid)
            sd = _SessionData(
                genre_slug=_GENRE,
                world_slug=_WORLD,
                player_name=name,
                player_id=pid,
                snapshot=self._snap,
                store=self._store,
                genre_pack=genre_pack,
                orchestrator=object(),  # never called on aside/barrier paths
                _room=self._room,
            )
            self._sessions[name] = _StubSession(sd, self._room, sid)

        # Capture every broadcast (real method still runs).
        self._last_recipients: set[str] = set()
        self._captured: list[Any] = []
        _real_broadcast = self._room.broadcast

        def _spy_broadcast(msg: Any, *, exclude_socket_id: str | None = None):
            delivered = _real_broadcast(msg, exclude_socket_id=exclude_socket_id)
            self._captured.append(msg)
            self._last_recipients = {
                self._name_by_pid.get(pid, pid) for _sid, pid in delivered if pid is not None
            }
            return delivered

        self._room.broadcast = _spy_broadcast  # type: ignore[method-assign]

        # Real factory seams: build_aside_llm (imported at call time inside
        # the handler branch) and the OTEL tracer.
        self._orig_build = _llm_factory.build_aside_llm
        self._orig_tracer = _telemetry_setup.tracer
        _llm_factory.build_aside_llm = lambda: self._llm_aside
        _telemetry_setup.tracer = lambda: self._tracer

    # --- introspection (all read REAL state) --------------------------- #

    def narrative_log_count(self) -> int:
        return len(self._snap.narrative_log)

    def scrapbook_count(self) -> int:
        cur = self._store._conn.execute("SELECT count(*) FROM scrapbook_entries")
        return int(cur.fetchone()[0])

    def turn_round(self) -> int:
        return self._snap.turn_manager.round

    def world_patch_count(self) -> int:
        return self._world_patch_calls

    def barrier_fired(self) -> bool:
        return any(s.narration_calls > 0 for s in self._sessions.values())

    def pending_player_ids(self) -> set[str]:
        owing = set(self._names)
        for pid in self._room._pending_actions:
            owing.discard(self._name_by_pid.get(pid, pid))
        return owing

    def last_broadcast_recipients(self) -> set[str]:
        return set(self._last_recipients)

    def spans_named(self, name: str) -> bool:
        return name in self._tracer.span_names

    def teardown(self) -> None:
        _llm_factory.build_aside_llm = self._orig_build
        _telemetry_setup.tracer = self._orig_tracer
        GameSnapshot.apply_world_patch = self._orig_apply_world_patch  # type: ignore[method-assign]


def make_mp_room(*, players: list[str], llm_aside: _FakeAsideLlm) -> MpRoomHarness:
    return MpRoomHarness(players, llm_aside)


async def submit(harness: MpRoomHarness, player: str, text: str, *, aside: bool) -> list[Any]:
    """Drive the REAL handler for ``player``; return what the table saw."""
    session = harness._sessions[player]
    msg = PlayerActionMessage(
        payload=PlayerActionPayload(action=text, aside=aside),
        player_id=harness._pid[player],
    )
    before = len(harness._captured)
    returned = await PlayerActionHandler().handle(session, msg)
    table_saw = list(harness._captured[before:])
    if returned:
        table_saw.extend(returned)
    return table_saw
