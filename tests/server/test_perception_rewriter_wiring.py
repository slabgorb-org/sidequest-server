"""Group G Task 8 — PerceptionRewriter wiring into the turn driver fan-out.

Proves two wiring invariants:

1. ``views.status_effects_by_player(handler)`` reads the existing
   character-status map from the live ``GameSnapshot`` — no new tracking
   state is introduced. A blinded character on ``snapshot.characters[0]``
   surfaces as ``{sd.player_id: ["blinded"]}``.

2. ``_emit_event`` invokes ``rewrite_for_recipient`` in the per-recipient
   fan-out loop after ``ProjectionFilter.project`` returns ``include=True``
   and before the frame is handed to the recipient's queue. Asserted
   end-to-end by seating a NARRATION event with visual/audio spans and
   spying on the rewriter call via monkeypatch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.session import GameSnapshot
from sidequest.game.sqlite_repository import SqliteSaveRepository
from sidequest.server import views
from sidequest.server.session_handler import (
    WebSocketSessionHandler,
    _SessionData,
)
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "perception-rewriter-wiring"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _make_character(*, name: str, statuses: list[str]) -> Character:
    core = CreatureCore(
        name=name,
        description="A test subject",
        personality="Stoic",
        inventory=Inventory(),
        statuses=statuses,
    )
    return Character(core=core, char_class="Fighter", race="Human", backstory="A wanderer.")


def _make_handler_with_character(tmp_path: Path, *, statuses: list[str]) -> WebSocketSessionHandler:
    """Construct a handler with a minimal _SessionData carrying a character
    whose ``statuses`` list drives ``views.status_effects_by_player``."""
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    # Minimal fake _SessionData — enough that views.status_effects_by_player
    # can read snapshot.characters[0].core.statuses.
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [_make_character(name="Thorn", statuses=statuses)]
    handler._session_data = _SessionData.__new__(_SessionData)
    handler._session_data.snapshot = snap
    handler._session_data.player_id = "alice"
    handler._session_data.genre_slug = _GENRE
    handler._session_data.world_slug = _WORLD
    return handler


# ---------------------------------------------------------------------------
# 1. status_effects_by_player accessor
# ---------------------------------------------------------------------------


def test_status_effects_by_player_reads_snapshot_statuses(tmp_path: Path) -> None:
    handler = _make_handler_with_character(tmp_path, statuses=["blinded"])
    assert views.status_effects_by_player(handler) == {"alice": ["blinded"]}


def test_status_effects_by_player_empty_when_no_session(tmp_path: Path) -> None:
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    assert views.status_effects_by_player(handler) == {}


def test_status_effects_by_player_empty_when_no_characters(tmp_path: Path) -> None:
    handler = WebSocketSessionHandler(save_dir=tmp_path, genre_pack_search_paths=[_FIXTURE_PACKS])
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    handler._session_data = _SessionData.__new__(_SessionData)
    handler._session_data.snapshot = snap
    handler._session_data.player_id = "alice"
    handler._session_data.genre_slug = _GENRE
    handler._session_data.world_slug = _WORLD
    assert views.status_effects_by_player(handler) == {}


# ---------------------------------------------------------------------------
# 2. _emit_event invokes rewrite_for_recipient
# ---------------------------------------------------------------------------


def _seed_game_row(tmp_path: Path) -> SqliteStore:
    db = db_path_for_slug(tmp_path, _SLUG)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=_SLUG,
        mode=GameMode.SOLO,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    return store


def test_emit_event_calls_rewriter_per_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spy on rewrite_for_recipient and assert _emit_event calls it with the
    (canonical_payload, viewer_player_id, status_effects) triple for every
    included recipient."""
    handler = _make_handler_with_character(tmp_path, statuses=["blinded"])

    # Minimal event log + projection filter (no genre rules — filter is
    # effectively pass-through so include=True for both recipients).
    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    event_log = EventLog(repo)
    projection_filter = ComposedFilter.with_no_genre_rules()
    projection_cache = ProjectionCache(repo)
    handler._event_log = event_log
    handler._projection_filter = projection_filter
    handler._projection_cache = projection_cache

    # Room with alice (emitter) + bob (recipient). Both connected.
    # Use MULTIPLAYER so two player_ids can share the room (SOLO enforces
    # single-slot occupancy).
    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    alice_queue: asyncio.Queue[object] = asyncio.Queue()
    bob_queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("alice", socket_id="sock-alice")
    room.attach_outbound("sock-alice", alice_queue)
    room.connect("bob", socket_id="sock-bob")
    room.attach_outbound("sock-bob", bob_queue)
    handler._room = room

    # Spy on the rewriter — capture calls and pass-through to the real impl.
    from sidequest.agents import perception_rewriter as pr_module
    from sidequest.server import emitters as emitters_module
    from sidequest.server import session_handler as handler_module

    real_rewrite = pr_module.rewrite_for_recipient
    calls: list[dict] = []

    def spy(*, canonical_payload: dict, viewer_player_id: str, status_effects: dict) -> dict:
        calls.append(
            {
                "viewer": viewer_player_id,
                "spans_in": [s.get("kind") for s in canonical_payload.get("spans", [])],
                "status_effects": dict(status_effects),
            }
        )
        out = real_rewrite(
            canonical_payload=canonical_payload,
            viewer_player_id=viewer_player_id,
            status_effects=status_effects,
        )
        calls[-1]["spans_out"] = [s.get("kind") for s in out.get("spans", [])]
        return out

    # emit_event is now in emitters.py, so we monkeypatch rewrite_for_recipient there
    monkeypatch.setattr(emitters_module, "rewrite_for_recipient", spy)

    # Emit a narration-shaped event via the raw path. Using a plain dict
    # payload sidesteps NarrationPayload's extra="forbid" + missing `spans`
    # field — we're testing the fan-out transform, not the wire schema.
    payload = {
        "text": "A guard slumps into shadow.",
        "spans": [
            {"id": "s1", "kind": "visual_only", "text": "slumps into shadow"},
            {"id": "s2", "kind": "audio_only", "text": "a soft thud"},
        ],
        "_visibility": {"visible_to": "all", "fidelity": {}},
    }

    # Bypass _KIND_TO_MESSAGE_CLS constraints by calling the fan-out logic
    # directly via a dict payload + a fake message_cls that accepts dict
    # payloads. The narrowest path is to invoke _emit_event with a kind
    # that's in the lookup AND populate the payload as a plain dict; the
    # legacy branch (lines ~607-610) rebuilds the recipient message as
    # ``message_cls(payload={**filtered_data, "seq": seq})`` when
    # payload_cls is None.
    #
    # We monkeypatch _KIND_TO_MESSAGE_CLS to register a fake message class
    # that stores the payload verbatim, so we can read back the final
    # dict the rewriter produced.
    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "TEST_KIND", _FakeMsg)

    handler._emit_event("TEST_KIND", payload)

    # One rewriter call per non-emitter recipient (just bob — alice is skipped).
    assert len(calls) == 1, f"expected 1 rewriter call, got {len(calls)}: {calls}"
    call = calls[0]
    assert call["viewer"] == "bob"
    # bob has no status effects (not present in the map — default []).
    # The emitter's blinded status is NOT projected onto bob: status
    # accessor only knows about sd.player_id (alice) until MP seat-to-
    # character wiring lands. So bob's effective fidelity is full,
    # and both spans survive.
    assert call["spans_in"] == ["visual_only", "audio_only"]
    assert call["spans_out"] == ["visual_only", "audio_only"]

    # Frame landed on bob's queue with both spans intact.
    assert bob_queue.qsize() == 1
    bob_frame = bob_queue.get_nowait()
    assert [s["kind"] for s in bob_frame.payload["spans"]] == [
        "visual_only",
        "audio_only",
    ]


