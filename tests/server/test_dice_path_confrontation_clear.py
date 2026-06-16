"""Ping-pong 2026-06-07 — "MP confrontation DESYNC: seat 2 dropped out of
confrontation mode mid-fight" (perseus_cloud `2026-06-07-perseus_cloud-mp`).

Measured gap (server log `.20260607-090551`, turn 16): the opponent-reprisal
dice path resolved the encounter (`hp_depletion` → `opponent_victory`) but NO
``CONFRONTATION {active: false}`` frame was ever broadcast — zero
``confrontation.peer_projection_broadcast active=False`` lines. Root cause:
the post-narration emit seam captures ``prior_live`` AFTER
``run_narration_turn`` — by then the dice path (a separate DICE_THROW handler
invocation) has already flipped ``resolved=True``, so the live→resolved
transition is invisible (``prior_live=False, now_live=False`` → neither emit
branch fires). Every client is left to its own NARRATION_END heuristic
(App.tsx, fix playtest-2026-04-12), which can fork per-seat in MP — one seat
keeps the beat picker, another drops to plain free-text input.

Fix under test (two halves joined by a take-semantics stash):
- ``DiceThrowHandler`` stashes ``sd.pending_confrontation_clear =
  <encounter_type>`` when ``dispatch_dice_throw`` reports
  ``encounter_resolved`` (producer half).
- ``_execute_narration_turn``'s CONFRONTATION emit seam consumes the stash:
  no live encounter + stash present → broadcast the deterministic clear frame
  to every connected socket (consumer half). A new live encounter supersedes
  the clear (active frame wins; stash dropped).

Harness cribbed from tests/server/test_confrontation_mp_broadcast.py (real
``_execute_narration_turn`` + RoomRegistry room + per-socket queues + the
production emit_event path) and tests/server/test_dice_stale_encounter_resync.py
(MagicMock session surface for the DiceThrowHandler half).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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

_SLUG = "dice-path-clear-test"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: events/projection persist to Postgres). Mirrors
    test_confrontation_mp_broadcast.py."""
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
        encounter_beat_choices=["attack", "defend"],
    )


def _combat_cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef(id="attack", label="Attack", kind=BeatKind.strike, stat_check="STR"),
            BeatDef(id="defend", label="Defend", kind=BeatKind.brace, stat_check="CON"),
        ],
    )


def _inject_combat_pack(sd) -> None:
    sd.genre_pack.classes = [_class_def("Fighter"), _class_def("Thief")]
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


def _encounter(*, resolved: bool) -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="combat",
        category="combat",
        player_metric=EncounterMetric(name="momentum", current=2, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=1, starting=0, threshold=10),
        beat=3,
        structured_phase=EncounterPhase.Climax,
        actors=[
            EncounterActor(name="Paul", role="combatant", side="player"),
            EncounterActor(name="John", role="combatant", side="player"),
            EncounterActor(name="Unknown hostile", role="combatant", side="opponent"),
        ],
    )
    if resolved:
        enc.resolved = True
        enc.outcome = "opponent_victory"
        enc.structured_phase = EncounterPhase.Resolution
    return enc


def _plain_narration_mock() -> AsyncMock:
    return AsyncMock(
        return_value=NarrationTurnResult(
            narration="The airlock light goes amber. Someone's cycling.",
        )
    )


def _drain_confrontations(queue: asyncio.Queue) -> list[ConfrontationMessage]:
    frames: list[ConfrontationMessage] = []
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, ConfrontationMessage):
            frames.append(item)
    return frames


def _mp_setup(session_handler_factory):
    """Two-seat MP setup with real room + per-socket queues."""
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "paul"
    sd.player_name = "Paul"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG
    _wire_production_emit(handler, _SLUG)
    _inject_combat_pack(sd)
    _seat(sd, [("paul", "Paul", "Fighter"), ("john", "John", "Thief")])

    registry = RoomRegistry()
    room = registry.get_or_create(slug=_SLUG, mode=GameMode.MULTIPLAYER)
    queues: dict[str, asyncio.Queue[object]] = {}
    for pid in ("paul", "john"):
        sid = f"sock-{pid}"
        queues[pid] = asyncio.Queue()
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, queues[pid])
    handler._room = room
    handler._socket_id = "sock-paul"
    return sd, handler, queues


