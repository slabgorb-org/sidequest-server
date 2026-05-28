"""Story 59-16 (RED→GREEN): collapse CONFRONTATION delivery to ONE filtered path.

The bug: a solo Fighter is offered other classes' beats (Backstab, Cast
Spell, Turn Undead, ...) — correct on first load, wrong after a flee or
reconnect. Root cause (Story 49-7 debt): beats reach clients via TWO
racing mechanisms — an unfiltered full-union CONFRONTATION broadcast plus
a per-PC class-filtered overlay queued behind it, with the UI rendering
last-message-wins. The emitter additionally raw-bypasses the ADR-105
projection firewall (``emitters.emit_event`` Invariant 3) because the
CONFRONTATION emit passes no ``author_player_id``.

Locked design (Keith, 2026-05-26 — see
docs/superpowers/specs/2026-05-26-confrontation-single-filtered-delivery.md):
ONE delivery path.

  * The canonical full-union payload is persisted to the EventLog ONLY
    (replay/audit). It is NEVER sent to a client socket.
  * Exactly one client fan-out for CONFRONTATION: per-recipient
    class-filtered, delivered to EVERY connected socket including the
    emitter (kills the solo bypass where the player IS the emitter).
  * If a seated, connected PC cannot be resolved to a class, FAIL LOUD
    (ERROR span) and do NOT send the union as a fallback.

Per sidequest-server/CLAUDE.md "No Source-Text Wiring Tests": every
assertion here is behavioral (delivered frames on real socket queues) or
an OTEL span assertion (``confrontation.beat_filter`` /
``confrontation.recipient_unresolved``) — never a grep of handler source.

Test construction note: the handler tests inject class defs + a
class-filtered combat ConfrontationDef into the in-memory genre pack and
pre-install a live combat encounter (the pattern in
test_dice_throw_confrontation_emit.py). This keeps the per-PC filtering
exercise independent of on-disk pack contents and of the encounter
instantiation pipeline — the surface under test is delivery, not
instantiation.
"""

from __future__ import annotations

import asyncio
import json
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
from sidequest.protocol.messages import ConfrontationMessage, ConfrontationPayload
from sidequest.server import emitters
from sidequest.server.session_room import RoomRegistry

_SLUG = "s59-16-single-delivery"

