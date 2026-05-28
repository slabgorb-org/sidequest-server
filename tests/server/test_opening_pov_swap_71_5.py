"""Story 71-5 — MP opening POV-swap WIRING test (end-to-end).

The unit behaviour of the helper lives in
``test_pov_swap_opening_helper_71_5.py``. THIS file is the project-doctrine
wiring proof: drive the REAL ``_chargen_confirmation`` opening-broadcast block
and confirm it routes the driver's copy through the swap (the driver's rendered
prose card reads "You…") while ``room.broadcast`` still sends peers the RAW
3rd-person originals byte-identically (single-anchor, seam Option a — peer path
untouched; the anchor-is-a-peer live case is out of scope, → 71-13).

The narrator is mocked (``_run_opening_turn_narration`` returns a canned
single-anchor opening: a generic, unanchored cold-open seed + a driver-anchored
prose card). Requires Postgres (the connect path persists per ADR-115).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

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
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def _canned_opening() -> list[object]:
    """A single-anchor MP opening: a generic (unanchored) cold-open seed plus a
    driver-anchored (Rux) narrator-prose card — the shape the narrator emits."""
    anchored = {"visible_to": "all", "anchor_pc": "Rux", "pov_strategy": "pc_anchored"}
    return [
        # Generic cold-open seed — NO sidecar (natural no-op for the swap).
        NarrationMessage(payload=NarrationPayload(text=NonBlankString(SEED_TEXT))),
        # Driver-anchored narrator prose — the card that must swap.
        NarrationMessage(
            payload=NarrationPayload(text=NonBlankString(PROSE_TEXT), visibility_sidecar=anchored)
        ),
    ]


async def _walk_to_confirmation(h: WebSocketSessionHandler) -> None:
    """Drive chargen up to (not through) the confirmation commit, building the
    driver's Character ('Rux') with pronouns."""
    sd = h._session_data  # type: ignore[attr-defined]
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
                    CharacterCreationMessage(
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
            CharacterCreationMessage(payload=payload, player_id="pid")
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")


def _make_mp(h: WebSocketSessionHandler) -> asyncio.Queue:
    """Rebind the handler onto a fresh MULTIPLAYER room with the driver (Rux)
    plus a seated peer (Donut); return the peer's outbound queue."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.server.session_room import SessionRoom

    sd = h._session_data  # type: ignore[attr-defined]
    sd.mode = GameMode.MULTIPLAYER
    driver_pid = sd.player_id or DRIVER_PID
    sd.player_id = driver_pid
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
            )
        )
    snap.player_seats[driver_pid] = "Rux"
    snap.player_seats[PEER_PID] = "Donut"

    room = SessionRoom(slug="pov-71-5", mode=GameMode.MULTIPLAYER)
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


async def _fire_opening(h: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    monkeypatch.setattr(chargen_mixin, "_should_fire_opening_narration", lambda _sd, _room: True)
    monkeypatch.setattr(h, "_run_opening_turn_narration", AsyncMock(return_value=_canned_opening()))
    out = await h.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id="pid",
        )
    )
    return list(out)


def _narration_texts(messages: list[object]) -> list[str]:
    texts: list[str] = []
    for m in messages:
        payload = getattr(m, "payload", None)
        text = getattr(payload, "text", None)
        if text is None:
            continue
        value = getattr(text, "root", None)
        texts.append(value if isinstance(value, str) else str(text))
    return texts


@pytest.mark.asyncio
async def test_opening_block_swaps_driver_card_and_broadcasts_raw_to_peers(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC4 + wiring: the inline opening block routes the driver's copy through
    the swap (driver's prose card → 'You…'; generic seed unchanged), while peers
    receive the RAW 3rd-person originals byte-identically (peer path untouched)."""
    await _connect(handler)
    await _walk_to_confirmation(handler)
    q_peer = _make_mp(handler)
    out = await _fire_opening(handler, monkeypatch)

    driver_texts = _narration_texts(out)
    driver_blob = " \n ".join(driver_texts)
    # Driver's prose card → 2nd person (proves the block invoked the swap helper).
    assert "You step into the galley" in driver_blob, (
        f"driver's prose card must be 2nd-person; got: {driver_blob!r}"
    )
    assert "Rux steps into the galley" not in driver_blob, (
        f"driver must not see their own name in 3rd person; got: {driver_blob!r}"
    )
    # Generic cold-open seed is unchanged for the driver (natural no-op).
    assert SEED_TEXT in driver_texts, f"generic seed must be unchanged; got: {driver_texts!r}"

    # Peers: raw 3rd-person, byte-identical to the canned originals.
    peer_msgs: list[object] = []
    while not q_peer.empty():
        peer_msgs.append(q_peer.get_nowait())
    peer_texts = _narration_texts(peer_msgs)
    assert PROSE_TEXT in peer_texts, (
        f"peer must receive the raw 3rd-person prose unchanged; got: {peer_texts!r}"
    )
    assert SEED_TEXT in peer_texts, f"peer must receive the raw seed; got: {peer_texts!r}"
    assert not any("You step into the galley" in t for t in peer_texts), (
        f"peer must NOT be POV-swapped (single-anchor); got: {peer_texts!r}"
    )
