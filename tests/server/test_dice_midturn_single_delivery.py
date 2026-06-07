"""Story 59-20 (RED→GREEN): route the DICE mid-turn CONFRONTATION through the
single ``emit_event(per_recipient_payload=...)`` supplier; delete the
``room_broadcast(union)`` + per-PC overlay race.

Follow-up to 59-16, which collapsed the post-narration START path and the
reconnect path onto the single filtered-delivery seam. The dice mid-turn /
flee path (``sidequest/server/dispatch/dice.py``) was DEFERRED: it still
fans the canonical full-union CONFRONTATION to every socket via
``room_broadcast`` and THEN queues a per-PC class-filtered overlay behind it
(the Story 49-7 mechanism). In production the overlay wins last-message-wins
so the common-case flee is correct — but the union still hits every socket,
and a reconnect racing the mid-turn emit can paint the 16-button union tab.

Locked design (Keith, 2026-05-26 — see
docs/superpowers/specs/2026-05-26-confrontation-single-filtered-delivery.md
§Implementation step 3): the dice mid-turn emit uses the SAME
``emit_event(per_recipient_payload=...)`` supplier as 59-16.

  * The canonical full-union payload is persisted to the EventLog ONLY.
  * Exactly ONE client fan-out for the mid-turn CONFRONTATION: per-recipient
    class-filtered, delivered to EVERY connected socket including the
    dispatcher/emitter. The union is NEVER sent to a client socket.
  * A seated, connected PC that cannot be resolved to a class FAILS LOUD
    (``confrontation.recipient_unresolved`` ERROR span) and gets no frame —
    never the union. An unseated/lobby socket gets nothing, silently.

Per sidequest-server/CLAUDE.md "No Source-Text Wiring Tests": every assertion
here is behavioral (delivered frames on real socket queues) or an OTEL span
assertion — never a grep of handler source. The harness mirrors
test_confrontation_single_delivery.py (59-16) but drives the production DICE
path: ``handle_message(DiceThrowMessage)`` → ``dispatch_dice_throw`` →
mid-turn CONFRONTATION emit → inline narrator.

RED expectation against the current union+overlay dice path:
  * Each seated socket receives the union (via ``room_broadcast``) AND the
    filtered overlay (via ``per_recipient_emit``) mid-turn = TWO frames, and
    the union frame leaks other-class beats. The single-supplier fix makes it
    exactly ONE filtered frame per socket and no union anywhere.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.persistence import GameMode
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.messages import (
    ConfrontationMessage,
    DiceThrowMessage,
)
from sidequest.server import emitters
from sidequest.server.session_handler import _State
from sidequest.server.session_room import RoomRegistry

# Reuse the proven 59-16 harness helpers — class-filtered combat pack, PC
# seating, live-encounter install, production emit wiring, real SessionRoom
# with per-socket queues, frame drains, and the cross-class beat set. Sharing
# them keeps the dice-path spec aligned with the start-path spec (one source
# of truth for what "class-filtered" means) instead of re-deriving it.
from tests.server.test_confrontation_single_delivery import (
    _OTHER_CLASS_BEATS,
    _beat_ids,
    _drain_confrontations,
    _inject_combat_pack,
    _install_live_combat_encounter,
    _seat,
    _union_payload,
    _wire_production_emit,
)

_SLUG = "s59-20-dice-midturn"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: events/projection persist to Postgres). Mirrors the isolation
    fixture in test_confrontation_single_delivery.py."""
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


def _bind_room(handler, sd, slug: str, player_ids: list[str]) -> dict[str, asyncio.Queue]:
    """Create a real SessionRoom, BIND the world (the dice ``handle_message`` path
    reads the bound snapshot, unlike the 59-16 ``_execute_narration_turn`` tests),
    and attach one socket+queue per player. Returns the per-player queue map."""
    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    store, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug="caverns_and_claudes",
        world_slug="",
    )
    registry = RoomRegistry()
    room = registry.get_or_create(slug=slug, mode=GameMode.MULTIPLAYER)
    room.bind_world(snapshot=sd.snapshot, store=store)
    queues: dict[str, asyncio.Queue] = {}
    for pid in player_ids:
        sid = f"sock-{pid}"
        q: asyncio.Queue = asyncio.Queue()
        queues[pid] = q
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, q)
    handler._room = room
    return queues


