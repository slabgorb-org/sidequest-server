"""Story 71-13 wiring tests (RED) — MP opening must route through emit_event.

The 71-5 one-off helper path (room.broadcast + _pov_swap_opening_for_driver)
is replaced by ``emit_event(author_player_id=<driver>)`` for uniform
per-recipient POV + perception fanout + event-sourcing (story 71-13).

These three tests drive the REAL ``_chargen_confirmation`` opening block and
assert the NEW 71-13 behaviour that doesn't yet exist:
  1. ``room.broadcast`` is NOT called for the opening.
  2. The ``opening.broadcast_to_peers`` watcher event is NOT emitted.
  3. The ``opening.narration_pov_swapped`` watcher event is NOT emitted.

All three FAIL NOW (RED) because the current code still uses room.broadcast
and emits both legacy watcher events.  They will pass (GREEN) once Dev routes
the opening through ``emit_event`` and deletes the helper + broadcast block.

Requires Postgres (the connect path persists per ADR-115).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from sidequest.game.persistence import GameMode
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    NarrationMessage,
    NarrationPayload,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.websocket_handlers import chargen_mixin
from tests.server.test_opening_turn_bootstrap import _connect, claude_mock, handler  # noqa: F401

DRIVER_PID = "p_driver"
PEER_PID = "p_peer"
DRIVER_SOCK = "sock-driver"
PEER_SOCK = "sock-peer"

SEED_TEXT = "The galley hatch yawns open; cool recycled air drifts out."
PROSE_TEXT = "Rux steps into the galley as the hatch seals behind Rux."


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db (ADR-115 F1)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")  # pyright: ignore[reportCallIssue, reportArgumentType]
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _canned_opening() -> list[NarrationMessage]:
    """Single-anchor MP opening: unanchored cold-open seed + driver-anchored prose."""
    anchored = {"visible_to": "all", "anchor_pc": "Rux", "pov_strategy": "pc_anchored"}
    return [
        NarrationMessage(payload=NarrationPayload(text=NonBlankString(SEED_TEXT))),
        NarrationMessage(
            payload=NarrationPayload(text=NonBlankString(PROSE_TEXT), visibility_sidecar=anchored)  # pyright: ignore[reportCallIssue]
        ),
    ]


async def _walk_to_confirmation(h: WebSocketSessionHandler) -> None:
    """Drive chargen up to (not through) the confirmation commit."""
    sd = h._session_data  # type: ignore[attr-defined]
    assert sd is not None
    builder = sd.builder
    assert builder is not None
    while not builder.is_confirmation():
        scene = builder.current_scene()
        eff = scene.mechanical_effects
        if eff is not None and eff.assignment_required:
            pool = builder.arrangement_pool() or []
            sorted_pool = sorted(pool, reverse=True)
            stat_order = list(builder._ability_score_names)  # type: ignore[attr-defined]
            for stat, value in zip(stat_order, sorted_pool, strict=True):
                out = await h.handle_message(
                    CharacterCreationMessage(  # pyright: ignore[reportArgumentType]
                        payload=CharacterCreationPayload(
                            phase="arrange_assign", stat=stat, value=value
                        ),
                        player_id="pid",
                    )
                )
                if out and isinstance(out[0], ErrorMessage):
                    raise AssertionError(f"walk error: {out[0].payload.message}")
            payload = CharacterCreationPayload(phase="arrange_confirm")
        elif eff is not None and eff.identity_capture is not None:
            payload = CharacterCreationPayload(
                phase="story_confirm",
                pronouns="they/them",
                background="A wanderer's past.",
                description="Watchful eyes, quiet hands.",
            )
        elif scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Rux")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await h.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")  # pyright: ignore[reportArgumentType]
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")


def _make_mp(h: WebSocketSessionHandler) -> asyncio.Queue:
    """Rebind handler onto a fresh MULTIPLAYER room with driver (Rux) + peer (Donut)."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.server.session_room import SessionRoom

    sd = h._session_data  # type: ignore[attr-defined]
    assert sd is not None
    sd.mode = GameMode.MULTIPLAYER
    driver_pid = sd.player_id or DRIVER_PID
    sd.player_id = driver_pid
    sd.player_name = "Rux"
    snap = sd.snapshot
    if not any(c.core.name == "Donut" for c in snap.characters):
        snap.characters.append(
            Character(
                core=CreatureCore(
                    name="Donut", description="A peer", personality="bold", inventory=Inventory()
                ),
                char_class="Fighter",
                race="Human",
                backstory="A wandering adventurer",
                pronouns="she/her",
            )
        )
    snap.player_seats[driver_pid] = "Rux"
    snap.player_seats[PEER_PID] = "Donut"

    room = SessionRoom(slug="pov-71-13", mode=GameMode.MULTIPLAYER)
    room.bind_world(snapshot=snap, store=sd.repository)
    room.connect(driver_pid, socket_id=DRIVER_SOCK)
    room.seat(driver_pid, character_slot="Rux")
    room.transition_to_playing(driver_pid)
    room.connect(PEER_PID, socket_id=PEER_SOCK)
    room.seat(PEER_PID, character_slot="Donut")
    room.transition_to_playing(PEER_PID)

    q_driver: asyncio.Queue = asyncio.Queue()
    q_peer: asyncio.Queue = asyncio.Queue()
    room.attach_outbound(DRIVER_SOCK, q_driver)
    room.attach_outbound(PEER_SOCK, q_peer)
    h._room = room
    sd._room = room
    h._socket_id = DRIVER_SOCK
    return q_peer