# The cross-class beat ids that leaked in the 2026-05-12 playtest. A
# Fighter must never receive any of these; a frame carrying several of
# them at once is the tell-tale full union.
_OTHER_CLASS_BEATS = frozenset(
    {"backstab", "slip_behind", "cast_cantrip", "cast_spell", "turn_undead", "pray_for_aid"}
)


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: events/projection persist to Postgres). Mirrors the
    isolation fixture in test_confrontation_mp_broadcast.py."""
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


def _seed_game_row(slug: str):
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
    """Attach a real EventLog + pass-through ProjectionFilter + cache so the
    production ``emit_event`` branch runs (not the legacy no-EventLog
    fallback)."""
    repo = _seed_game_row(slug)
    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)


def _connect_room(handler, slug: str, player_ids: list[str]) -> dict[str, asyncio.Queue]:
    """Create a real SessionRoom with one socket+queue per player. Returns the
    per-player queue map so tests can inspect delivered frames."""
    registry = RoomRegistry()
    room = registry.get_or_create(slug=slug, mode=GameMode.MULTIPLAYER)
    queues: dict[str, asyncio.Queue] = {}
    for pid in player_ids:
        sid = f"sock-{pid}"
        q: asyncio.Queue = asyncio.Queue()
        queues[pid] = q
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, q)
    handler._room = room
    return queues


def _drain_confrontations(queue: asyncio.Queue) -> list[ConfrontationMessage]:
    frames: list[ConfrontationMessage] = []
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, ConfrontationMessage):
            frames.append(item)
    return frames


def _beat_ids(msg: ConfrontationMessage) -> set[str]:
    return {b["id"] for b in msg.payload.beats}


def _union_payload(beats: list[str]) -> ConfrontationPayload:
    return ConfrontationPayload(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        genre_slug="caverns_and_claudes",
        beats=[{"id": b, "label": b.title(), "kind": "strike"} for b in beats],
    )


# ---------------------------------------------------------------------------
# In-memory pack injection — class defs + a class-filtered combat cdef, plus a
# pre-installed live encounter. Mirrors test_dice_throw_confrontation_emit.py's
# direct-construction approach so per-PC filtering is exercised without an
# on-disk pack or the instantiation pipeline.
# ---------------------------------------------------------------------------

_CLASS_CHOICES = {
    "Fighter": ["attack", "defend", "flee", "shield_bash"],
    "Thief": ["attack", "defend", "flee", "backstab", "slip_behind"],
    "Mage": ["attack", "defend", "flee", "cast_cantrip", "cast_spell"],
    "Cleric": ["attack", "defend", "flee", "turn_undead", "pray_for_aid"],
}


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
    """Inject the four class defs + the class-filtered combat cdef into the
    in-memory genre pack so resolve_recipient_pc + build_confrontation_payload
    operate on known classes and beats."""
    sd.genre_pack.classes = [_class_def(n) for n in _CLASS_CHOICES]
    sd.genre_pack.rules.confrontations = [_combat_cdef()]


def _seat(sd, pairs: list[tuple[str, str, str]]) -> None:
    """Seat ``(player_id, character_name, char_class)`` PCs on the snapshot."""
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
    """Pre-install an active (unresolved) combat encounter so the start-path
    CONFRONTATION emit fires on the next turn (now_live=True)."""
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


# ---------------------------------------------------------------------------
# AC1 + AC2 — the new emit_event primitive: a per_recipient_payload supplier.
# ---------------------------------------------------------------------------


def test_emit_event_per_recipient_payload_delivers_filtered_to_emitter_and_peers(
    session_handler_factory,
) -> None:
    """AC1. With ``per_recipient_payload`` supplied, EVERY connected socket —
    including the emitter's own — receives ``per_recipient_payload(pid)``,
    not the canonical union. This is the kill-shot for the solo bypass: the
    emitter is no longer raw-bypassed when a supplier is given.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG
    _wire_production_emit(handler, _SLUG)
    queues = _connect_room(handler, _SLUG, ["katia", "carl"])

    union = _union_payload(["attack", "backstab", "cast_spell", "turn_undead"])

    def supplier(pid: str) -> ConfrontationPayload:
        return _union_payload([f"{pid}_only"])

    out = emitters.emit_event(
        handler,
        "CONFRONTATION",
        union,
        per_recipient_payload=supplier,
    )

    assert isinstance(out, ConfrontationMessage)
    assert _beat_ids(out) == {"katia_only"}, (
        f"emitter must receive per_recipient_payload('katia'), not the union; "
        f"got {sorted(_beat_ids(out))}"
    )

    peer_frames = _drain_confrontations(queues["carl"])
    assert len(peer_frames) == 1, f"peer expected one frame; got {len(peer_frames)}"
    assert _beat_ids(peer_frames[0]) == {"carl_only"}