def _throw(player_id: str, *, beat_id: str = "attack", face: int = 18) -> DiceThrowMessage:
    """A DICE_THROW from a seated player on a non-opposed combat beat.

    ``attack`` carries no ``class_filter`` (every class may use it) so the
    roll resolves for any seated PC; ``face`` is high to land a Success tier,
    but the mid-turn emit is gated only on ``not opposed_pending`` — outcome
    tier does not change whether it fires.
    """
    return DiceThrowMessage(
        payload=DiceThrowPayload(
            request_id=f"{_SLUG}-{player_id}-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[face],
            beat_id=beat_id,
        ),
        player_id=player_id,
    )


def _combat_mock_with_confrontation() -> AsyncMock:
    return AsyncMock(
        return_value=NarrationTurnResult(
            narration="The party trades blows with the Chalk Moth.",
            confrontation="combat",
            npcs_present=[NpcMention(name="Chalk Moth", side="opponent", role="hostile")],
        ),
    )


async def _seated_dice_turn(
    session_handler_factory,
    *,
    slug: str,
    seats: list[tuple[str, str, str]],
    roller: str,
    capture_mid_turn: dict[str, list] | None = None,
):
    """Wire a real-room, multi-seat live-combat session and drive ONE DICE_THROW
    from ``roller`` through the production handler.

    Returns ``(sd, handler, queues)``. When ``capture_mid_turn`` is supplied,
    the narrator mock drains each socket queue at narrator-call time into it —
    isolating the mid-turn CONFRONTATION frames (which fire BEFORE the narrator)
    from the additive post-narration emit (which fires AFTER).
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = roller
    sd.player_name = roller.title()
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = slug
    _wire_production_emit(handler, slug)
    _inject_combat_pack(sd)
    _seat(sd, seats)
    _install_live_combat_encounter(sd, [name for _pid, name, _cls in seats])
    queues = _bind_room(handler, sd, slug, [pid for pid, _n, _c in seats])
    handler._state = _State.Playing
    handler._socket_id = f"sock-{roller}"

    # Give the roller a STRENGTH modifier so the attack beat resolves cleanly.
    roller_name = next(name for pid, name, _c in seats if pid == roller)
    roller_char = next(c for c in sd.snapshot.characters if c.core.name == roller_name)
    roller_char.stats["STRENGTH"] = 14

    if capture_mid_turn is not None:

        async def _capture(*_a, **_k):
            for pid, q in queues.items():
                while not q.empty():
                    item = q.get_nowait()
                    if isinstance(item, ConfrontationMessage):
                        capture_mid_turn.setdefault(pid, []).append(item)
            return _combat_mock_with_confrontation().return_value

        sd.orchestrator.run_narration_turn = AsyncMock(side_effect=_capture)
    else:
        sd.orchestrator.run_narration_turn = _combat_mock_with_confrontation()

    await handler.handle_message(_throw(roller))
    return sd, handler, queues


# ---------------------------------------------------------------------------
# AC1 — dice mid-turn delivers exactly ONE class-filtered frame per socket;
# the union+overlay race is deleted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dice_midturn_delivers_one_filtered_frame_per_socket_no_overlay(
    session_handler_factory,
) -> None:
    """The mid-turn CONFRONTATION (the one that fires BEFORE the narrator) must
    reach each connected seated socket EXACTLY ONCE, class-filtered.

    Pre-fix the dice path does ``room_broadcast(union)`` (fans the full union to
    every socket) AND a per-PC overlay (``per_recipient_emit`` of the filtered
    frame) — so each seated socket receives TWO mid-turn frames, the first
    carrying every class's beats. This asserts both 'exactly one' and 'filtered',
    which fails red on the union+overlay path.
    """
    mid_turn: dict[str, list] = {}
    sd, _handler, _queues = await _seated_dice_turn(
        session_handler_factory,
        slug=_SLUG + "-one",
        seats=[
            ("carl", "Carl", "Fighter"),
            ("donut", "Donut", "Cleric"),
            ("katia", "Katia", "Thief"),
        ],
        roller="katia",
        capture_mid_turn=mid_turn,
    )

    # 45-3 momentum-sync contract (preserved on the new delivery path): the
    # mid-turn frame is captured at narrator-call time, proving it landed BEFORE
    # the narrator ran, and it must carry the LIVE post-apply momentum — not a
    # stale snapshot. Tie each delivered frame to the live engine state.
    live_current = sd.snapshot.encounter.player_metric.current

    legal_by_pid = {
        "carl": ("shield_bash", _OTHER_CLASS_BEATS),  # Fighter
        "donut": ("turn_undead", {"backstab", "shield_bash", "cast_spell"}),  # Cleric
        "katia": ("backstab", {"shield_bash", "cast_spell", "turn_undead"}),  # Thief
    }
    for pid, (must_have, must_not_have) in legal_by_pid.items():
        frames = mid_turn.get(pid, [])
        assert len(frames) == 1, (
            f"{pid} must receive EXACTLY ONE mid-turn CONFRONTATION (single "
            f"filtered supplier); got {len(frames)}. Pre-fix the dice path "
            f"broadcasts the union AND queues the overlay = 2."
        )
        ids = _beat_ids(frames[0])
        assert must_have in ids, (
            f"{pid}'s class-legal beat {must_have!r} missing from the mid-turn "
            f"frame; got {sorted(ids)}"
        )
        leaked = ids & set(must_not_have)
        assert not leaked, (
            f"{pid}'s mid-turn frame leaked other-class beats {sorted(leaked)} "
            f"— the union reached the dice tab."
        )
        assert frames[0].payload.player_metric["current"] == live_current, (
            f"{pid}'s mid-turn frame must carry the LIVE post-apply momentum "
            f"({live_current}); got {frames[0].payload.player_metric.get('current')!r} "
            f"— a stale-zero frame means the broadcast fired before apply_beat (45-3)."
        )


@pytest.mark.asyncio
async def test_dice_midturn_no_union_reaches_any_socket(
    session_handler_factory,
) -> None:
    """No socket — dispatcher or peer — may ever receive a union frame from the
    dice path (mid-turn OR post-narration). A union frame is one carrying
    other-class beats; the canonical union belongs to the EventLog only.

    Pre-fix the mid-turn ``room_broadcast`` delivers a 10-beat union to every
    socket, so this fails red on the leaked cross-class beats.
    """
    _sd, _handler, queues = await _seated_dice_turn(
        session_handler_factory,
        slug=_SLUG + "-nounion",
        seats=[
            ("carl", "Carl", "Fighter"),
            ("donut", "Donut", "Cleric"),
            ("katia", "Katia", "Thief"),
        ],
        roller="katia",
    )

    legal_other = {
        "carl": _OTHER_CLASS_BEATS,  # a Fighter must never see backstab/cast_spell/etc.
        "donut": {"backstab", "shield_bash", "cast_spell"},
        "katia": {"shield_bash", "cast_spell", "turn_undead"},
    }
    for pid, forbidden in legal_other.items():
        for frame in _drain_confrontations(queues[pid]):
            leaked = _beat_ids(frame) & forbidden
            assert not leaked, (
                f"{pid} received a CONFRONTATION carrying other-class beats "
                f"{sorted(leaked)} on the dice path — the union must never reach "
                f"a client socket (EventLog only)."
            )


# ---------------------------------------------------------------------------
# AC5 regression — the post-narration emit is ADDITIVE, not replaced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dice_path_post_narration_emit_still_fans_out_to_seated_peer(
    session_handler_factory,
) -> None:
    """The mid-turn single-supplier emit must be ADDITIVE: the post-narration
    CONFRONTATION emit at ``_execute_narration_turn`` still fires and reaches a
    seated peer's socket AFTER the narrator returns.

    The mid-turn frames are captured (and drained) at narrator-call time; whatever
    remains on the peer's queue afterward is the post-narration fan-out. A
    regression that 'moved' the post-narration emit into the mid-turn slot (rather
    than keeping both) would leave the post-narration queue empty and strand the
    peer's tab when the narrator advances the metric a second time.

    (Migrated from the retired stub-room AC5 test in
    test_dice_throw_confrontation_emit.py — the peer is now properly SEATED, since
    the deleted union broadcast was the only thing reaching an UNSEATED peer.)
    """
    mid_turn: dict[str, list] = {}
    _sd, _handler, queues = await _seated_dice_turn(
        session_handler_factory,
        slug=_SLUG + "-ac5",
        seats=[("carl", "Carl", "Fighter"), ("donut", "Donut", "Cleric")],
        roller="carl",
        capture_mid_turn=mid_turn,
    )

    # Post-narration frames = whatever landed on the peer queue after the narrator
    # ran (the mid-turn frames were drained inside the capture side_effect).
    post_narration = _drain_confrontations(queues["donut"])
    assert len(post_narration) >= 1, (
        f"the post-narration CONFRONTATION emit must still fan out to the seated "
        f"peer 'donut' after the dice path (additive contract); got "
        f"{len(post_narration)} post-narration frame(s). A regression that replaced "
        f"the post-narration emit with the mid-turn one would land here."
    )
    # Cleric peer must still see class-filtered beats — never the union.
    for frame in post_narration:
        leaked = _beat_ids(frame) & {"backstab", "shield_bash", "cast_spell"}
        assert not leaked, (
            f"post-narration frame to Cleric 'donut' leaked other-class beats "
            f"{sorted(leaked)} — the union must never reach a socket."
        )


# ---------------------------------------------------------------------------
# AC1 negative — opposed_check defers; no mid-turn CONFRONTATION on any socket.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dice_midturn_opposed_check_emits_no_confrontation_to_any_socket(
    session_handler_factory,
) -> None:
    """When the active confrontation is ``opposed_check`` the beat application is
    deferred to ``narration_apply`` — there is no post-apply momentum to emit at
    the dice site, so NO socket may receive a mid-turn CONFRONTATION.

    Guards against a single-supplier refactor that emits unconditionally and
    pushes a stale-zero filtered frame on the opposed branch.
    """
    from sidequest.genre.models.rules import BeatDef, BeatKind, ConfrontationDef, MetricDef

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    slug = _SLUG + "-opposed"
    sd.player_id = "katia"
    sd.player_name = "Katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = slug
    _wire_production_emit(handler, slug)
    _inject_combat_pack(sd)
    # Override the injected cdef with an opposed_check variant.
    sd.genre_pack.rules.confrontations = [
        ConfrontationDef(
            type="combat",
            label="Dungeon Combat",
            category="combat",
            resolution_mode="opposed_check",
            opponent_default_stats={"STR": 12},
            player_metric=MetricDef(name="momentum", starting=0, threshold=10),
            opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
            beats=[
                BeatDef(id="attack", label="Attack", kind=BeatKind.strike, stat_check="STR"),
            ],
        )
    ]
    _seat(sd, [("carl", "Carl", "Fighter"), ("katia", "Katia", "Thief")])
    _install_live_combat_encounter(sd, ["Carl", "Katia"])
    queues = _bind_room(handler, sd, slug, ["carl", "katia"])
    handler._state = _State.Playing
    handler._socket_id = "sock-katia"
    katia_char = next(c for c in sd.snapshot.characters if c.core.name == "Katia")
    katia_char.stats["STRENGTH"] = 14

    # Capture the mid-turn frames ONLY (those that land before the narrator runs).
    # The opposed branch legitimately still emits a post-narration CONFRONTATION
    # once narration_apply resolves the opposed roll — asserting on the full turn
    # would wrongly flag that. Snapshot each queue at narrator-call time.
    mid_turn: dict[str, list] = {}

    async def _capture(*_a, **_k):
        for pid, q in queues.items():
            while not q.empty():
                item = q.get_nowait()
                if isinstance(item, ConfrontationMessage):
                    mid_turn.setdefault(pid, []).append(item)
        return _combat_mock_with_confrontation().return_value

    sd.orchestrator.run_narration_turn = AsyncMock(side_effect=_capture)

    await handler.handle_message(_throw("katia"))

    for pid in queues:
        confrontations = mid_turn.get(pid, [])
        assert confrontations == [], (
            f"opposed_check defers beat application; the dice site MUST NOT emit a "
            f"mid-turn CONFRONTATION on the opposed branch. {pid} received "
            f"{len(confrontations)} mid-turn CONFRONTATION frame(s)."
        )


# ---------------------------------------------------------------------------
# AC2 — fail LOUD on a seated-unresolvable PC; never the union. Unseated is silent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dice_midturn_seated_unresolvable_fails_loud_no_union(
    session_handler_factory,
    otel_capture,
) -> None:
    """A seated, connected PC whose class the pack no longer defines must NOT
    silently receive the union on the dice path. Contract: a
    ``confrontation.recipient_unresolved`` ERROR span fires and no union frame
    reaches that socket.

    Pre-fix the dice overlay does ``if recipient_pc is None: continue`` (silent
    skip) while ``room_broadcast`` still hands that socket the union — exactly
    the silent fallback this story removes.
    """
    from tests.server.conftest import span_attrs_by_name

    _sd, _handler, queues = await _seated_dice_turn(
        session_handler_factory,
        slug=_SLUG + "-failloud",
        # Carl is a normal seated Fighter (the roller). Ghost is seated but bound
        # to a class the pack does not define → resolve_recipient_pc -> (None, "Ghost").
        seats=[("carl", "Carl", "Fighter"), ("ghost", "Ghost", "Nonexistent")],
        roller="carl",
    )

    for frame in _drain_confrontations(queues["ghost"]):
        leaked = _beat_ids(frame) & _OTHER_CLASS_BEATS
        assert not leaked, (
            f"seated-unresolvable PC 'Ghost' received a union/unfiltered frame with "
            f"beats {sorted(leaked)} on the dice path — the banned silent fallback."
        )

    unresolved = span_attrs_by_name(otel_capture, "confrontation.recipient_unresolved")
    assert any(a.get("player_id") == "ghost" or a.get("actor") == "Ghost" for a in unresolved), (
        "a seated, connected PC that cannot be resolved to a class must emit a "
        "confrontation.recipient_unresolved ERROR span on the dice mid-turn path — "
        "never a silent skip + union broadcast."
    )


@pytest.mark.asyncio
async def test_dice_midturn_unseated_socket_gets_nothing_silently(
    session_handler_factory,
    otel_capture,
) -> None:
    """An unseated/lobby socket (connected but holding no PC) receives NO mid-turn
    CONFRONTATION and is NOT an error — the supplier returns None for it and
    emit_event skips it. No ``recipient_unresolved`` span for an unseated socket.

    Pre-fix ``room_broadcast`` (exclude=None) hands the union to the lobby socket
    too, so the 'gets nothing' assertion fails red.
    """
    from tests.server.conftest import span_attrs_by_name

    # The lobby socket must be present DURING the emit, so this test does its own
    # wiring (the _seated_dice_turn helper connects seated players only).
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    slug = _SLUG + "-lobby"
    sd.player_id = "carl"
    sd.player_name = "Carl"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = slug
    _wire_production_emit(handler, slug)
    _inject_combat_pack(sd)
    _seat(sd, [("carl", "Carl", "Fighter")])
    _install_live_combat_encounter(sd, ["Carl"])
    # Connect the seated Fighter AND an unseated lobby socket 'watcher'.
    queues = _bind_room(handler, sd, slug, ["carl", "watcher"])
    handler._state = _State.Playing
    handler._socket_id = "sock-carl"
    carl_char = next(c for c in sd.snapshot.characters if c.core.name == "Carl")
    carl_char.stats["STRENGTH"] = 14
    sd.orchestrator.run_narration_turn = _combat_mock_with_confrontation()

    await handler.handle_message(_throw("carl"))

    watcher_frames = _drain_confrontations(queues["watcher"])
    assert watcher_frames == [], (
        f"an unseated/lobby socket must receive NO mid-turn CONFRONTATION on the "
        f"dice path (supplier returns None → skip); got {len(watcher_frames)} frame(s)."
    )
    unresolved = span_attrs_by_name(otel_capture, "confrontation.recipient_unresolved")
    assert not any(a.get("player_id") == "watcher" for a in unresolved), (
        "an unseated/lobby socket is NOT an error — no recipient_unresolved span may fire for it."
    )


# ---------------------------------------------------------------------------
# AC4 (LOW) — emitter-absent / supplier-None fallback returns a CLEAR frame for
# the caller's return value, never the canonical union.
# ---------------------------------------------------------------------------


def test_emit_event_emitter_absent_supplier_returns_clear_not_union(
    session_handler_factory,
) -> None:
    """When the per-recipient supplier returns None for the emitter (emitter not
    resolvable to a frame), ``emit_event`` must return a CLEAR/empty frame for the
    caller's outbound — NOT the canonical union.

    Pre-fix (emitters.py emitter-absent branch) the fallback is
    ``fallback = payload_model`` — i.e. the union is returned as out_to_self. The
    union must never be the value handed back to a socket-bound caller.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    from sidequest.game.persistence import GameMode

    slug = _SLUG + "-fallback"
    sd.player_id = "katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = slug
    _wire_production_emit(handler, slug)
    queues = _bind_room(handler, sd, slug, ["katia", "carl"])

    union = _union_payload(["attack", "UNION_MARKER", "cast_spell", "turn_undead"])

    def supplier(pid: str):
        # None for the emitter (katia) → exercise the emitter-absent fallback;
        # a real filtered frame for the peer so the fan-out itself is healthy.
        if pid == "katia":
            return None
        return _union_payload([f"{pid}_only"])

    out = emitters.emit_event(
        handler,
        "CONFRONTATION",
        union,
        per_recipient_payload=supplier,
    )

    assert isinstance(out, ConfrontationMessage)
    assert "UNION_MARKER" not in _beat_ids(out), (
        f"emit_event returned the canonical union as the emitter's frame when the "
        f"supplier yielded None for them; got beats={sorted(_beat_ids(out))}. The "
        f"emitter-absent fallback must be a clear/empty frame, never the union."
    )
    # The peer still gets its filtered frame — the fallback change is emitter-only.
    peer_frames = _drain_confrontations(queues["carl"])
    assert len(peer_frames) == 1 and _beat_ids(peer_frames[0]) == {"carl_only"}, (
        f"peer fan-out must be unaffected; got {[sorted(_beat_ids(f)) for f in peer_frames]}"
    )


# asyncio marker for the test module
_ = asyncio
