"""Story 71-5 — MP opening narration POV-swap for the driving player.

The bug: in MP, the opening narration is a SINGLE shared blob anchored to ONE
PC (the driver/lead). It is broadcast to peers via ``room.broadcast`` (raw) and
returned to the driver via the handler's local ``out`` — but the driver's local
copy is NOT POV-swapped, so the driver (the anchor) reads third-person
("Carl steps...") instead of second-person ("You step...").

Architect ruling (model CONFIRMED): single-anchor; driver-only bug; seam
Option (a) — swap the driver's local copy at the opening-broadcast point in
``_chargen_confirmation`` (chargen_mixin ~1453); peer broadcast stays raw.

These tests drive the REAL ``_chargen_confirmation`` opening-broadcast seam with
the narrator mocked (``_run_opening_turn_narration`` returns a canned, anchored
opening). We assert the driver's returned card is swapped, peers' bytes are
unchanged, and the ``opening.narration_pov_swapped`` watcher event fires only
when a swap actually happened.
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


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: connect/events persist to Postgres)."""
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


def _canned_opening(anchor_pc: str, *, seed_text: str, prose_text: str) -> list[object]:
    """Two NARRATION cards (cold-open seed + narrator prose) carrying a
    pc-anchored visibility sidecar — the shape the narrator emits for an MP
    opening (ADR-036 / 49-8)."""
    sidecar = {"visible_to": "all", "anchor_pc": anchor_pc, "pov_strategy": "pc_anchored"}
    return [
        NarrationMessage(
            payload=NarrationPayload(text=NonBlankString(seed_text), visibility_sidecar=sidecar),
        ),
        NarrationMessage(
            payload=NarrationPayload(text=NonBlankString(prose_text), visibility_sidecar=sidecar),
        ),
    ]


async def _walk_to_confirmation(h: WebSocketSessionHandler) -> None:
    """Drive chargen up to (but not through) the confirmation commit, so the
    driver's Character ('Rux') is fully built with pronouns."""
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
    plus a seated peer (Donut), returning the peer's outbound queue. Mirrors
    the conftest MP factory's post-chargen room shape."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.server.session_room import SessionRoom

    sd = h._session_data  # type: ignore[attr-defined]
    sd.mode = GameMode.MULTIPLAYER
    driver_pid = sd.player_id or DRIVER_PID
    sd.player_id = driver_pid
    snap = sd.snapshot
    # Ensure a Donut character exists for the peer seat.
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


def _record_watcher(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    real = chargen_mixin._watcher_publish

    def _spy(event_type, fields, **kwargs):  # noqa: ANN001, ANN202
        events.append((event_type, fields))
        return real(event_type, fields, **kwargs)

    monkeypatch.setattr(chargen_mixin, "_watcher_publish", _spy)
    return events


async def _fire_opening(
    h: WebSocketSessionHandler,
    monkeypatch: pytest.MonkeyPatch,
    opening: list[object],
) -> list[object]:
    """Force the opening to fire with the canned messages and return the
    driver's local out list from the confirmation commit."""
    monkeypatch.setattr(chargen_mixin, "_should_fire_opening_narration", lambda _sd, _room: True)
    monkeypatch.setattr(
        h, "_run_opening_turn_narration", AsyncMock(return_value=opening)
    )
    out = await h.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id="pid",
        )
    )
    return list(out)


