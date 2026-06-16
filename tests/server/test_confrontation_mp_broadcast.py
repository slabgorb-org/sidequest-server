"""Pingpong 2026-04-26 S2-BUG: Confrontations are PRIVATE to the acting player.

Repro from the Beatles 4-player session: Paul opened a Diplomatic Negotiation
vs Veriti Onua. Paul saw the confrontation card, NPC, and action buttons.
John, George, and Ringo's tabs froze on the prior shared beat — no NPC card,
no narration, no buttons.

Root cause: ``_execute_narration_turn`` built the ``ConfrontationMessage``
directly via ``ConfrontationMessage(...)`` and only appended it to the
acting player's ``outbound`` list — the same list returned to the caller
and pushed to the actor's socket. Peer sockets were never notified.

Fix: route the message through ``self._emit_event("CONFRONTATION", payload)``
so the canonical EventLog + ProjectionFilter fan-out path delivers a
per-player frame to every connected peer (mirrors NARRATION at L3365 of
session_handler.py).

This is a wiring test — it drives the actual ``_execute_narration_turn``
handler against a real :class:`SessionRoom` with four connected sockets
and asserts every non-acting peer's outbound queue receives a
CONFRONTATION frame.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.sqlite_repository import SqliteSaveRepository
from sidequest.protocol.messages import ConfrontationMessage
from sidequest.server.session_room import RoomRegistry

_SLUG = "s2-confrontation-broadcast-test"


def _seed_game_row(tmp_path: Path) -> SqliteStore:
    db = db_path_for_slug(tmp_path, _SLUG)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.initialize()
    upsert_game(
        store,
        slug=_SLUG,
        mode=GameMode.MULTIPLAYER,
        genre_slug="caverns_and_claudes",
        world_slug="",
    )
    return store


@pytest.mark.asyncio
async def test_confrontation_broadcasts_to_all_four_peer_sockets(
    session_handler_factory,
    tmp_path: Path,
) -> None:
    """4-player MP regression: every connected socket receives the
    CONFRONTATION frame on encounter start, not just the actor's socket.

    Drives the actual ``_execute_narration_turn`` handler — no mocked
    broadcast, no projection-filter shortcut. Wires up a real
    :class:`SessionRoom` with Paul, John, George, and Ringo connected on
    distinct sockets, fires the encounter via the orchestrator mock, and
    inspects each socket's ``asyncio.Queue`` for a delivered frame.

    Pre-fix: only Paul's outbound list (the function's return value)
    received the frame; John/George/Ringo's queues stayed empty.
    Post-fix: ``_emit_event("CONFRONTATION", ...)`` fans out to all three
    peer queues via the projection-filter pipeline.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "paul"
    sd.player_name = "Paul"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG

    # Real EventLog + ProjectionFilter so _emit_event takes the production
    # branch (not the legacy fallback). ComposedFilter.with_no_genre_rules
    # is a pass-through filter — every recipient receives the same payload,
    # which is the correct shared-world behavior for confrontation frames.
    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    # 4-socket multiplayer room. Paul is the actor; John/George/Ringo are
    # peers. Each socket gets its own asyncio.Queue — the production
    # fan-out path looks up queue_for_socket(socket_for_player(pid)) per
    # recipient, so unique queues per socket are required.
    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    socket_ids = {
        "paul": "sock-paul",
        "john": "sock-john",
        "george": "sock-george",
        "ringo": "sock-ringo",
    }
    queues: dict[str, asyncio.Queue[object]] = {pid: asyncio.Queue() for pid in socket_ids}
    for pid, sid in socket_ids.items():
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, queues[pid])
    handler._room = room
    handler._socket_id = socket_ids["paul"]

    # Orchestrator mock: opens a confrontation on this turn (encounter is
    # currently None on the snapshot; result.confrontation="combat" makes
    # the dispatch branch take the now_live path).
    # Story 45-33: combat encounters require an opponent post-fallback;
    # supply Veriti Onua (already named in the prose) explicitly so the
    # lifecycle does not raise — the test's focus is the broadcast fan-out.
    from sidequest.agents.orchestrator import NpcMention

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Paul squares off against Veriti Onua.",
            confrontation="combat",
            npcs_present=[
                NpcMention(name="Veriti Onua", side="opponent", role="hostile"),
            ],
        ),
    )

    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "I open negotiations with Veriti Onua.",
        _build_turn_context(sd),
    )

    # Pingpong 2026-04-30 follow-on (sibling of f0b40c7): CONFRONTATION
    # delivery to the dispatcher was migrated off ``outbound.append`` —
    # the closure-captured outbound is the dispatcher's PRE-await socket
    # queue, which is dead when the WS cycles mid-narration. The
    # dispatcher now receives CONFRONTATION via a current-socket lookup
    # at delivery time (room.queue_for_socket(socket_for_player(...)))
    # so reconnected sockets pick up the frame. Mirrors the f0b40c7
    # pattern for NARRATION_END/CHAPTER_MARKER/PARTY_STATUS/AUDIO_CUE.
    outbound_kinds = [type(m).__name__ for m in msgs]
    assert ConfrontationMessage.__name__ not in outbound_kinds, (
        f"Actor's `outbound` list must NOT contain CONFRONTATION anymore — "
        f"it's delivered to the dispatcher's current socket queue at "
        f"delivery time so a reconnected dispatcher's NEW socket gets it. "
        f"Got outbound: {outbound_kinds}"
    )

    # Peer branch — every non-acting socket queue must have received
    # exactly one CONFRONTATION frame via ``_emit_event`` peer fan-out.
    # The new dispatcher-current-socket delivery does NOT broadcast to
    # peers (avoids double-delivery), so peer count stays at 1.
    for peer_pid in ("john", "george", "ringo"):
        peer_frames: list[object] = []
        while not queues[peer_pid].empty():
            peer_frames.append(queues[peer_pid].get_nowait())
        peer_conf = [f for f in peer_frames if isinstance(f, ConfrontationMessage)]
        assert len(peer_conf) == 1, (
            f"Peer {peer_pid!r} expected exactly one CONFRONTATION frame "
            f"on their queue; got {len(peer_conf)} (frames on queue: "
            f"{[type(f).__name__ for f in peer_frames]}). "
            "Pingpong S2-BUG: confrontations were PRIVATE to the actor "
            "before the _emit_event broadcast fix."
        )
        assert peer_conf[0].payload.active is True
        assert peer_conf[0].payload.type == "combat"

    # Pingpong 2026-04-30 follow-on: Paul's queue MUST contain exactly
    # one CONFRONTATION frame, delivered via the dispatcher-current-socket
    # lookup. Pre-fix this queue was empty (frame went to ``outbound``);
    # if the dispatcher's WS cycled mid-narration, ``outbound`` landed
    # on a dead queue and the encounter dial never activated.
    paul_frames: list[object] = []
    while not queues["paul"].empty():
        paul_frames.append(queues["paul"].get_nowait())
    paul_conf_via_queue = [f for f in paul_frames if isinstance(f, ConfrontationMessage)]
    assert len(paul_conf_via_queue) == 1, (
        f"Dispatcher (Paul) must receive exactly one CONFRONTATION frame "
        f"on their CURRENT socket queue (post pingpong 2026-04-30 fix); "
        f"got {len(paul_conf_via_queue)} (frames on queue: "
        f"{[type(f).__name__ for f in paul_frames]}). "
        "If 0, the dispatcher's reconnected socket would miss the encounter "
        "activation — the bug this fix addresses."
    )
    assert paul_conf_via_queue[0].payload.active is True
    assert paul_conf_via_queue[0].payload.type == "combat"


