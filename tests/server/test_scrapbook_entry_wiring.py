"""Wiring test for SCRAPBOOK_ENTRY emission (pingpong 2026-04-26 [S3-REGRESSION]).

The UI's ImageBusProvider has been wired to consume SCRAPBOOK_ENTRY for two
stories, but the server never emitted any. This test drives a real turn
through the orchestrator and asserts:

1. A row lands in the ``scrapbook_entries`` table.
2. A SCRAPBOOK_ENTRY event is appended to the journal.
3. A reconnecting client receives the prior SCRAPBOOK_ENTRY frame during
   replay (closes the loop on the gallery use-case).

Mocked at ``Orchestrator.run_narration_turn`` exactly like
``test_event_log_wiring.py`` — same fake-narration pattern, same in-memory
DB. No FastAPI, no daemon, no LLM calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
from sidequest.game.session import GameSnapshot
from sidequest.game.sqlite_repository import SqliteSaveRepository
from sidequest.protocol import GameMessage
from sidequest.protocol.enums import MessageType
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
_SLUG = "scrapbook-wiring-fixture"
_SLUG_RESUME = "scrapbook-resume-fixture"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _seed_with_character(tmp_path: Path, slug: str) -> None:
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.SOLO,
        genre_slug=_GENRE,
        world_slug=_WORLD,
    )
    core = CreatureCore(
        name="Thorn",
        description="A wandering fighter",
        personality="Grim",
        inventory=Inventory(),
    )
    char = Character(
        core=core,
        char_class="Fighter",
        race="Human",
        backstory="A wanderer.",
    )
    snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD)
    snap.characters = [char]
    store.init_session(_GENRE, _WORLD)
    store.save(snap)
    store.close()


def _fake_narration_result():
    """Build a narration result with the structured fields the scrapbook
    emitter reuses: location, npcs_present, footnotes."""
    from sidequest.agents.orchestrator import (
        NarrationTurnResult,
        NpcMention,
        VisualScene,
    )

    return NarrationTurnResult(
        narration=(
            "The dungeon echoes with your footsteps. A lantern flickers near the rough-hewn altar."
        ),
        location="Forgotten Crypt",
        visual_scene=VisualScene.from_dict(
            {
                "subject": "lantern-lit altar in a crypt",
                "tier": "scene_illustration",
                "mood": "ominous",
                "tags": ["crypt", "altar"],
            }
        ),
        npcs_present=[
            NpcMention(name="Caretaker Eldrin", role="silent witness", side="neutral"),
        ],
        footnotes=[
            {"summary": "The altar bears claw marks far too large for any human."},
        ],
        is_degraded=False,
        agent_duration_ms=42,
    )


@pytest.mark.asyncio
async def test_scrapbook_entry_persists_and_journals(tmp_path: Path) -> None:
    """Drive one PLAYER_ACTION → NARRATION turn → assert the scrapbook
    side-effects landed: row in ``scrapbook_entries`` AND a SCRAPBOOK_ENTRY
    row in ``events``."""
    _seed_with_character(tmp_path, _SLUG)
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-alice",
        out_queue=queue,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG,
                "last_seen_seq": 0,
            },
        }
    )

    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler.handle_message(connect)

        action = GameMessage.model_validate(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I look around the dungeon."},
            }
        )
        await handler.handle_message(action)

    db = db_path_for_slug(tmp_path, _SLUG)
    store = SqliteStore(db)
    store.initialize()
    try:
        # 1. scrapbook_entries row landed.
        rows = store._conn.execute(
            "SELECT turn_id, location, narrative_excerpt, scene_title, scene_type "
            "FROM scrapbook_entries"
        ).fetchall()
        assert rows, "expected at least one row in scrapbook_entries"
        turn_id, location, excerpt, scene_title, scene_type = rows[0]
        assert isinstance(turn_id, int)
        assert location, "scrapbook entry missing location"
        assert "dungeon" in excerpt.lower() or "altar" in excerpt.lower(), (
            f"excerpt did not echo narrator prose: {excerpt!r}"
        )
        assert scene_type == "scene_illustration"
        assert scene_title and "altar" in scene_title.lower()

        # 2. SCRAPBOOK_ENTRY row in events journal.
        events = EventLog(SqliteSaveRepository(store)).read_since(since_seq=0)
        kinds = [e.kind for e in events]
        assert "SCRAPBOOK_ENTRY" in kinds, f"expected SCRAPBOOK_ENTRY in event journal; got {kinds}"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reconnecting_client_replays_prior_scrapbook_entry(
    tmp_path: Path,
) -> None:
    """The full loop: emit during turn 1 (handler A), then connect a fresh
    handler B against the same save and assert B receives a SCRAPBOOK_ENTRY
    frame in its connect outbound. Catches "row exists but replay drops it"
    failures."""
    _seed_with_character(tmp_path, _SLUG_RESUME)

    # ------- Handler A: drive a turn that emits a SCRAPBOOK_ENTRY -------
    handler_a = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue_a: asyncio.Queue[object] = asyncio.Queue()
    handler_a.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-alice-a",
        out_queue=queue_a,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG_RESUME,
                "last_seen_seq": 0,
            },
        }
    )
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_fake_narration_result()),
    ):
        await handler_a.handle_message(connect)
        action = GameMessage.model_validate(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I look around the dungeon."},
            }
        )
        await handler_a.handle_message(action)

    # ------- Handler B: fresh reconnect, last_seen_seq=0 (full replay) -------
    handler_b = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue_b: asyncio.Queue[object] = asyncio.Queue()
    handler_b.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-alice-b",
        out_queue=queue_b,
    )
    outbound_b = await handler_b.handle_message(connect)

    types_b = [getattr(m, "type", None) for m in outbound_b]
    assert MessageType.SCRAPBOOK_ENTRY in types_b, (
        f"reconnecting client must replay SCRAPBOOK_ENTRY; got {types_b}"
    )
    # The replayed entry must carry the metadata from handler A's turn.
    scrapbook_msgs = [
        m for m in outbound_b if getattr(m, "type", None) == MessageType.SCRAPBOOK_ENTRY
    ]
    payload = scrapbook_msgs[0].payload
    assert getattr(payload, "location", "") != ""
    assert getattr(payload, "narrative_excerpt", "")


# ---------------------------------------------------------------------------
# Story 45-30: ``render_status`` discriminator on ScrapbookEntryPayload
#
# AC4 / AC5: the scrapbook payload carries a discriminator the UI uses to
# render distinct affordances for "rendered" vs "skipped by policy" vs
# "dispatched but failed". Pre-story the UI flips on ``hasImage`` only —
# it cannot tell a banter turn (no image was ever requested) from a
# daemon-down turn (image was requested and failed). The new field closes
# that gap.
# ---------------------------------------------------------------------------


_SLUG_RENDER_STATUS_BANTER = "scrapbook-render-status-banter"
_SLUG_RENDER_STATUS_RENDERED = "scrapbook-render-status-rendered"


def _banter_narration_result():
    """Banter turn — narrator emitted a visual_scene but ZERO structured
    signals (no beat, no scene change, no new NPC, no encounter
    resolution). The new render trigger policy must classify this as
    ``none_policy`` and the scrapbook payload must record that with
    ``render_status="skipped_policy"``."""
    from sidequest.agents.orchestrator import (
        NarrationTurnResult,
        VisualScene,
    )

    return NarrationTurnResult(
        narration=("Thorn stretches their shoulders against the cold and says little."),
        visual_scene=VisualScene.from_dict(
            {
                "subject": "fighter rolls shoulders by lantern light",
                "tier": "scene_illustration",
                "mood": "quiet",
                "tags": ["pause"],
            }
        ),
        # Deliberately empty: no location, no beats, no NPCs, no
        # confrontation. This is the load-bearing negative — pre-story the
        # render would have fired purely because visual_scene was set.
        is_degraded=False,
        agent_duration_ms=10,
    )


@pytest.mark.asyncio
async def test_scrapbook_render_status_skipped_policy_for_banter_turn(
    tmp_path: Path,
) -> None:
    """AC4: banter turn produces a SCRAPBOOK_ENTRY whose
    ``render_status`` is ``"skipped_policy"``. The entry still persists
    (the story still wants to remember the turn) but the UI must be
    able to render the eligible-but-skipped indicator distinctly from
    the daemon-failed indicator."""
    _seed_with_character(tmp_path, _SLUG_RENDER_STATUS_BANTER)
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-banter",
        out_queue=queue,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG_RENDER_STATUS_BANTER,
                "last_seen_seq": 0,
            },
        }
    )
    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=_banter_narration_result()),
    ):
        await handler.handle_message(connect)
        action = GameMessage.model_validate(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I take a moment to breathe."},
            }
        )
        await handler.handle_message(action)

    # Assert against the persisted scrapbook_entries row + the SCRAPBOOK_ENTRY
    # event in the journal. These are the same wire — emit_scrapbook_entry
    # both persists and routes through emit_event — and the journal is what
    # reconnecting clients replay. Mirroring the existing test's pattern
    # (test_scrapbook_entry_persists_and_journals) keeps assertion shape
    # consistent and avoids depending on action-handler outbound flow.
    db = db_path_for_slug(tmp_path, _SLUG_RENDER_STATUS_BANTER)
    store = SqliteStore(db)
    store.initialize()
    try:
        rows = store._conn.execute("SELECT render_status FROM scrapbook_entries").fetchall()
        assert rows, (
            "banter turn must still persist a scrapbook_entries row — the "
            "story remembers the turn even when no image was rendered"
        )
        actual = rows[0][0]
        assert actual == "skipped_policy", (
            f"banter turn render_status={actual!r} "
            "(expected 'skipped_policy') — the UI cannot distinguish this "
            "from a daemon failure without the discriminator"
        )

        # And the journaled event carries the same field on its payload —
        # this is what reconnects replay to the gallery.
        events = EventLog(SqliteSaveRepository(store)).read_since(since_seq=0)
        scrapbook_events = [e for e in events if e.kind == "SCRAPBOOK_ENTRY"]
        assert scrapbook_events, (
            "SCRAPBOOK_ENTRY missing from event journal — gallery won't see this turn on reconnect"
        )
        # The journal stores payloads as JSON strings — assert the field
        # is present and carries the correct value in whatever shape the
        # journal stores. ``payload_json`` is the canonical column.
        payload_repr = scrapbook_events[0].payload_json
        assert "render_status" in payload_repr, (
            f"render_status missing from journaled SCRAPBOOK_ENTRY payload: {payload_repr[:200]}"
        )
        assert "skipped_policy" in payload_repr
    finally:
        store.close()


@pytest.mark.asyncio
async def test_scrapbook_render_status_rendered_for_eligible_turn(
    tmp_path: Path,
) -> None:
    """AC4: an eligible turn (here: NPC intro) produces a SCRAPBOOK_ENTRY
    with ``render_status="rendered"`` even though the daemon path is
    not actually called in this test fixture — the discriminator
    reflects the POLICY decision (was a render dispatched), not whether
    the bytes have arrived from the daemon yet. The async IMAGE arrival
    is a separate downstream signal."""
    _seed_with_character(tmp_path, _SLUG_RENDER_STATUS_RENDERED)
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-rendered",
        out_queue=queue,
    )

    connect = GameMessage.model_validate(
        {
            "type": "SESSION_EVENT",
            "player_id": "alice",
            "payload": {
                "event": "connect",
                "game_slug": _SLUG_RENDER_STATUS_RENDERED,
                "last_seen_seq": 0,
            },
        }
    )
    # The eligible-turn fixture from the existing test is reused — it
    # carries an NpcMention with default is_new=False, so we override.
    from sidequest.agents.orchestrator import NpcMention

    eligible = _fake_narration_result()
    eligible.npcs_present = [NpcMention(name="Caretaker Eldrin", is_new=True, side="neutral")]

    with patch(
        "sidequest.agents.orchestrator.Orchestrator.run_narration_turn",
        new=AsyncMock(return_value=eligible),
    ):
        await handler.handle_message(connect)
        action = GameMessage.model_validate(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I greet the caretaker."},
            }
        )
        await handler.handle_message(action)

    # Read render_status from the persisted row — same reasoning as the
    # banter test above (mirrors test_scrapbook_entry_persists_and_journals
    # so we test the journal-side wire, not the action-handler outbound).
    db = db_path_for_slug(tmp_path, _SLUG_RENDER_STATUS_RENDERED)
    store = SqliteStore(db)
    store.initialize()
    try:
        rows = store._conn.execute("SELECT render_status FROM scrapbook_entries").fetchall()
        assert rows, "expected a row in scrapbook_entries"
        # Acceptable terminal values for an eligible turn whose async
        # image has not yet arrived: "rendered" (policy dispatched) or
        # "failed" (daemon refused at the gate). NOT "skipped_policy"
        # — the eligible NPC intro must NOT be classified as banter.
        actual = rows[0][0]
        assert actual in {"rendered", "failed"}, (
            f"eligible (NPC intro) turn render_status={actual!r} — must be "
            "'rendered' (policy dispatched) or 'failed' (daemon gate "
            "refused), never 'skipped_policy'"
        )
    finally:
        store.close()
