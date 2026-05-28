"""CONFRONTATION fan-out — single filtered delivery path (Story 59-16).

Originally the 2026-04-26 S2-BUG suite: confrontations were PRIVATE to the
acting player (peers froze) until the message was routed through
``_emit_event("CONFRONTATION", ...)`` so the EventLog + ProjectionFilter
fan-out reached every peer socket. Story 49-7 then added a per-PC
class-filtered overlay so each player saw only their class's beats.

Story 59-16 collapses the two racing delivery mechanisms (unfiltered union
broadcast + per-PC overlay) into ONE filtered path:

  * the canonical full-union payload goes to the EventLog ONLY,
  * exactly one per-recipient class-filtered frame is delivered to every
    connected socket including the emitter,
  * a seated, connected PC that cannot resolve a class fails LOUD (no
    silent union fallback).

These tests therefore assert the *single filtered* contract: every seated
peer (and the dispatcher) receives EXACTLY ONE CONFRONTATION frame and it
carries only that player's class-legal beats — never the full union. They
seat real PCs against the production caverns_and_claudes pack so
``resolve_recipient_pc`` finds each class; the historical fixture-pack
variants (no classes.yaml → unseated sockets) tested the union-broadcast
behavior that 59-16 deletes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.protocol.messages import ConfrontationMessage
from sidequest.server.session_room import RoomRegistry

_SLUG = "s2-confrontation-broadcast-test"

# Cross-class beat ids — a frame carrying any of these to a player whose
# class can't use them is the full-union leak Story 59-16 removes.
_OTHER_CLASS_BEATS = frozenset(
    {"backstab", "slip_behind", "cast_cantrip", "cast_spell", "turn_undead", "pray_for_aid"}
)


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: events/projection persist to Postgres)."""
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


def _seed_game_row(slug: str = _SLUG):
    """Register the session in Postgres and return the PgSaveRepository."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug="caverns_and_claudes",
        world_slug="",
    )
    return repo


def _use_real_content_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repoint the genre loader at the real sidequest-content packs so
    caverns_and_claudes carries real Fighter/Thief/Cleric classes with
    distinct ``encounter_beat_choices``."""
    content_packs = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    assert content_packs.is_dir(), (
        f"real sidequest-content packs directory not found at {content_packs} — "
        f"these tests assert per-class filtering that only manifests with the "
        f"production class definitions."
    )
    monkeypatch.setattr(
        "sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [content_packs],
    )


def _seat(snap, pairs: list[tuple[str, str, str]]) -> None:
    """Seat ``(player_id, character_name, char_class)`` PCs on the snapshot."""
    for pid, char_name, char_class in pairs:
        if not any(c.core.name == char_name for c in snap.characters):
            snap.characters.append(
                Character(
                    core=CreatureCore(
                        name=char_name,
                        description=f"{char_name} the adventurer",
                        personality="bold",
                        inventory=Inventory(),
                    ),
                    char_class=char_class,
                    race="Human",
                    backstory="A wandering adventurer",
                )
            )
        snap.player_seats[pid] = char_name


def _drain_confrontations(queue: asyncio.Queue) -> list[ConfrontationMessage]:
    frames: list[ConfrontationMessage] = []
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, ConfrontationMessage):
            frames.append(item)
    return frames