@pytest.mark.asyncio
async def test_confrontation_reaches_dispatcher_after_socket_cycle(
    session_handler_factory,
    tmp_path: Path,
) -> None:
    """Pingpong 2026-04-30 follow-on regression: dispatcher's WS cycles
    mid-narration and the NEW socket receives CONFRONTATION.

    Repro: 4P MP, Linus is the dispatch winner. During the 30-60s
    Claude narration await, Linus's browser refreshes — old socket
    detaches, new socket attaches with a different socket_id. Pre-fix,
    ``outbound.append(confrontation_msg)`` landed on the closure-
    captured pre-await queue (now dead, writer task cancelled); the
    new socket's queue was empty, encounter dial never activated.
    Post-fix: the dispatcher-current-socket lookup runs at delivery
    time so the new queue receives the frame.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "linus"
    sd.player_name = "Linus"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-cycle"

    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    registry = RoomRegistry()
    room = registry.get_or_create(slug=sd.game_slug, mode=GameMode.MULTIPLAYER)

    # Linus first connects on the PRE-await socket — this is the socket
    # the merged-dispatch closure captures. Charlie/Snoopy/Lucy connect
    # as peers.
    pre_socket_id = "sock-linus-pre"
    pre_queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("linus", socket_id=pre_socket_id)
    room.attach_outbound(pre_socket_id, pre_queue)
    handler._socket_id = pre_socket_id  # Closure captures THIS

    peer_queues: dict[str, asyncio.Queue[object]] = {}
    for peer_pid, peer_sid in (
        ("charlie", "sock-charlie"),
        ("snoopy", "sock-snoopy"),
        ("lucy", "sock-lucy"),
    ):
        q: asyncio.Queue[object] = asyncio.Queue()
        peer_queues[peer_pid] = q
        room.connect(peer_pid, socket_id=peer_sid)
        room.attach_outbound(peer_sid, q)

    handler._room = room

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Linus opens negotiations with Inspector Karenina.",
            confrontation="negotiation",
        ),
    )

    # Simulate the WS cycle: between handler attach and the dispatch
    # delivery, Linus's browser refreshes. The old socket detaches
    # (writer task cancelled), a new socket attaches with a different
    # socket_id and a fresh queue. The handler's closure-captured
    # `_socket_id` still points at the OLD socket — this is the exact
    # production race.
    room.detach_outbound(pre_socket_id)
    post_socket_id = "sock-linus-post"
    post_queue: asyncio.Queue[object] = asyncio.Queue()
    room.connect("linus", socket_id=post_socket_id)
    room.attach_outbound(post_socket_id, post_queue)

    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "I open negotiations.",
        _build_turn_context(sd),
    )

    # Load-bearing assertion: the NEW socket's queue receives
    # CONFRONTATION even though the closure captured the OLD socket_id.
    post_conf = []
    while not post_queue.empty():
        item = post_queue.get_nowait()
        if isinstance(item, ConfrontationMessage):
            post_conf.append(item)
    assert len(post_conf) == 1, (
        f"Dispatcher's NEW (post-reconnect) socket queue must receive "
        f"exactly one CONFRONTATION frame; got {len(post_conf)}. "
        "Pre-fix the frame went to the OLD socket's outbound list and "
        "was silently dropped — encounter dial never activated on the "
        "reconnected tab."
    )
    assert post_conf[0].payload.active is True

    # And the old (now-detached) queue must not receive anything — the
    # closure-captured socket_id no longer has an outbound queue
    # registered, so the helper's `room.queue_for_socket(...)` returns
    # None and skips delivery rather than landing on a dead queue.
    pre_conf = []
    while not pre_queue.empty():
        item = pre_queue.get_nowait()
        if isinstance(item, ConfrontationMessage):
            pre_conf.append(item)
    assert pre_conf == [], (
        f"Old (detached) socket queue must not receive CONFRONTATION; "
        f"got {len(pre_conf)} frames. If non-empty, the helper isn't "
        "looking up the CURRENT socket at delivery time — the very "
        "regression this test guards against."
    )

    # Outbound list returned to the caller is also free of CONFRONTATION
    # — guards against accidentally re-introducing the dead-queue path.
    outbound_kinds = [type(m).__name__ for m in msgs]
    assert ConfrontationMessage.__name__ not in outbound_kinds, (
        f"Returned outbound list must not contain CONFRONTATION post-fix; "
        f"got {outbound_kinds}. Re-introducing outbound.append would "
        "resurrect the dead-queue race."
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
    full 16-button class union. oq-2 isolated the pattern: the broken
    tab was always the PC who appeared LAST in the resolved-round
    narration order — i.e. the one whose ``sd.player_id`` became the
    dispatcher for the merged-dispatch turn.

    Root cause: ``_emit_event("CONFRONTATION", ...)`` returns the raw
    unfiltered canonical payload as ``confrontation_msg`` for the
    emitter. The per-PC overlay loop above the dispatcher-current-socket
    push queues a class-filtered CONFRONTATION to every connected socket
    (including the dispatcher's). The dispatcher-current-socket push
    then queues the unfiltered ``confrontation_msg`` to the dispatcher's
    queue AFTER the filtered overlay. UI renders last-message-wins, so
    the dispatcher's tab snapped back to the full 16-button union.

    Fix: skip the canonical push to the dispatcher when the per-PC
    overlay above already queued a filtered frame for them. The
    canonical push remains the dispatcher's sole delivery path in the
    legacy/unseated branch (no PC seat / no class resolution / clear
    payload / stub-room fixtures).

    This test exercises the seated-PC branch with a real
    caverns_and_claudes genre pack so ``resolve_recipient_pc`` actually
    finds the Thief class for Katia. Asserts:

      1. Katia's queue receives exactly one CONFRONTATION (not two —
         no canonical-clobber after the filtered overlay).
      2. The frame Katia receives is Thief-filtered: contains
         ``backstab`` (Thief-specific) and DOES NOT contain
         ``shield_bash`` (Fighter), ``cast_spell`` (Mage), or
         ``turn_undead`` (Cleric).
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    # Repoint genre-pack search at the real sidequest-content pack so the
    # loaded pack carries the real Fighter/Cleric/Thief classes with their
    # distinct encounter_beat_choices. The autouse
    # ``_fixture_pack_search_paths`` fixture points the loader at
    # tests/fixtures/packs which has no classes.yaml in caverns_and_claudes
    # — that fixture is fine for shape tests but cannot exercise per-class
    # beat filtering. This monkeypatch supersedes the autouse one for the
    # duration of this test only.
    content_packs = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    assert content_packs.is_dir(), (
        f"real sidequest-content packs directory not found at {content_packs} — "
        f"this test asserts behavior that only manifests with the production "
        f"class definitions (Fighter has shield_bash, Thief has backstab, etc.)"
    )
    monkeypatch.setattr(
        "sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [content_packs],
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "katia"
    sd.player_name = "Katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-seated-trail"

    # Real EventLog + ProjectionFilter so the production emit path runs.
    store = _seed_game_row(tmp_path)
    repo = SqliteSaveRepository(store)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    # Seat 3 PCs with distinct classes so the per-PC overlay produces
    # different verb lists per recipient. The factory's default snapshot
    # already has a "Rux" Fighter — append Donut (Cleric) and Katia
    # (Thief). All three classes exist in caverns_and_claudes/classes.yaml
    # with non-overlapping signature beats (shield_bash / turn_undead /
    # backstab) — required for the class-filter assertion to be sharp.
    snap = sd.snapshot
    for char_name, char_class in (("Carl", "Fighter"), ("Donut", "Cleric"), ("Katia", "Thief")):
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
                ),
            )
    snap.player_seats["carl"] = "Carl"
    snap.player_seats["donut"] = "Donut"
    snap.player_seats["katia"] = "Katia"

    # Connect all 3 to a real SessionRoom. Katia is the dispatcher
    # (sd.player_id), matching the playtest scenario where the last-
    # narrated PC in merged dispatch became the dispatcher and broke.
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

    # Drain Katia's queue and collect CONFRONTATION frames.
    katia_frames: list[ConfrontationMessage] = []
    while not queues["katia"].empty():
        item = queues["katia"].get_nowait()
        if isinstance(item, ConfrontationMessage):
            katia_frames.append(item)

    assert len(katia_frames) == 1, (
        f"Dispatcher (Katia, seated as Thief) must receive EXACTLY ONE "
        f"CONFRONTATION frame on their socket queue. Pre-fix the dispatcher "
        f"received two frames — the per-PC overlay's filtered frame followed "
        f"by the unfiltered canonical from the dispatcher-current-socket "
        f"push — and the UI's last-message-wins render snapped the panel "
        f"back to the full 16-button union (pingpong 2026-05-12 17:48). "
        f"Got {len(katia_frames)} frames."
    )

    beat_ids = {b["id"] for b in katia_frames[0].payload.beats}
    assert "backstab" in beat_ids, (
        f"Katia is seated as Thief; her Confrontation panel must include "
        f"the Thief-specific 'backstab' beat. Got beats: {sorted(beat_ids)}. "
        f"If 'backstab' is missing the per-PC overlay didn't reach Katia at "
        f"all — distinct failure mode from the canonical-clobber regression."
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
        f"Thief-only Katia's CONFRONTATION leaked non-Thief beats: {sorted(leaked)}. "
        f"This is the exact playtest 2026-05-12 17:30 regression — the "
        f"unfiltered canonical landed in Katia's queue after the filtered "
        f"overlay and the UI rendered the union. Pre-fix beats included "
        f"Fighter (shield_bash/cleave/parry), Mage (cast_spell), and Cleric "
        f"(turn_undead/pray_for_aid) beats Katia (Thief) cannot use."
    )
