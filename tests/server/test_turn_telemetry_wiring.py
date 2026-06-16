"""Mandatory wiring test: a real production turn (through connect.py's
bind_event_store, not a hand-bound store) actually writes turn_telemetry
rows. Proves the sink is reached from the live turn path, not just
importable.

Harness (from test_event_log_wiring.py):
- _seed_with_character: seed a MULTIPLAYER game with alice's character
- WebSocketSessionHandler + attach_room_context + handle_message for connect
- handle_message for PLAYER_ACTION

C2 join-path exercise: after alice connects, a second player (bob) is
added to the room as a connected observer (not seated, so playing_player_count
stays 1 — barrier fires on alice's single submission). The fake narration
result carries a SubsystemDispatch in secret_routes, so the session handler
emits a SECRET_NOTE event. SECRET_NOTE is VISIBILITY_GATED, so
_publish_secret_routed fires from within the emitters.py `with conn:` C2
block (while conn.in_transaction is True), writing a turn_telemetry row
with a non-NULL event_seq.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.session import GameSnapshot
from sidequest.protocol import GameMessage
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag
from sidequest.protocol.enums import MessageType
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry

_GENRE = "test_genre"
_WORLD = "flickering_reach"
# A fixed slug is collision-safe across parallel test runs because each test
# gets its own pytest tmp_path; the save.db lives under that isolated dir,
# never a shared location (matches test_event_log_wiring.py's rationale).
_SLUG = "turn-telemetry-wiring-fixture"
_FIXTURE_PACKS = Path(__file__).resolve().parents[1] / "fixtures" / "packs"


def _seed_with_character(tmp_path: Path, slug: str) -> None:
    """Seed a MULTIPLAYER game row + a saved snapshot carrying one Character
    for alice, so the slug-connect branch goes straight to Playing (skipping
    chargen).

    MULTIPLAYER mode is required (not SOLO) because:
    - The C2 join-path test adds a second connected player (bob) to the room
      after alice's connect, so SECRET_NOTE fanout has a non-empty recipient
      list. SOLO rooms raise SoloSlotConflict if a second player tries to
      connect.
    - Bob is connected but NOT seated, so playing_player_count() == 1 and
      the turn barrier fires on alice's single submission — no deadlock.
    """
    db = db_path_for_slug(tmp_path, slug)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=slug,
        mode=GameMode.MULTIPLAYER,
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
    # player_seats shape: {player_id: character_name}. Binding player_id
    # "alice" → character "Thorn" makes the MP slug-connect branch
    # (connect.py:479) resolve has_character=True for alice directly
    # (rather than falling through to the display_name matching branch which
    # would not find "alice" in {"Thorn"}).
    snap.player_seats["alice"] = "Thorn"
    store.init_session(_GENRE, _WORLD)
    store.save(snap)
    store.close()


def _fake_narration_result_with_secret():
    """Build a NarrationTurnResult that carries one SubsystemDispatch in
    secret_routes.

    The SubsystemDispatch causes the session handler to emit a SECRET_NOTE
    event (a VISIBILITY_GATED kind). When that event is routed through
    emitters.emit_event, the C2 transaction is open and _project_frames
    runs for the connected bob. CoreInvariantStage hits the VISIBILITY_GATED
    branch and calls _publish_secret_routed, which calls publish_event
    with conn.in_transaction == True — writing a turn_telemetry row with
    a non-NULL event_seq.

    The ``visible_to=["alice"]`` value is deliberately a non-empty player-id
    list (NOT ``"all"``, NOT ``[]``) so CoreInvariantStage routes bob through
    the VISIBILITY_GATED decision with ``malformed=False`` — the documented
    happy-path mechanism.

    NOTE (silent-rot guard): the mechanism depends on SecretNotePayload's
    ``visibility_sidecar`` field serializing to the ``"_visibility"`` wire
    key that CoreInvariantStage reads. If that wire alias is ever renamed,
    this turn would route via CoreInvariantStage's *malformed-fail-close*
    sub-branch instead. That sub-branch STILL calls _publish_secret_routed
    inside the C2 transaction, so both assertions would still pass — but the
    documented happy-path mechanism would no longer match reality. Any such
    rename MUST be manually cross-checked against this test, because the
    assertions alone will not catch the divergence.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult

    secret = SubsystemDispatch(
        subsystem="test_subsystem",
        idempotency_key="wiring-test-key-1",
        params={"note": "wiring proof"},
        visibility=VisibilityTag(visible_to=["alice"]),
    )
    return NarrationTurnResult(
        narration="The dungeon echoes with your footsteps.",
        location=None,
        quest_updates={},
        lore_established=[],
        npcs_present=[],
        is_degraded=False,
        agent_duration_ms=42,
        secret_routes=[secret],
    )