def test_emit_event_persists_union_to_eventlog_but_never_to_a_socket(
    session_handler_factory,
) -> None:
    """AC2. The canonical full-union payload is persisted to the EventLog for
    replay/audit parity, but NO client socket (emitter or peer) ever receives
    the union frame.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG
    _wire_production_emit(handler, _SLUG)
    queues = _connect_room(handler, _SLUG, ["katia", "carl"])

    union = _union_payload(["attack", "UNION_MARKER", "cast_spell"])

    def supplier(pid: str) -> ConfrontationPayload:
        return _union_payload([f"{pid}_only"])

    out = emitters.emit_event(
        handler,
        "CONFRONTATION",
        union,
        per_recipient_payload=supplier,
    )

    rows = handler._event_log.read_since(since_seq=0)
    conf_rows = [r for r in rows if r.kind == "CONFRONTATION"]
    assert conf_rows, "canonical CONFRONTATION row must be persisted to the EventLog"
    persisted_beats = {b["id"] for b in json.loads(conf_rows[-1].payload_json)["beats"]}
    assert "UNION_MARKER" in persisted_beats, (
        f"EventLog must hold the canonical union; got {sorted(persisted_beats)}"
    )

    delivered = [
        out,
        *_drain_confrontations(queues["carl"]),
        *_drain_confrontations(queues["katia"]),
    ]
    for msg in delivered:
        if isinstance(msg, ConfrontationMessage):
            assert "UNION_MARKER" not in _beat_ids(msg), (
                f"union payload reached a client socket: beats={sorted(_beat_ids(msg))}. "
                f"The union must go to the EventLog ONLY."
            )


# ---------------------------------------------------------------------------
# AC3 + AC5 — handler-level behavior: single filtered fan-out, no union on any
# socket, solo/seated PC sees only their class's beats.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seated_pcs_each_receive_one_class_filtered_frame_no_union(
    session_handler_factory,
) -> None:
    """AC3 + AC5. Drive the real start path (``_execute_narration_turn``) with
    three seated PCs of distinct classes against a live combat encounter. Each
    connected socket — including the dispatcher/emitter — receives EXACTLY ONE
    CONFRONTATION frame, and it is class-filtered (no other-class beats). NO
    socket receives the union.

    Pre-fix every non-dispatcher seated peer received TWO frames — the
    canonical union (from the emit_event fan-out) followed by the filtered
    overlay — so this fails on both 'exactly one' and 'no union' for the
    peers. This is the overlay race the single-path design deletes.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "katia"  # dispatcher == the emitter
    sd.player_name = "Katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-seated"
    _wire_production_emit(handler, sd.game_slug)
    _inject_combat_pack(sd)
    _seat(
        sd, [("carl", "Carl", "Fighter"), ("donut", "Donut", "Cleric"), ("katia", "Katia", "Thief")]
    )
    _install_live_combat_encounter(sd, ["Carl", "Donut", "Katia"])
    queues = _connect_room(handler, sd.game_slug, ["carl", "donut", "katia"])
    handler._socket_id = "sock-katia"

    sd.orchestrator.run_narration_turn = _combat_mock()

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(sd, "Open combat.", _build_turn_context(sd))

    legal_by_pid = {
        "carl": ("shield_bash", _OTHER_CLASS_BEATS),  # Fighter
        "donut": ("turn_undead", {"backstab", "shield_bash", "cast_spell"}),  # Cleric
        "katia": ("backstab", {"shield_bash", "cast_spell", "turn_undead"}),  # Thief
    }
    for pid, (must_have, must_not_have) in legal_by_pid.items():
        frames = _drain_confrontations(queues[pid])
        assert len(frames) == 1, (
            f"{pid} must receive exactly one CONFRONTATION frame (single filtered "
            f"delivery path); got {len(frames)}. Pre-fix peers got union + overlay = 2."
        )
        ids = _beat_ids(frames[0])
        assert must_have in ids, (
            f"{pid}'s class-legal beat {must_have!r} missing; got {sorted(ids)}"
        )
        leaked = ids & set(must_not_have)
        assert not leaked, (
            f"{pid} leaked other-class beats {sorted(leaked)} (union reached the tab)"
        )