def test_emit_event_strips_visual_spans_for_blinded_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the *viewer's* status_effects_by_player entry carries 'blinded',
    the fan-out strips visual_only spans from the payload they receive.

    This is the load-bearing wiring proof: even with pass-through
    ProjectionFilter, the recipient frame is span-filtered purely by the
    rewriter. Asserts the transform runs *after* project() and *before*
    send.
    """
    handler = _make_handler_with_character(tmp_path, statuses=[])
    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    alice_queue: asyncio.Queue[object] = asyncio.Queue()
    bob_queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("alice", socket_id="sock-alice")
    room.attach_outbound("sock-alice", alice_queue)
    room.connect("bob", socket_id="sock-bob")
    room.attach_outbound("sock-bob", bob_queue)
    handler._room = room

    # Override status accessor to mark bob as blinded — seat-to-character
    # wiring isn't plumbed through yet, so we stub the accessor directly.
    # What we're testing here is that *whatever* status map the accessor
    # returns flows into the per-recipient rewriter.
    monkeypatch.setattr(views, "status_effects_by_player", lambda _h: {"bob": ["blinded"]})

    # Fake message class so we can inspect the delivered payload.
    from sidequest.server import session_handler as handler_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "TEST_KIND", _FakeMsg)

    payload = {
        "text": "A guard slumps into shadow.",
        "spans": [
            {"id": "s1", "kind": "visual_only", "text": "slumps into shadow"},
            {"id": "s2", "kind": "audio_only", "text": "a soft thud"},
        ],
        "_visibility": {"visible_to": "all", "fidelity": {}},
    }

    handler._emit_event("TEST_KIND", payload)

    assert bob_queue.qsize() == 1
    bob_frame = bob_queue.get_nowait()
    kinds = [s["kind"] for s in bob_frame.payload["spans"]]
    assert "visual_only" not in kinds, (
        f"blinded bob must not receive visual_only spans; got {kinds}"
    )
    assert "audio_only" in kinds, f"blinded bob must still receive audio_only spans; got {kinds}"


def test_emit_event_preserves_spans_for_unaffected_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sister test: a non-blinded viewer in the same session gets all spans."""
    handler = _make_handler_with_character(tmp_path, statuses=[])
    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    alice_queue: asyncio.Queue[object] = asyncio.Queue()
    bob_queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("alice", socket_id="sock-alice")
    room.attach_outbound("sock-alice", alice_queue)
    room.connect("bob", socket_id="sock-bob")
    room.attach_outbound("sock-bob", bob_queue)
    handler._room = room

    # bob has no status effects.
    monkeypatch.setattr(views, "status_effects_by_player", lambda _h: {})

    from sidequest.server import session_handler as handler_module

    class _FakeMsg:
        def __init__(self, payload):
            self.payload = payload

    monkeypatch.setitem(handler_module._KIND_TO_MESSAGE_CLS, "TEST_KIND", _FakeMsg)

    payload = {
        "text": "A guard slumps into shadow.",
        "spans": [
            {"id": "s1", "kind": "visual_only", "text": "slumps into shadow"},
            {"id": "s2", "kind": "audio_only", "text": "a soft thud"},
        ],
        "_visibility": {"visible_to": "all", "fidelity": {}},
    }

    handler._emit_event("TEST_KIND", payload)

    assert bob_queue.qsize() == 1
    bob_frame = bob_queue.get_nowait()
    kinds = [s["kind"] for s in bob_frame.payload["spans"]]
    assert kinds == ["visual_only", "audio_only"]