async def _drive_one_real_turn(tmp_path: Path) -> Path:
    """Shared harness: seed a MULTIPLAYER game, drive ONE real production turn
    through connect.py (alice connects, bob joins as unseated observer, alice
    submits a PLAYER_ACTION), and return the save.db Path.

    Extracted from the original test_a_real_turn_persists_turn_telemetry_rows
    body() so that both the wiring test and the cost-measurement test can
    reuse the SAME scenario without duplicating the setup.  The extraction is
    purely mechanical — no observable behaviour was changed; the existing
    test's assertions remain identical.

    INTENTIONAL execution model: callers MUST wrap this in ``asyncio.run()``.
    Do NOT call this from an ``async def`` test or add @pytest.mark.asyncio —
    that would create a nested-event-loop RuntimeError.  See the wiring test
    below for the authoritative comment.
    """
    _seed_with_character(tmp_path, _SLUG)
    registry = RoomRegistry()
    handler = WebSocketSessionHandler(
        save_dir=tmp_path,
        genre_pack_search_paths=[_FIXTURE_PACKS],
    )
    queue: asyncio.Queue[object] = asyncio.Queue()
    handler.attach_room_context(
        registry=registry,
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
        new=AsyncMock(return_value=_fake_narration_result_with_secret()),
    ):
        connect_out = await handler.handle_message(connect)

        # Verify slug-connect succeeded (has_character=True — alice's
        # pre-seeded character lets us skip chargen and go straight to
        # Playing).
        connected_msgs = [
            m
            for m in connect_out
            if getattr(m, "type", None) == MessageType.SESSION_EVENT
            and getattr(getattr(m, "payload", None), "event", None) == "connected"
        ]
        assert connected_msgs, (
            f"slug-connect did not emit SESSION_EVENT{{connected}}; got: {connect_out}"
        )
        assert getattr(connected_msgs[0].payload, "has_character", False) is True, (
            "slug-connect must see the pre-seeded character (has_character=True) "
            "so the handler is in Playing state for the PLAYER_ACTION below"
        )

        # Add bob as a connected-but-unseated observer to the room.
        # This ensures emit_event("SECRET_NOTE", ...) has a non-empty
        # recipient list → _project_frames fires for bob → VISIBILITY_GATED
        # branch → _publish_secret_routed → C2 join-path write.
        # Bob is NOT seated so playing_player_count() == 1 and the
        # turn barrier fires immediately on alice's submission (no deadlock).
        assert handler._room is not None, (
            "handler._room must be set after slug-connect — "
            "connect.py wires the room during the connected branch"
        )
        handler._room.connect("bob", socket_id="sock-bob")

        action = GameMessage.model_validate(
            {
                "type": "PLAYER_ACTION",
                "player_id": "alice",
                "payload": {"action": "I look around the dungeon."},
            }
        )
        await handler.handle_message(action)

    return db_path_for_slug(tmp_path, _SLUG)


def test_a_real_turn_persists_turn_telemetry_rows(tmp_path: Path) -> None:
    """Drive ONE real turn through the production connect path and assert
    that turn_telemetry rows are written, including at least one with a
    non-NULL event_seq (the C2 join-path row from a SECRET_NOTE fanout).

    connect.py's slug-connect branch calls bind_event_store(store) so the
    watcher_hub knows which SqliteStore to write to. Every publish_event
    call during the subsequent PLAYER_ACTION turn calls
    _persist_turn_telemetry, which writes a turn_telemetry row. This test
    verifies that end-to-end wiring actually persists rows — not just that
    the sink function is importable.
    """
    # INTENTIONAL execution model: this test is a sync ``def`` that wraps
    # the turn drive in ``asyncio.run(...)`` — NOT an ``async def``,
    # even though pyproject.toml sets asyncio_mode="auto" and the sibling
    # test_event_log_wiring.py uses ``async def``. Do NOT "fix" this to
    # ``async def`` and do NOT add @pytest.mark.asyncio: either change,
    # combined with the internal ``asyncio.run`` below, raises a
    # nested-event-loop RuntimeError. The sync wrapper is required so the
    # read-only sqlite assertions run after the loop has fully closed.
    save_db = asyncio.run(_drive_one_real_turn(tmp_path))
    conn = sqlite3.connect(f"file:{save_db}?mode=ro", uri=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM turn_telemetry").fetchone()[0]
        assert total > 0, "no turn_telemetry rows: sink is not wired into the live turn"
        attributed = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry WHERE event_seq IS NOT NULL"
        ).fetchone()[0]
        assert attributed > 0, "no event_seq-attributed rows: C2 join path not exercised"
    finally:
        conn.close()


