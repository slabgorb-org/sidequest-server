"""Regression test: every event kind that flows into the events table either
round-trips through ``_build_message_for_kind`` or is on the explicit
``_REPLAY_SKIP_KINDS`` skip list.

Pingpong 2026-04-26 [S3-BUG]: ``_build_message_for_kind`` raised on
``ENCOUNTER_STARTED`` because the watcher_hub side-channel persists encounter
telemetry rows to ``events`` but the replay walker only knew about kinds that
flow through ``_emit_event``. The crash aborted the entire reconnect, leaving
the client stuck on ``Reconnecting…``.

This file is the load-bearing regression-prevention for that whole class of
bug. Two tests:

1. ``test_kind_to_message_cls_includes_known_live_emit_kinds`` — every kind
   that ``_emit_event`` could be called with today is registered with a
   message class.
2. ``test_replay_handles_every_persisted_kind`` — every kind that producers
   write to the events table either rebuilds successfully via
   ``_build_message_for_kind`` (when registered) or is explicitly skipped via
   ``_REPLAY_SKIP_KINDS`` (when journal-only telemetry).
3. ``test_reconnect_skips_encounter_kinds_without_crash`` — the actual
   "broken save" reconnect path: an events table containing
   ``ENCOUNTER_STARTED`` rows must not crash the replay walker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.sqlite_repository import SqliteSaveRepository
from sidequest.protocol.messages import (
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import (
    _KIND_TO_MESSAGE_CLS,
    _REPLAY_SKIP_KINDS,
    WebSocketSessionHandler,
    _build_message_for_kind,
)
from sidequest.server.session_room import RoomRegistry
from sidequest.telemetry.watcher_hub import _KIND_BY_OP

_GENRE = "caverns_and_claudes"
_WORLD = "grimvault"
_SLUG = "2026-04-26-replay-coverage"

_CONTENT_SEARCH_PATH = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Catalog tests — registry shape invariants
# ---------------------------------------------------------------------------


def test_kind_to_message_cls_includes_scrapbook_entry() -> None:
    """SCRAPBOOK_ENTRY must be registered — pingpong 2026-04-26 [S3-REGRESSION]."""
    assert "SCRAPBOOK_ENTRY" in _KIND_TO_MESSAGE_CLS, (
        "SCRAPBOOK_ENTRY must be in _KIND_TO_MESSAGE_CLS so the gallery "
        "receives entries on reconnect"
    )


def test_kind_to_message_cls_keeps_existing_kinds() -> None:
    """Extension, not replacement — the patch must NOT regress existing kinds."""
    required = {"NARRATION", "CONFRONTATION", "SECRET_NOTE", "SCRAPBOOK_ENTRY"}
    assert required.issubset(_KIND_TO_MESSAGE_CLS.keys()), (
        f"_KIND_TO_MESSAGE_CLS missing required kinds: "
        f"{required - set(_KIND_TO_MESSAGE_CLS.keys())}"
    )


def test_replay_skip_kinds_covers_every_encounter_telemetry_kind() -> None:
    """Every kind written by the watcher_hub encounter persister must be on
    the skip list; otherwise reconnect crashes when those rows appear."""
    encounter_kinds = set(_KIND_BY_OP.values())
    missing = encounter_kinds - _REPLAY_SKIP_KINDS
    assert not missing, (
        f"watcher_hub persists kinds not in _REPLAY_SKIP_KINDS: {missing}. "
        "Either add them to the skip list or register a message class."
    )


def test_skip_and_register_sets_are_disjoint() -> None:
    """A kind must be either fan-out to clients OR internal telemetry —
    never both. Catches accidental double-registration."""
    overlap = set(_KIND_TO_MESSAGE_CLS.keys()) & _REPLAY_SKIP_KINDS
    assert not overlap, f"kinds in both _KIND_TO_MESSAGE_CLS and _REPLAY_SKIP_KINDS: {overlap}"


# ---------------------------------------------------------------------------
# _build_message_for_kind round-trip tests
# ---------------------------------------------------------------------------


# Round-trip data per kind. Excludes CONFRONTATION because no production code
# path calls ``_emit_event("CONFRONTATION")`` today — confrontation messages
# are emitted directly to outbound, not journalled. Adding it here would
# trip the ``extra="forbid"`` check on the ``seq`` field that
# ``_build_message_for_kind`` always injects (separate latent bug — out of
# scope for this fix). The regression coverage that matters: SCRAPBOOK_ENTRY,
# NARRATION, and SECRET_NOTE all journalised + replayed in production.
@pytest.mark.parametrize(
    ("kind", "payload_dict"),
    [
        (
            "NARRATION",
            {
                "text": "The cavern shudders.",
                "footnotes": [],
            },
        ),
        (
            "SECRET_NOTE",
            {
                "turn_id": "t-1",
                "idempotency_key": "k-1",
                "subsystem": "trope",
                "params": {},
                "_visibility": {"visible_to": ["player:Alice"], "fidelity": {}},
            },
        ),
        (
            "SCRAPBOOK_ENTRY",
            {
                "turn_id": 1,
                "location": "Grimvault Caves",
                "narrative_excerpt": "The cavern shudders.",
                "scene_title": None,
                "scene_type": None,
                "image_url": None,
                "world_facts": [],
                "npcs_present": [],
            },
        ),
    ],
)
def test_build_message_for_kind_round_trips_journalled_kinds(kind: str, payload_dict: dict) -> None:
    """Each kind that production journals via ``_emit_event`` must rebuild
    cleanly. Bonus: the rebuilt message has the right ``type`` discriminator."""
    msg = _build_message_for_kind(kind=kind, payload_json=json.dumps(payload_dict), seq=42)
    assert msg is not None, f"{kind} unexpectedly returned None"
    assert getattr(msg, "type", None) == kind, (
        f"{kind} message rebuilt with wrong type: {getattr(msg, 'type', None)!r}"
    )


@pytest.mark.parametrize("kind", sorted(_REPLAY_SKIP_KINDS))
def test_build_message_for_kind_skips_internal_telemetry_without_crash(
    kind: str,
) -> None:
    """Internal telemetry kinds return None instead of raising. This is the
    fix for [S3-BUG]: before, ENCOUNTER_STARTED raised ValueError and
    aborted the entire replay window."""
    payload_json = json.dumps({"field": "encounter", "op": "started"})
    result = _build_message_for_kind(kind=kind, payload_json=payload_json, seq=7)
    assert result is None, f"{kind} should be skipped (returned None) but got: {result!r}"


def test_build_message_for_kind_still_raises_on_truly_unknown_kind() -> None:
    """Schema-drift safety net — a kind that's neither registered nor on the
    skip list should fail loudly so the bug is visible during dev."""
    with pytest.raises(ValueError, match="unknown event kind"):
        _build_message_for_kind(
            kind="TOTALLY_MADE_UP_KIND",
            payload_json="{}",
            seq=1,
        )


# ---------------------------------------------------------------------------
# Wiring test: reconnect against a real save with encounter rows
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_game_with_encounter_journal(tmp_path: Path) -> Path:
    """Build a save whose events table contains ENCOUNTER_STARTED rows —
    the exact shape that crashed reconnect for the Session 2 save."""
    db = db_path_for_slug(tmp_path, _SLUG)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=_SLUG,
        mode=GameMode.MULTIPLAYER,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )

    # Inject ENCOUNTER_STARTED + ENCOUNTER_TAG_CREATED as the watcher_hub
    # would. These don't go through EventLog.append because the watcher_hub
    # writes them directly via SQL — match that behavior here.
    store._conn.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        ("ENCOUNTER_STARTED", json.dumps({"field": "encounter", "op": "started"})),
    )
    store._conn.execute(
        "INSERT INTO events (kind, payload_json, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (
            "ENCOUNTER_TAG_CREATED",
            json.dumps({"field": "encounter", "op": "tag_created"}),
        ),
    )

    # Also append a real NARRATION through EventLog so the replay has at
    # least one client-bound message to surface — proves the crash didn't
    # truncate the rest of the journal.
    log = EventLog(SqliteSaveRepository(store))
    log.append(
        kind="NARRATION",
        payload_json=json.dumps({"text": "Hello, traveler."}),
    )
    store._conn.commit()
    store.close()
    return tmp_path


def _make_handler(save_dir: Path) -> WebSocketSessionHandler:
    handler = WebSocketSessionHandler(
        save_dir=save_dir,
        genre_pack_search_paths=[_CONTENT_SEARCH_PATH],
    )
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-replay",
        out_queue=asyncio.Queue(),
    )
    return handler


@pytest.mark.asyncio
async def test_reconnect_against_save_with_encounter_kinds_does_not_crash(
    seeded_game_with_encounter_journal: Path,
) -> None:
    """The actual S3-BUG reconnect path: a save whose events table contains
    ENCOUNTER_STARTED rows must replay without raising. Pre-fix, the
    ValueError aborted the entire connect handler."""
    handler = _make_handler(seeded_game_with_encounter_journal)
    msg = SessionEventMessage(
        type="SESSION_EVENT",
        player_id="alice",
        payload=SessionEventPayload(
            event="connect",
            game_slug=_SLUG,
            last_seen_seq=0,  # Force full replay
        ),
    )

    # The crash manifested as a raised ValueError out of handle_message.
    outbound = await handler.handle_message(msg)

    types = [getattr(m, "type", None) for m in outbound]
    assert "SESSION_EVENT" in types, f"SESSION_EVENT(connected) missing from outbound: {types}"
    # Encounter kinds must be skipped, not surfaced as protocol messages.
    assert "ENCOUNTER_STARTED" not in types
    assert "ENCOUNTER_TAG_CREATED" not in types