@pytest.mark.asyncio
async def test_confrontation_delivers_one_filtered_frame_to_every_seated_peer(
    session_handler_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4-player MP: every connected socket receives EXACTLY ONE
    class-filtered CONFRONTATION frame on encounter start — never the union.

    Drives the actual ``_execute_narration_turn`` handler against a real
    :class:`SessionRoom` with four seated PCs of distinct classes. The
    S2-BUG intent (peers are notified, not just the actor) is preserved:
    each peer still receives a CONFRONTATION. Story 59-16 sharpens it — the
    frame must be the player's class-filtered slice, and there must be
    exactly one (no union arriving first and clobbering the tab).

    Pre-59-16: each peer received TWO frames — the canonical union (from
    the emit_event fan-out) followed by the filtered overlay — so this
    fails on both 'exactly one' and 'no union leak'.
    """
    _use_real_content_packs(monkeypatch)
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "paul"
    sd.player_name = "Paul"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG

    store = _seed_game_row()
    handler._event_log = EventLog(store)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(store)

    # Seat four PCs of distinct classes so each recipient's class-legal
    # slice is distinguishable. Paul is the dispatcher/emitter.
    _seat(
        sd.snapshot,
        [
            ("paul", "Paul", "Fighter"),
            ("john", "John", "Thief"),
            ("george", "George", "Mage"),
            ("ringo", "Ringo", "Cleric"),
        ],
    )

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    socket_ids = {pid: f"sock-{pid}" for pid in ("paul", "john", "george", "ringo")}
    queues: dict[str, asyncio.Queue[object]] = {pid: asyncio.Queue() for pid in socket_ids}
    for pid, sid in socket_ids.items():
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, queues[pid])
    handler._room = room
    handler._socket_id = socket_ids["paul"]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Paul squares off against Veriti Onua.",
            confrontation="combat",
            npcs_present=[NpcMention(name="Veriti Onua", side="opponent", role="hostile")],
        ),
    )

    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "I open negotiations with Veriti Onua.",
        _build_turn_context(sd),
    )

    # The returned outbound list must not carry CONFRONTATION — delivery is
    # via the per-recipient socket fan-out, not the dispatcher's return value.
    outbound_kinds = [type(m).__name__ for m in msgs]
    assert ConfrontationMessage.__name__ not in outbound_kinds, (
        f"CONFRONTATION must be delivered via the per-recipient fan-out, not "
        f"the returned outbound list; got {outbound_kinds}"
    )

    legal = {
        "paul": "shield_bash",  # Fighter
        "john": "backstab",  # Thief
        "george": "cast_cantrip",  # Mage
        "ringo": "turn_undead",  # Cleric
    }
    for pid, must_have in legal.items():
        frames = _drain_confrontations(queues[pid])
        assert len(frames) == 1, (
            f"{pid!r} must receive exactly one CONFRONTATION frame (single "
            f"filtered delivery); got {len(frames)}. Pre-59-16 each peer got "
            f"union + overlay = 2."
        )
        ids = {b["id"] for b in frames[0].payload.beats}
        assert frames[0].payload.active is True
        assert frames[0].payload.type == "combat"
        assert must_have in ids, (
            f"{pid!r} ({must_have}'s class) is missing its class-legal beat; got {sorted(ids)}"
        )
        # No other class's signature beats may leak to this recipient.
        forbidden = _OTHER_CLASS_BEATS - {must_have}
        leaked = ids & forbidden
        assert not leaked, (
            f"{pid!r} leaked other-class beats {sorted(leaked)} — the full union "
            f"reached the tab (the 59-16 bug)."
        )


@pytest.mark.asyncio
async def test_filtered_confrontation_reaches_dispatcher_after_socket_cycle(
    session_handler_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect is the 59-16 bug trigger. The dispatcher's WS cycles
    mid-narration; the NEW socket must receive exactly one *class-filtered*
    CONFRONTATION (not the union), and the old detached socket nothing.

    Repro: 4P MP, Linus (Fighter) is the dispatch winner. During the
    narration await Linus's browser refreshes — old socket detaches, new
    socket attaches with a different socket_id. The frame must follow the
    current socket AND be Fighter-filtered.
    """
    _use_real_content_packs(monkeypatch)
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "linus"
    sd.player_name = "Linus"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-cycle"

    store = _seed_game_row(sd.game_slug)
    handler._event_log = EventLog(store)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(store)

    _seat(
        sd.snapshot,
        [
            ("linus", "Linus", "Fighter"),
            ("charlie", "Charlie", "Thief"),
            ("snoopy", "Snoopy", "Mage"),
            ("lucy", "Lucy", "Cleric"),
        ],
    )

    registry = RoomRegistry()
    room = registry.get_or_create(slug=sd.game_slug, mode=GameMode.MULTIPLAYER)

    pre_socket_id = "sock-linus-pre"
    pre_queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("linus", socket_id=pre_socket_id)
    room.attach_outbound(pre_socket_id, pre_queue)
    handler._socket_id = pre_socket_id  # Closure captures THIS

    for peer_pid, peer_sid in (
        ("charlie", "sock-charlie"),
        ("snoopy", "sock-snoopy"),
        ("lucy", "sock-lucy"),
    ):
        q: asyncio.Queue[object] = asyncio.Queue()
        room.connect(peer_pid, socket_id=peer_sid)
        room.attach_outbound(peer_sid, q)

    handler._room = room

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Linus opens negotiations with Inspector Karenina.",
            confrontation="combat",
            npcs_present=[NpcMention(name="Inspector Karenina", side="opponent", role="hostile")],
        ),
    )

    # Simulate the WS cycle: old socket detaches, a new socket attaches with
    # a fresh queue. The handler's closure-captured `_socket_id` still points
    # at the OLD socket — the production race.
    room.detach_outbound(pre_socket_id)
    post_socket_id = "sock-linus-post"
    post_queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("linus", socket_id=post_socket_id)
    room.attach_outbound(post_socket_id, post_queue)

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(
        sd,
        "I open negotiations.",
        _build_turn_context(sd),
    )

    post_conf = _drain_confrontations(post_queue)
    assert len(post_conf) == 1, (
        f"dispatcher's NEW (post-reconnect) socket must receive exactly one "
        f"CONFRONTATION frame; got {len(post_conf)}."
    )
    assert post_conf[0].payload.active is True
    ids = {b["id"] for b in post_conf[0].payload.beats}
    assert "shield_bash" in ids, (
        f"Linus (Fighter) must receive his class-legal beats after reconnect; got {sorted(ids)}"
    )
    leaked = ids & (_OTHER_CLASS_BEATS - {"shield_bash"})
    assert not leaked, (
        f"reconnected Fighter leaked other-class beats {sorted(leaked)} — the exact "
        f"flee/reconnect regression 59-16 fixes."
    )

    # The old (now-detached) socket must receive nothing.
    pre_conf = _drain_confrontations(pre_queue)
    assert pre_conf == [], (
        f"old (detached) socket must not receive CONFRONTATION; got {len(pre_conf)} frames."
    )