async def _fire_opening(
    h: WebSocketSessionHandler,
    monkeypatch: pytest.MonkeyPatch,
    *,
    opening_factory=None,
) -> list[object]:
    """Drive the confirmation commit with a canned opening; return local out.

    Post sq-playtest 2026-05-28 #G1, ``_run_opening_turn_narration`` emits its
    own frames via ``emit_event`` and the chargen caller only extends the
    returned list (no re-emit). This fake mirrors that contract — it routes the
    canned opening frames through the REAL ``_emit_event`` so the no-room-broadcast
    / event-sourcing outcomes under test still fire.
    """
    factory = opening_factory or _canned_opening
    monkeypatch.setattr(chargen_mixin, "_should_fire_opening_narration", lambda _sd, _room: True)

    async def _emitting_opening(sd: object, _player_id: str, _span: object) -> list[object]:
        room = h._room
        connected = (
            room.connected_player_ids()
            if room is not None and callable(getattr(room, "connected_player_ids", None))
            else []
        )
        author = sd.player_id if len(connected) > 1 else None  # type: ignore[attr-defined]
        return [
            h._emit_event("NARRATION", m.payload, author_player_id=author) for m in factory()
        ]

    monkeypatch.setattr(h, "_run_opening_turn_narration", _emitting_opening)
    out = await h.handle_message(
        CharacterCreationMessage(  # pyright: ignore[reportArgumentType]
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id="pid",
        )
    )
    return list(out)


# ---------------------------------------------------------------------------
# RED tests (all three must FAIL under the current room.broadcast code path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opening_does_not_use_room_broadcast(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC1 (wiring) — the opening NARRATION must NOT use room.broadcast.

    After 71-13, the opening narration must route through ``emit_event``, NOT
    ``room.broadcast``.  Scope: this asserts only that no **NARRATION** message
    is broadcast — party_status (a non-NARRATION message) legitimately stays on
    ``room.broadcast`` to keep its ``broadcast.recipient_dropped`` telemetry
    (Architect spec-check, Deviation 2 / No Silent Fallbacks). The earlier
    ``assert not mock_broadcast.called`` over-reached and forbade ALL broadcast.
    """
    await _connect(handler)
    await _walk_to_confirmation(handler)
    q_peer = _make_mp(handler)

    assert handler._room is not None
    with patch.object(handler._room, "broadcast", wraps=handler._room.broadcast) as mock_broadcast:
        await _fire_opening(handler, monkeypatch)

    narration_broadcast_calls = [
        c
        for c in mock_broadcast.call_args_list
        if getattr(c.args[0], "type", None) == "NARRATION"
    ]
    assert narration_broadcast_calls == [], (
        "Opening NARRATION must NOT use room.broadcast after 71-13 — it must route "
        "through emit_event(author_player_id=<driver>). NARRATION was broadcast "
        f"{len(narration_broadcast_calls)} time(s): {narration_broadcast_calls!r}"
    )
    # Silence unused-variable warning for q_peer (set up for room completeness).
    _ = q_peer


@pytest.mark.asyncio
async def test_broadcast_to_peers_watcher_event_retired(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC7 (OTEL retire) — RED: opening.broadcast_to_peers IS emitted now; must vanish after fix.

    The ``opening.broadcast_to_peers`` watcher event was the lie-detector for
    the old broadcast path.  Once the broadcast block is deleted, this event
    must never fire — the standard ``emit.author_resolved`` /
    ``projection.filter.decide`` spans replace it.  The assertion fails today
    because the event IS emitted at chargen_mixin:1595-1604.
    """
    events: list[tuple[str, object]] = []

    def _spy(event_type: str, fields: object, **_: object) -> None:
        events.append((event_type, fields))

    monkeypatch.setattr(chargen_mixin, "_watcher_publish", _spy)

    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler)
    await _fire_opening(handler, monkeypatch)

    broadcast_events = [f for (_, f) in events if isinstance(f, dict) and f.get("field") == "opening.broadcast_to_peers" or  # noqa: E501
                        (isinstance(f, dict) and "opening.broadcast_to_peers" in str(f))]
    # The watcher_publish for this event uses a positional event_type arg of
    # "opening.broadcast_to_peers" (not a nested "field" key).
    raw_broadcast = [et for (et, _) in events if et == "opening.broadcast_to_peers"]
    assert raw_broadcast == [], (
        "opening.broadcast_to_peers watcher event must be RETIRED after 71-13 "
        "(broadcast block deleted, subsumed by emit.author_resolved + "
        f"projection.filter.decide).  Got: {raw_broadcast!r}"
    )
    _ = broadcast_events  # silence unused


@pytest.mark.asyncio
async def test_pov_swap_helper_watcher_event_retired(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC7 (OTEL retire) — RED: opening.narration_pov_swapped IS emitted now; must vanish.

    The ``opening.narration_pov_swapped`` watcher event was emitted by the
    now-deleted ``_pov_swap_opening_for_driver`` helper (when swap_count > 0).
    Once the helper is deleted, this event must never fire.  Today the canned
    opening has anchor_pc="Rux" and the driver IS Rux, so the swap fires and
    the event IS emitted.  The assertion fails today.
    """
    events: list[tuple[str, object]] = []

    def _spy(event_type: str, fields: object, **_: object) -> None:
        events.append((event_type, fields))

    monkeypatch.setattr(chargen_mixin, "_watcher_publish", _spy)

    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler)
    await _fire_opening(handler, monkeypatch)

    pov_events = [et for (et, _) in events if et == "opening.narration_pov_swapped"]
    assert pov_events == [], (
        "opening.narration_pov_swapped watcher event must be RETIRED after 71-13 "
        "(_pov_swap_opening_for_driver helper deleted, subsumed by emit_event "
        f"Track-A POV swap).  Got: {pov_events!r}"
    )