@pytest.mark.asyncio
async def test_solo_fighter_emitter_never_sees_other_class_beats(
    session_handler_factory,
) -> None:
    """AC5 (solo). The headline repro: a SOLO Fighter is the only seat and the
    emitter. Their delivered CONFRONTATION frame must contain only Fighter
    beats — never Backstab/Cast Spell/Turn Undead/etc. — and there must be
    exactly one frame (no union arriving last and clobbering the tab).
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "carl"
    sd.player_name = "Carl"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-solo"
    _wire_production_emit(handler, sd.game_slug)
    _inject_combat_pack(sd)
    _seat(sd, [("carl", "Carl", "Fighter")])
    _install_live_combat_encounter(sd, ["Carl"])
    queues = _connect_room(handler, sd.game_slug, ["carl"])
    handler._socket_id = "sock-carl"

    sd.orchestrator.run_narration_turn = _combat_mock()

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(sd, "I attack.", _build_turn_context(sd))

    frames = _drain_confrontations(queues["carl"])
    assert len(frames) == 1, (
        f"solo Fighter must receive exactly one CONFRONTATION frame; got {len(frames)}"
    )
    leaked = _beat_ids(frames[0]) & _OTHER_CLASS_BEATS
    assert not leaked, (
        f"solo Fighter was offered other classes' beats {sorted(leaked)} — the exact 59-16 bug."
    )


# ---------------------------------------------------------------------------
# AC3 — OTEL wiring: the class-filter span fires once per connected recipient.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_beat_filter_span_fires_once_per_connected_recipient(
    session_handler_factory,
    otel_capture,
) -> None:
    """AC3 (wiring). On a single CONFRONTATION emit the ``confrontation.beat_filter``
    span (source='ui_panel_projection') fires exactly once per connected seated
    recipient INCLUDING the emitter — proving the single filtered fan-out
    covers every socket.
    """
    from tests.server.conftest import span_attrs_by_name

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "katia"
    sd.player_name = "Katia"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-span"
    _wire_production_emit(handler, sd.game_slug)
    _inject_combat_pack(sd)
    _seat(
        sd, [("carl", "Carl", "Fighter"), ("donut", "Donut", "Cleric"), ("katia", "Katia", "Thief")]
    )
    _install_live_combat_encounter(sd, ["Carl", "Donut", "Katia"])
    _connect_room(handler, sd.game_slug, ["carl", "donut", "katia"])
    handler._socket_id = "sock-katia"

    sd.orchestrator.run_narration_turn = _combat_mock()

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(sd, "Open combat.", _build_turn_context(sd))

    panel_spans = [
        attrs
        for attrs in span_attrs_by_name(otel_capture, "confrontation.beat_filter")
        if attrs.get("source") == "ui_panel_projection"
    ]
    actors = sorted(a.get("actor") for a in panel_spans)
    assert actors == ["Carl", "Donut", "Katia"], (
        f"the panel-projection filter span must fire once per connected seated "
        f"recipient (incl. the emitter Katia); got actors={actors}"
    )


# ---------------------------------------------------------------------------
# AC4 — fail LOUD on an unresolvable seated PC; never fall back to the union.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seated_unresolvable_pc_fails_loud_and_gets_no_union_fallback(
    session_handler_factory,
    otel_capture,
) -> None:
    """AC4. A seated, connected PC whose class cannot be resolved (e.g. a save
    referencing a class the pack no longer defines) must NOT silently receive
    the unfiltered union. The contract: emit an ERROR span
    (``confrontation.recipient_unresolved``) and deliver no union frame to that
    socket. A lobby/unseated socket is NOT an error.

    Pre-fix the start-path overlay does ``if recipient_pc is None: continue``,
    silently skipping the filter, while the canonical fan-out delivers the
    union to that socket — exactly the silent fallback being removed.
    """
    from tests.server.conftest import span_attrs_by_name

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    sd.player_id = "carl"
    sd.player_name = "Carl"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = _SLUG + "-failloud"
    _wire_production_emit(handler, sd.game_slug)
    _inject_combat_pack(sd)
    # Carl is a normal seated Fighter (the dispatcher). Ghost is seated but
    # bound to a class the pack does not define → resolve_recipient_pc returns
    # (None, "Ghost"): a seated PC that won't resolve.
    _seat(sd, [("carl", "Carl", "Fighter"), ("ghost", "Ghost", "Nonexistent")])
    _install_live_combat_encounter(sd, ["Carl", "Ghost"])
    queues = _connect_room(handler, sd.game_slug, ["carl", "ghost"])
    handler._socket_id = "sock-carl"

    sd.orchestrator.run_narration_turn = _combat_mock()

    from sidequest.server.session_handler import _build_turn_context

    await handler._execute_narration_turn(sd, "Open combat.", _build_turn_context(sd))

    # No union fallback delivered to the unresolvable seated PC's socket.
    for msg in _drain_confrontations(queues["ghost"]):
        leaked = _beat_ids(msg) & _OTHER_CLASS_BEATS
        assert not leaked, (
            f"seated-unresolvable PC 'Ghost' received a union/unfiltered frame "
            f"with beats {sorted(leaked)} — the banned silent fallback (AC4)."
        )

    # And the failure is observable: a fail-loud span fires for the seated
    # unresolvable recipient (GM-panel lie-detector, CLAUDE.md OTEL principle).
    unresolved = span_attrs_by_name(otel_capture, "confrontation.recipient_unresolved")
    assert any(a.get("player_id") == "ghost" or a.get("actor") == "Ghost" for a in unresolved), (
        "a seated, connected PC that cannot be resolved to a class must emit a "
        "confrontation.recipient_unresolved ERROR span — never a silent skip."
    )