@pytest.mark.asyncio
async def test_seated_dispatcher_receives_class_filtered_not_unfiltered_canonical(
    session_handler_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pingpong 2026-05-12 17:48: trailing-PC regression of Story 49-7.

    Repro from the 3-PC Carl/Donut/Katia caverns_sunden playtest: per-PC
    verb projection worked at confrontation-open, then after a couple of
    resolved rounds the dispatcher's Confrontation tab regressed to the
    full 16-button class union — always the PC who narrated LAST (the
    merged-dispatch dispatcher).

    Under Story 59-16's single filtered path the dispatcher (the emitter)
    must receive exactly one Thief-filtered frame — the same contract as
    every other recipient.
    """
    _use_real_content_packs(monkeypatch)

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "katia"
    sd.player_name = "Katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-seated-trail"

    store = _seed_game_row(sd.game_slug)
    handler._event_log = EventLog(store)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(store)

    _seat(
        sd.snapshot,
        [
            ("carl", "Carl", "Fighter"),
            ("donut", "Donut", "Cleric"),
            ("katia", "Katia", "Thief"),
        ],
    )

    registry = RoomRegistry()
    room = registry.get_or_create(slug=sd.game_slug, mode=GameMode.MULTIPLAYER)
    queues: dict[str, asyncio.Queue[object]] = {}
    for pid, sid in (("carl", "sock-carl"), ("donut", "sock-donut"), ("katia", "sock-katia")):
        q: asyncio.Queue[object] = asyncio.Queue()
        queues[pid] = q
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, q)
    handler._room = room
    handler._socket_id = "sock-katia"

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Carl, Donut, and Katia square off against the Chalk Moth.",
            confrontation="combat",
            npcs_present=[NpcMention(name="Chalk Moth", side="opponent", role="hostile")],
        ),
    )

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(
        sd,
        "Open combat.",
        _build_turn_context(sd),
    )

    katia_frames = _drain_confrontations(queues["katia"])
    assert len(katia_frames) == 1, (
        f"Dispatcher (Katia, Thief) must receive EXACTLY ONE CONFRONTATION frame. "
        f"Pre-fix the dispatcher received the per-PC filtered frame followed by the "
        f"unfiltered canonical and the UI's last-message-wins render snapped back to "
        f"the full union. Got {len(katia_frames)} frames."
    )

    beat_ids = {b["id"] for b in katia_frames[0].payload.beats}
    assert "backstab" in beat_ids, (
        f"Katia (Thief) must see the Thief-specific 'backstab' beat; got {sorted(beat_ids)}"
    )
    forbidden = {
        "shield_bash",
        "cast_spell",
        "turn_undead",
        "pray_for_aid",
        "cleave",
        "parry",
        "taunt",
    }
    leaked = beat_ids & forbidden
    assert not leaked, (
        f"Thief-only Katia's CONFRONTATION leaked non-Thief beats: {sorted(leaked)} "
        f"(the 2026-05-12 union regression)."
    )