# ── consumer half: the emit seam folds the stash into the clear branch ──────


@pytest.mark.asyncio
async def test_dice_resolved_stash_emits_clear_to_every_socket(
    session_handler_factory,
    caplog,
) -> None:
    """Turn-16 shape: the dice path already resolved the encounter (so
    prior_live is False at the seam's capture) and stashed the clear. The
    narration re-entry must broadcast ``CONFRONTATION {active: false}`` to
    EVERY connected socket — both seats unmount deterministically instead of
    each relying on its own NARRATION_END heuristic (the MP fork)."""
    sd, handler, queues = _mp_setup(session_handler_factory)
    sd.snapshot.encounter = _encounter(resolved=True)
    sd.pending_confrontation_clear = "combat"
    sd.orchestrator.run_narration_turn = _plain_narration_mock()

    from sidequest.server.session_handler import _build_turn_context

    with caplog.at_level("INFO"):
        await handler._execute_narration_turn(
            sd,
            "[DICE_RESOLVED] the broadside lands",
            _build_turn_context(sd),
        )

    for pid in ("paul", "john"):
        frames = _drain_confrontations(queues[pid])
        assert len(frames) == 1, (
            f"{pid!r} must receive exactly one CONFRONTATION clear frame for a "
            f"dice-path resolution; got {len(frames)}. Pre-fix: ZERO frames — "
            f"the client was left to its NARRATION_END heuristic (the MP desync)."
        )
        assert frames[0].payload.active is False, (
            f"{pid!r}'s frame must be the clear (active=False); got {frames[0].payload!r}"
        )
        assert frames[0].payload.type == "combat"

    assert sd.pending_confrontation_clear is None, (
        "take semantics: the stash must be consumed by the emitting turn"
    )


@pytest.mark.asyncio
async def test_no_stash_no_transition_emits_no_frame(
    session_handler_factory,
) -> None:
    """Guard: an already-resolved encounter WITHOUT the dice stash (e.g. the
    clear already went out on a prior turn) must not re-broadcast a clear
    every subsequent turn."""
    sd, handler, queues = _mp_setup(session_handler_factory)
    sd.snapshot.encounter = _encounter(resolved=True)
    sd.orchestrator.run_narration_turn = _plain_narration_mock()

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(
        sd,
        "We catch our breath.",
        _build_turn_context(sd),
    )

    for pid in ("paul", "john"):
        assert _drain_confrontations(queues[pid]) == [], (
            f"{pid!r} must receive no CONFRONTATION frame when there is no "
            f"live encounter, no transition, and no pending dice clear"
        )


@pytest.mark.asyncio
async def test_stash_superseded_by_new_live_encounter(
    session_handler_factory,
) -> None:
    """A new LIVE encounter in the same turn supersedes the pending clear:
    the active frame is what every socket gets (a clear would unmount the
    tab the new fight just mounted). The stash is still consumed."""
    sd, handler, queues = _mp_setup(session_handler_factory)
    sd.snapshot.encounter = _encounter(resolved=False)
    sd.pending_confrontation_clear = "combat"
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="A second hostile bursts through the hatch.",
            confrontation="combat",
            npcs_present=[NpcMention(name="Unknown hostile", side="opponent", role="hostile")],
        )
    )

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(
        sd,
        "I draw on the boarder.",
        _build_turn_context(sd),
    )

    for pid in ("paul", "john"):
        frames = _drain_confrontations(queues[pid])
        assert len(frames) == 1
        assert frames[0].payload.active is True, (
            f"{pid!r} must get the ACTIVE frame — the live encounter wins over "
            f"the stale pending clear; got {frames[0].payload!r}"
        )
    assert sd.pending_confrontation_clear is None, (
        "the stale stash must be dropped, not left to clear a future turn"
    )


