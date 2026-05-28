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

These tests assert the *single filtered* contract: every seated peer (and
the dispatcher) receives EXACTLY ONE CONFRONTATION frame carrying only that
player's class-legal beats — never the full union. Class defs + a
class-filtered combat ConfrontationDef are injected into the in-memory pack
and a live combat encounter is pre-installed (the
test_dice_throw_confrontation_emit.py pattern) so the delivery contract is
exercised independent of on-disk pack contents.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.event_log import EventLog
from sidequest.game.persistence import GameMode
from sidequest.game.projection.cache import ProjectionCache
from sidequest.game.projection.composed import ComposedFilter
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import BeatDef, BeatKind, ConfrontationDef, MetricDef
from sidequest.protocol.messages import ConfrontationMessage
from sidequest.server.session_room import RoomRegistry

_SLUG = "s2-confrontation-broadcast-test"

_OTHER_CLASS_BEATS = frozenset(
    {"backstab", "slip_behind", "cast_cantrip", "cast_spell", "turn_undead", "pray_for_aid"}
)

_CLASS_CHOICES = {
    "Fighter": ["attack", "defend", "flee", "shield_bash"],
    "Thief": ["attack", "defend", "flee", "backstab", "slip_behind"],
    "Mage": ["attack", "defend", "flee", "cast_cantrip", "cast_spell"],
    "Cleric": ["attack", "defend", "flee", "turn_undead", "pray_for_aid"],
}


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


def _wire_production_emit(handler, slug: str) -> None:
    repo = _seed_game_row(slug)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)


def _class_def(name: str) -> ClassDef:
    return ClassDef(
        id=name.lower(),
        display_name=name,
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="STR",
        minimum_score=9,
        kit_table=f"{name.lower()}_kit",
        flavor="-",
        encounter_beat_choices=_CLASS_CHOICES[name],
    )


def _beat(id_: str, *, class_filter: list[str] | None = None, stat: str = "STR") -> BeatDef:
    return BeatDef(
        id=id_,
        label=id_.replace("_", " ").title(),
        kind=BeatKind.strike,
        stat_check=stat,
        class_filter=class_filter,
    )


def _combat_cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            _beat("attack"),
            _beat("defend", stat="CON"),
            _beat("flee", stat="DEX"),
            _beat("shield_bash", class_filter=["Fighter"]),
            _beat("backstab", class_filter=["Thief"], stat="DEX"),
            _beat("slip_behind", class_filter=["Thief"], stat="DEX"),
            _beat("cast_cantrip", class_filter=["Mage"], stat="INT"),
            _beat("cast_spell", class_filter=["Mage"], stat="INT"),
            _beat("turn_undead", class_filter=["Cleric"], stat="WIS"),
            _beat("pray_for_aid", class_filter=["Cleric"], stat="CHA"),
        ],
    )


def _inject_combat_pack(sd) -> None:
    sd.genre_pack.classes = [_class_def(n) for n in _CLASS_CHOICES]
    sd.genre_pack.rules.confrontations = [_combat_cdef()]


def _seat(sd, pairs: list[tuple[str, str, str]]) -> None:
    snap = sd.snapshot
    for _pid, char_name, char_class in pairs:
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
    for pid, char_name, _cls in pairs:
        snap.player_seats[pid] = char_name