def _narration_texts(messages: list[object]) -> list[str]:
    """Pull the prose out of NARRATION messages. ``payload.text`` is a
    ``NonBlankString`` (RootModel[str]), so coerce via ``.root``/``str``."""
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
async def test_driver_own_opening_card_is_pov_swapped(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC1: the driver's own opening card (seed + prose), anchored to their PC,
    is swapped to 2nd person ('You step...'), not raw 3rd person."""
    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler)
    opening = _canned_opening(
        "Rux",
        seed_text="Rux steps into the galley as the hatch seals behind Rux.",
        prose_text="Rux checks the console; Rux's hands are steady.",
    )
    out = await _fire_opening(handler, monkeypatch, opening)

    texts = " \n ".join(_narration_texts(out))
    assert "You step into the galley" in texts, (
        f"driver's own opening card must be 2nd-person; got: {texts!r}"
    )
    assert "Rux steps into the galley" not in texts, (
        f"driver must not see their own name in 3rd person; got: {texts!r}"
    )


@pytest.mark.asyncio
async def test_opening_swap_skipped_when_anchor_is_not_driver(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC2: when the opening is anchored to a DIFFERENT PC, the driver's copy is
    NOT swapped (don't over-swap)."""
    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler)
    opening = _canned_opening(
        "Donut",  # anchored to the peer, not the driver (Rux)
        seed_text="Donut steps into the galley as the hatch seals behind Donut.",
        prose_text="Donut checks the console; Donut's hands are steady.",
    )
    out = await _fire_opening(handler, monkeypatch, opening)

    texts = " \n ".join(_narration_texts(out))
    assert "Donut steps into the galley" in texts, (
        f"non-anchor card must stay 3rd-person for the driver; got: {texts!r}"
    )
    assert "You step into the galley" not in texts, (
        f"driver must NOT be swapped for a peer-anchored card; got: {texts!r}"
    )


@pytest.mark.asyncio
async def test_opening_pov_swapped_watcher_event_emitted(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC3: a watcher event `opening.narration_pov_swapped` fires with the
    documented attrs when a swap actually happens (swap_count > 0)."""
    events = _record_watcher(monkeypatch)
    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler)
    opening = _canned_opening(
        "Rux",
        seed_text="Rux steps into the galley as the hatch seals behind Rux.",
        prose_text="Rux checks the console; Rux's hands are steady.",
    )
    await _fire_opening(handler, monkeypatch, opening)

    swapped = [f for (name, f) in events if name == "opening.narration_pov_swapped"]
    assert len(swapped) == 1, (
        f"expected one opening.narration_pov_swapped event; got {len(swapped)}"
    )
    fields = swapped[0]
    for key in (
        "driver_player_id",
        "anchor_pc",
        "anchor_pronouns",
        "swap_count",
        "original_text_length",
        "swapped_text_length",
    ):
        assert key in fields, f"watcher event missing attr {key!r}: {fields!r}"
    assert fields["anchor_pc"] == "Rux"
    assert fields["swap_count"] > 0


@pytest.mark.asyncio
async def test_no_pov_swapped_event_for_non_anchored_opening(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC3 (negative): an atmospheric / non-driver-anchored opening fires NO
    opening.narration_pov_swapped event (swap_count would be 0)."""
    events = _record_watcher(monkeypatch)
    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler)
    opening = _canned_opening(
        "Donut",  # not the driver → no swap for the driver
        seed_text="Donut steps into the galley.",
        prose_text="Donut checks the console.",
    )
    await _fire_opening(handler, monkeypatch, opening)

    swapped = [f for (name, f) in events if name == "opening.narration_pov_swapped"]
    assert swapped == [], f"no swap fired → no event expected; got {swapped!r}"


@pytest.mark.asyncio
async def test_peers_receive_raw_third_person_no_regression(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC4: MP single-anchor opening — the DRIVER sees 'You...' while PEERS
    receive the raw 3rd-person card unchanged (peer broadcast untouched)."""
    await _connect(handler)
    await _walk_to_confirmation(handler)
    q_peer = _make_mp(handler)
    seed = "Rux steps into the galley as the hatch seals behind Rux."
    prose = "Rux checks the console; Rux's hands are steady."
    opening = _canned_opening("Rux", seed_text=seed, prose_text=prose)
    out = await _fire_opening(handler, monkeypatch, opening)

    # Driver: swapped.
    driver_texts = " \n ".join(_narration_texts(out))
    assert "You step into the galley" in driver_texts

    # Peer: raw 3rd-person, byte-identical to the canned input.
    peer_msgs: list[object] = []
    while not q_peer.empty():
        peer_msgs.append(q_peer.get_nowait())
    peer_texts = _narration_texts(peer_msgs)
    assert seed in peer_texts, f"peer must receive the raw seed unchanged; got {peer_texts!r}"
    assert any("Rux steps into the galley" in t for t in peer_texts), (
        f"peer must see 3rd-person (anchor name), NOT 'You'; got {peer_texts!r}"
    )
    assert not any("You step into the galley" in t for t in peer_texts), (
        f"peer must NOT be POV-swapped (single-anchor); got {peer_texts!r}"
    )