@pytest.mark.asyncio
async def test_clear_delivery_logs_per_recipient_evidence(
    session_handler_factory,
    caplog,
) -> None:
    """OTEL lie-detector (CLAUDE.md): the per-recipient CONFRONTATION fan-out
    must log WHO actually got a frame. The 2026-06-07 desync was undiagnosable
    because ``confrontation.peer_projection_broadcast`` logs room PRESENCE,
    not delivery — a silently-skipped seat (supplier → None) was invisible."""
    sd, handler, queues = _mp_setup(session_handler_factory)
    sd.snapshot.encounter = _encounter(resolved=True)
    sd.pending_confrontation_clear = "combat"
    sd.orchestrator.run_narration_turn = _plain_narration_mock()

    from sidequest.server.session_handler import _build_turn_context

    with caplog.at_level("INFO"):
        await handler._execute_narration_turn(
            sd,
            "[DICE_RESOLVED] the broadside lands",
            _build_turn_context(sd),
        )

    delivery_lines = [r.message for r in caplog.records if "confrontation.delivery" in r.message]
    assert delivery_lines, (
        "missing confrontation.delivery INFO line — per-recipient delivery "
        "evidence is the lie-detector for the next MP desync"
    )
    assert any("paul" in line and "john" in line for line in delivery_lines), (
        f"the delivery line must name the recipients that got a frame; got {delivery_lines!r}"
    )


# ── producer half: DiceThrowHandler stashes the clear on resolution ─────────


@pytest.mark.asyncio
async def test_dice_handler_stashes_clear_on_resolution(monkeypatch) -> None:
    """When ``dispatch_dice_throw`` reports ``encounter_resolved``, the handler
    must stash ``sd.pending_confrontation_clear = <encounter_type>`` so the
    inline narration re-entry's emit seam broadcasts the deterministic clear."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.handlers import dice_throw as dice_throw_mod
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import DiceThrowMessage
    from sidequest.server.dispatch import dice as dice_dispatch_mod
    from sidequest.server.dispatch.dice import DiceThrowOutcome, RollOutcome
    from sidequest.server.session_handler import _State

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.encounter = _encounter(resolved=False)

    session = MagicMock()
    session._state = _State.Playing
    session._room = None  # skip room broadcast / emit supplier wiring
    session._retrieve_lore_for_turn = AsyncMock(return_value=None)
    session._execute_narration_turn = AsyncMock(return_value=[])

    sd = MagicMock()
    sd.snapshot = snap
    sd.player_id = "p1"
    sd.player_name = "Paul"
    sd.genre_slug = "test_pack"
    sd.world_slug = "test_world"
    sd.pending_confrontation_clear = None
    session._session_data = sd

    monkeypatch.setattr(
        dice_dispatch_mod,
        "dispatch_dice_throw",
        lambda **_kw: DiceThrowOutcome(
            request=MagicMock(),
            result=MagicMock(),
            replay_action_text="[DICE_RESOLVED] strike lands",
            outcome=RollOutcome.Success,
            encounter_resolved=True,
        ),
    )
    monkeypatch.setattr(
        dice_throw_mod,
        "_build_turn_context",
        lambda *_a, **_kw: MagicMock(),
    )

    msg = DiceThrowMessage(
        type=MessageType.DICE_THROW,
        payload=DiceThrowPayload(
            request_id="req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 0.0, 0.0),
                angular=(0.0, 0.0, 0.0),
                position=(0.0, 0.0),
            ),
            face=[12],
            beat_id="attack",
        ),
        player_id="p1",
    )

    await dice_throw_mod.HANDLER.handle(session, msg)

    assert sd.pending_confrontation_clear == "combat", (
        "DiceThrowHandler must stash the resolved encounter's type so the "
        "narration re-entry emits the CONFRONTATION clear; got "
        f"{sd.pending_confrontation_clear!r}"
    )