def test_turn_telemetry_insert_count_is_not_pathological(tmp_path: Path) -> None:
    """Sink cost guard: one real turn must not explode telemetry inserts.
    The C2 model batches in-txn inserts into one commit; out-of-txn
    publishes each take a short txn. This pins a sane ceiling; if a future
    change blows it, that is the signal to coalesce per-turn (spec risk)."""
    # INTENTIONAL execution model: sync def wrapping asyncio.run — see the
    # comment in test_a_real_turn_persists_turn_telemetry_rows for rationale.
    # Do NOT convert to async def / @pytest.mark.asyncio.
    save_db = asyncio.run(_drive_one_real_turn(tmp_path))
    conn = sqlite3.connect(f"file:{save_db}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM turn_telemetry").fetchone()[0]
    finally:
        conn.close()
    # One turn's watcher publishes. Generous ceiling: regression tripwire,
    # not a tight bound. If a real turn legitimately exceeds it, raise the
    # ceiling AND open a Phase-follow-on coalesce note — do not silently bump.
    assert 0 < n <= 500, f"one turn wrote {n} telemetry rows — investigate/coalesce"


def test_a_real_turn_persists_mechanical_census_rows(tmp_path: Path) -> None:
    """A real production turn writes one component='mechanical' census
    row per seated PC + one session trope_census row, attributed to the
    turn's event frame. Proves the emitter is wired into emit_event's
    NARRATION path, not just importable."""
    save_db = asyncio.run(_drive_one_real_turn(tmp_path))  # existing harness
    conn = sqlite3.connect(f"file:{save_db}?mode=ro", uri=True)
    try:
        census = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry "
            "WHERE component='mechanical' AND event_type='census'"
        ).fetchone()[0]
        tropes = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry "
            "WHERE component='mechanical' AND event_type='trope_census'"
        ).fetchone()[0]
        attributed = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry "
            "WHERE component='mechanical' AND event_seq IS NOT NULL"
        ).fetchone()[0]
        assert census >= 1, (
            "no mechanical census rows: emit_mechanical_census is not "
            "wired into emit_event's NARRATION path"
        )
        assert tropes >= 1, "no session trope_census row from a real turn"
        assert attributed >= 1, (
            "mechanical rows not event_seq-attributed: census did not ride "
            "the C2 turn txn (R1 violated)"
        )
    finally:
        conn.close()


def test_mechanical_census_row_count_per_turn_is_bounded(tmp_path: Path) -> None:
    """One real turn must write exactly (seated_PCs census rows + 1 session
    trope_census), NOT once-per-segment. If this explodes, the NARRATION
    gate (R1 #3) regressed — fix the gate, do not bump the bound."""
    # INTENTIONAL execution model: sync def wrapping asyncio.run — see the
    # comment in test_a_real_turn_persists_turn_telemetry_rows for rationale.
    # Do NOT convert to async def / @pytest.mark.asyncio.
    save_db = asyncio.run(_drive_one_real_turn(tmp_path))
    conn = sqlite3.connect(f"file:{save_db}?mode=ro", uri=True)
    try:
        census = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry "
            "WHERE component='mechanical' AND event_type='census'"
        ).fetchone()[0]
        tropes = conn.execute(
            "SELECT COUNT(*) FROM turn_telemetry "
            "WHERE component='mechanical' AND event_type='trope_census'"
        ).fetchone()[0]
    finally:
        conn.close()
    # The Phase-1 harness seats a small MP party. Generous ceiling: this
    # is a regression tripwire, not a tight bound. Once-per-turn gate means
    # tropes == (number of NARRATION turns played by the harness == 1).
    assert 0 < census <= 12, f"{census} census rows — NARRATION gate regressed?"
    assert tropes == 1, f"{tropes} trope rows — expected exactly one per turn"