def _install_live_combat_encounter(sd, player_names: list[str]) -> None:
    actors = [EncounterActor(name=n, role="combatant", side="player") for n in player_names]
    actors.append(EncounterActor(name="Chalk Moth", role="combatant", side="opponent"))
    sd.snapshot.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=actors,
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _combat_mock() -> AsyncMock:
    return AsyncMock(
        return_value=NarrationTurnResult(
            narration="The party trades blows with the Chalk Moth.",
            confrontation="combat",
            npcs_present=[NpcMention(name="Chalk Moth", side="opponent", role="hostile")],
        ),
    )


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
) -> None:
    """4-player MP: every connected socket receives EXACTLY ONE
    class-filtered CONFRONTATION frame on encounter start — never the union.

    The S2-BUG intent (peers are notified, not just the actor) is preserved:
    each peer still receives a CONFRONTATION. Story 59-16 sharpens it — the
    frame must be the player's class-filtered slice, and there must be
    exactly one (no union arriving first and clobbering the tab).

    Pre-59-16: each peer received TWO frames — the canonical union (from the
    emit_event fan-out) followed by the filtered overlay.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "paul"
    sd.player_name = "Paul"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG
    _wire_production_emit(handler, _SLUG)
    _inject_combat_pack(sd)
    _seat(
        sd,
        [
            ("paul", "Paul", "Fighter"),
            ("john", "John", "Thief"),
            ("george", "George", "Mage"),
            ("ringo", "Ringo", "Cleric"),
        ],
    )
    _install_live_combat_encounter(sd, ["Paul", "John", "George", "Ringo"])

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    socket_ids = {pid: f"sock-{pid}" for pid in ("paul", "john", "george", "ringo")}
    queues: dict[str, asyncio.Queue[object]] = {pid: asyncio.Queue() for pid in socket_ids}
    for pid, sid in socket_ids.items():
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, queues[pid])
    handler._room = room
    handler._socket_id = socket_ids["paul"]

    sd.orchestrator.run_narration_turn = _combat_mock()

    from sidequest.server.session_handler import _build_turn_context

    msgs = await handler._execute_narration_turn(
        sd,
        "I open combat with the Chalk Moth.",
        _build_turn_context(sd),
    )

    outbound_kinds = [type(m).__name__ for m in msgs]
    assert ConfrontationMessage.__name__ not in outbound_kinds, (
        f"CONFRONTATION must be delivered via the per-recipient fan-out, not "
        f"the returned outbound list; got {outbound_kinds}"
    )

    # (signature beat that MUST appear, the recipient's full set of
    # class-specific beats) — forbidden = every other class's specials.
    legal = {
        "paul": ("shield_bash", {"shield_bash"}),  # Fighter
        "john": ("backstab", {"backstab", "slip_behind"}),  # Thief
        "george": ("cast_cantrip", {"cast_cantrip", "cast_spell"}),  # Mage
        "ringo": ("turn_undead", {"turn_undead", "pray_for_aid"}),  # Cleric
    }
    for pid, (must_have, own_specials) in legal.items():
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
        forbidden = _OTHER_CLASS_BEATS - own_specials
        leaked = ids & forbidden
        assert not leaked, (
            f"{pid!r} leaked other-class beats {sorted(leaked)} — the full union "
            f"reached the tab (the 59-16 bug)."
        )


@pytest.mark.asyncio
async def test_filtered_confrontation_reaches_dispatcher_after_socket_cycle(
    session_handler_factory,
) -> None:
    """Reconnect is the 59-16 bug trigger. The dispatcher's WS cycles
    mid-narration; the NEW socket must receive exactly one *class-filtered*
    CONFRONTATION (not the union), and the old detached socket nothing.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "linus"
    sd.player_name = "Linus"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-cycle"
    _wire_production_emit(handler, sd.game_slug)
    _inject_combat_pack(sd)
    _seat(
        sd,
        [
            ("linus", "Linus", "Fighter"),
            ("charlie", "Charlie", "Thief"),
            ("snoopy", "Snoopy", "Mage"),
            ("lucy", "Lucy", "Cleric"),
        ],
    )
    _install_live_combat_encounter(sd, ["Linus", "Charlie", "Snoopy", "Lucy"])

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

    sd.orchestrator.run_narration_turn = _combat_mock()

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
        "I press the attack.",
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

    pre_conf = _drain_confrontations(pre_queue)
    assert pre_conf == [], (
        f"old (detached) socket must not receive CONFRONTATION; got {len(pre_conf)} frames."
    )


@pytest.mark.asyncio
async def test_seated_dispatcher_receives_class_filtered_not_unfiltered_canonical(
    session_handler_factory,
) -> None:
    """Pingpong 2026-05-12 17:48: trailing-PC regression of Story 49-7 — the
    dispatcher's Confrontation tab regressed to the full union after a couple
    of resolved rounds. Under Story 59-16's single filtered path the
    dispatcher (the emitter) must receive exactly one Thief-filtered frame —
    the same contract as every other recipient.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "katia"
    sd.player_name = "Katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-seated-trail"
    _wire_production_emit(handler, sd.game_slug)
    _inject_combat_pack(sd)
    _seat(
        sd, [("carl", "Carl", "Fighter"), ("donut", "Donut", "Cleric"), ("katia", "Katia", "Thief")]
    )
    _install_live_combat_encounter(sd, ["Carl", "Donut", "Katia"])

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

    sd.orchestrator.run_narration_turn = _combat_mock()

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(
        sd,
        "Open combat.",
        _build_turn_context(sd),
    )

    katia_frames = _drain_confrontations(queues["katia"])
    assert len(katia_frames) == 1, (
        f"Dispatcher (Katia, Thief) must receive EXACTLY ONE CONFRONTATION frame; "
        f"got {len(katia_frames)}."
    )
    beat_ids = {b["id"] for b in katia_frames[0].payload.beats}
    assert "backstab" in beat_ids, (
        f"Katia (Thief) must see the Thief-specific 'backstab' beat; got {sorted(beat_ids)}"
    )
    leaked = beat_ids & (_OTHER_CLASS_BEATS - {"backstab", "slip_behind"})
    assert not leaked, (
        f"Thief-only Katia's CONFRONTATION leaked non-Thief beats: {sorted(leaked)} "
        f"(the 2026-05-12 union regression)."
    )
