"""Story 153-24 — RED: persist the room-graph axis as the dungeon is walked.

[DUNGEON-ROOM-STATE-NOT-PERSISTED] Playtest finding (2026-06-20/21, beneath_sunden):
after crossing into the dungeon the snapshot advanced the *region* axis
(``current_region`` entrance → exp002.r2, ``dungeon.map_emitted`` counting up) but
the *room* axis stayed blank the whole time — ``discovered_rooms: []``,
``room_states: {}``, ``characters[].current_room: None``. Forensics and cold reload
(ADR-133 / ADR-115) therefore see an empty dungeon even though the party demonstrably
moved.

These are wire-first behavior tests. They drive the production turn seam
(``WebSocketSessionHandler._execute_narration_turn`` →
``_apply_narration_result_to_snapshot``'s ``if result.location:`` branch at
narration_apply.py:4041) with a narrator result that CHANGES the acting character's
location, and assert that — in room-graph navigation mode — a room transition writes
the three room-axis fields and emits a discovery span. A region-mode regression guard
proves none of it fires under region navigation.

----------------------------------------------------------------------
CONTRACT NOTES — pinned by TEA (The Architect); mirror Story 71-15's notes.

- **Seam.** The transition seam is ``_apply_narration_result_to_snapshot``'s
  ``if result.location:`` branch (narration_apply.py:4041); a move is
  ``old_loc is not None and result.location != old_loc`` (line 4087). Today that block
  fires room-graph side-effects (trope tick + item depletion) but never appends the
  entered room to ``discovered_rooms``, never seeds ``room_states[new]``, and never sets
  ``Character.current_room``. That is the gap this story closes — wiring the existing
  seam to write the existing fields (ADR-055 persistence half), NOT a new system.

- **The catch-22.** The Story 71-15 side-effect block is GATED on
  ``snapshot.discovered_rooms`` being non-empty (narration_apply.py:4089). The room-axis
  write CANNOT use that gate or it never starts (the playtest case has
  ``discovered_rooms == []``). The seam now has ``pack``/``world`` context
  (Story 90-6), so the room-graph signal is ``cartography.navigation_mode ==
  room_graph`` — see ``TestPlaytestReproduction`` which starts from an EMPTY
  ``discovered_rooms``, the actual bug.

- **Room-graph signal.** ``_enter_room_graph_mode`` sets the world's
  ``navigation_mode = room_graph`` (the seam-readable signal) and threads
  ``sd.world_slug`` so ``pack.worlds.get(world)`` resolves. Tests that aren't the
  empty-repro ALSO seed ``discovered_rooms`` with the entrance (the realistic
  post-``init_room_graph_location`` state) so they are robust to whichever signal Dev
  keys on. The region-mode guard sets neither.

- **No-clobber (AC2).** ``room_states[room_id]`` is also created by the Story 45-43
  container-retrieval path (narration_apply.py:5086). Re-entering a room with existing
  ``room_states`` must PRESERVE it (containers, props), never overwrite with a fresh
  ``RoomState``.

- **Span (contract).** ``room.discovered`` carrying ``room_id``,
  ``newly_discovered`` (bool — first-discovery vs backtracking), ``discovered_count``,
  and ``character``. Dev may rename; if so, update these assertions in the same PR.

Every behavior assertion here is RED until Story 153-24 lands.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.session import ContainerState, GameSnapshot, RoomState
from sidequest.genre.models.world import NavigationMode
from sidequest.telemetry.setup import init_tracer
from tests.server.conftest import _build_turn_context_for_test

_ACTOR = "Rux"  # the character the session_handler_factory seeds
# The world name is derived from the loaded pack at runtime (the test harness
# monkeypatches the pack search path to tests/fixtures/packs, where
# caverns_and_claudes ships ``flickering_reach`` rather than the content pack's
# ``beneath_sunden``). Every world model carries a default CartographyConfig we can
# flip — see ``_pick_world``.

# Room ids walked in the tests. Clean, validate-able strings (Story 71-15 used the
# same shape); the playtest ids (entrance / exp002.r2) are mirrored in comments.
_ENTRANCE = "The Entrance"
_ROOM_2 = "The Crypt"
_ROOM_3 = "The Ossuary"

_DISCOVERED_SPAN = "room.discovered"


@pytest.fixture
def otel_capture():
    """Install an in-memory span exporter on the current TracerProvider."""

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()
        exporter.clear()


def _moving_orchestrator(location: str | None) -> AsyncMock:
    """Orchestrator that narrates a benign turn and emits a ``location`` field.

    A ``location`` distinct from the actor's current room is a room transition;
    equal is a re-entry of the current room (no move); ``None`` is a no-move turn.
    """

    return AsyncMock(
        return_value=NarrationTurnResult(
            narration="Footsteps echo on wet stone.",
            location=location,
            is_degraded=False,
            agent_duration_ms=1,
        )
    )


def _pick_world(sd) -> str:
    """Return a world name from the loaded pack and assert it has a CartographyConfig
    (every world model defaults one) — derived dynamically so the test does not
    depend on the fixture pack's specific world name."""

    worlds = sd.genre_pack.worlds
    assert worlds, "loaded pack must expose at least one world to flip nav mode on"
    name = next(iter(worlds))
    assert getattr(worlds[name], "cartography", None) is not None, (
        f"world {name!r} must carry a CartographyConfig to flip navigation_mode"
    )
    return name


def _enter_room_graph_mode(sd, *, seed_entrance: bool = True) -> None:
    """Put the session into room-graph navigation.

    Sets ``sd.world_slug`` (threaded to the apply seam as ``world=``) and flips the
    world's ``cartography.navigation_mode`` to ``room_graph`` — the seam-readable
    signal (the seam resolves ``pack.worlds.get(world).cartography``). When
    ``seed_entrance`` is True we also pre-populate ``discovered_rooms`` with the
    entrance (the realistic post-``init_room_graph_location`` state) so the test is
    robust to whichever room-graph signal Dev keys on.
    """

    name = _pick_world(sd)
    sd.world_slug = name
    sd.snapshot.world_slug = name
    sd.genre_pack.worlds[name].cartography.navigation_mode = NavigationMode.room_graph
    if seed_entrance and _ENTRANCE not in sd.snapshot.discovered_rooms:
        sd.snapshot.discovered_rooms.append(_ENTRANCE)


def _enter_region_mode(sd) -> None:
    """Put the session into region navigation (the default) with a resolvable world,
    so the seam's ``_is_region_mode_world`` evaluates True and the regression guard is
    honest (vs. an unresolved world that simply degrades)."""

    name = _pick_world(sd)
    sd.world_slug = name
    sd.snapshot.world_slug = name
    sd.genre_pack.worlds[name].cartography.navigation_mode = NavigationMode.region


def _place_actor(sd, room: str) -> None:
    sd.snapshot.character_locations[_ACTOR] = room


def _actor_character(sd):
    return next(c for c in sd.snapshot.characters if c.core.name == _ACTOR)


def _finished(otel_capture, name: str):
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


async def _move(handler, sd, *, to: str, action: str = "I press deeper.") -> None:
    """Drive one real narration turn whose narrator emits ``to`` as the location."""

    sd.orchestrator.run_narration_turn = _moving_orchestrator(to)
    ctx = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, action, ctx)


# ---------------------------------------------------------------------------
# AC1 — entered dungeon rooms are recorded in discovered_rooms (idempotent).
# ---------------------------------------------------------------------------


class TestDiscoveredRoomsRecorded:
    @pytest.mark.asyncio
    async def test_transition_appends_entered_room_to_discovered_rooms(
        self, session_handler_factory
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd, _ENTRANCE)

        await _move(handler, sd, to=_ROOM_2)

        assert _ROOM_2 in sd.snapshot.discovered_rooms, (
            "A room-graph transition must append the entered room to discovered_rooms "
            f"so forensics/reload sees the walk; discovered_rooms={sd.snapshot.discovered_rooms}"
        )

    @pytest.mark.asyncio
    async def test_reentering_known_room_does_not_duplicate(self, session_handler_factory) -> None:
        # Drive REAL transitions so the room is appended by the production write, then
        # re-entered — proving idempotency of the write itself (not a pre-seeded list).
        # Before the fix ROOM_2 is never appended, so count==0 and this fails RED.
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd, _ENTRANCE)

        await _move(handler, sd, to=_ROOM_2)  # first discovery -> append
        await _move(handler, sd, to=_ROOM_3, action="I climb to the ossuary.")
        await _move(handler, sd, to=_ROOM_2, action="I go back to the crypt.")  # re-entry

        assert _ROOM_2 in sd.snapshot.discovered_rooms, (
            "The write path must run (entered room recorded) before idempotency is "
            f"meaningful; discovered_rooms={sd.snapshot.discovered_rooms}"
        )
        assert sd.snapshot.discovered_rooms.count(_ROOM_2) == 1, (
            "Re-entering a known room must be idempotent (no duplicate); "
            f"discovered_rooms={sd.snapshot.discovered_rooms}"
        )


# ---------------------------------------------------------------------------
# AC1 verbatim — the playtest reproduction: discovered_rooms starts EMPTY (the bug),
# and after a cross BOTH endpoints must be present.
# ---------------------------------------------------------------------------


class TestPlaytestReproduction:
    @pytest.mark.asyncio
    async def test_empty_discovered_rooms_records_both_endpoints(
        self, session_handler_factory
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        # Reproduce the finding exactly: room-graph nav, but discovered_rooms == [].
        _enter_room_graph_mode(sd, seed_entrance=False)
        _place_actor(sd, _ENTRANCE)
        assert sd.snapshot.discovered_rooms == []  # the bug state

        await _move(handler, sd, to=_ROOM_2)  # entrance -> exp002.r2 (playtest cross)

        # AC1: "must now show both the entrance and exp002.r2".
        assert _ENTRANCE in sd.snapshot.discovered_rooms, (
            "After a cross from an empty discovered_rooms, the room the actor crossed "
            f"FROM (entrance) must be recorded; discovered_rooms={sd.snapshot.discovered_rooms}"
        )
        assert _ROOM_2 in sd.snapshot.discovered_rooms, (
            "After a cross from an empty discovered_rooms, the entered room must be "
            f"recorded; discovered_rooms={sd.snapshot.discovered_rooms}"
        )


# ---------------------------------------------------------------------------
# AC2 — per-room mechanical state is seeded in room_states (no clobber on re-entry).
# ---------------------------------------------------------------------------


class TestRoomStatesSeeded:
    @pytest.mark.asyncio
    async def test_first_entry_seeds_room_state(self, session_handler_factory) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd, _ENTRANCE)
        assert _ROOM_2 not in sd.snapshot.room_states

        await _move(handler, sd, to=_ROOM_2)

        assert _ROOM_2 in sd.snapshot.room_states, (
            "First entry to a room-graph room must seed room_states[room]; "
            f"room_states keys={list(sd.snapshot.room_states)}"
        )
        assert isinstance(sd.snapshot.room_states[_ROOM_2], RoomState), (
            "room_states value must be a RoomState model, not a bare dict/None"
        )

    @pytest.mark.asyncio
    async def test_reentry_preserves_existing_room_state(self, session_handler_factory) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        # ROOM_3 is the actor's current room; ROOM_2 carries lifecycle state the Story
        # 45-43 container path wrote on a prior visit (a retrieved chest + an
        # established prop) but is NOT yet in discovered_rooms — the two axes are
        # independent (room_states keyed by location vs. discovered_rooms membership).
        sd.snapshot.discovered_rooms.append(_ROOM_3)
        sd.snapshot.room_states[_ROOM_2] = RoomState(
            room_id=_ROOM_2,
            containers={
                "chest": ContainerState(container_id="chest", retrieved=True, retrieved_at_round=1)
            },
            props=["altar"],
        )
        # Actor is in ROOM_3 and backtracks (a real transition) into stateful ROOM_2.
        _place_actor(sd, _ROOM_3)

        await _move(handler, sd, to=_ROOM_2, action="I go back to the crypt.")

        # The write path must have run (ROOM_2 now recorded) — this is what fails RED
        # before the fix, so the no-clobber assertions below can't false-green.
        assert _ROOM_2 in sd.snapshot.discovered_rooms, (
            "Re-entry must still record the room on the discovery axis; "
            f"discovered_rooms={sd.snapshot.discovered_rooms}"
        )
        rs = sd.snapshot.room_states[_ROOM_2]
        assert "chest" in rs.containers and rs.containers["chest"].retrieved, (
            "Re-entry must not clobber container-retrieval state written by the "
            f"Story 45-43 path; containers={rs.containers}"
        )
        assert rs.props == ["altar"], f"Re-entry must preserve established props; props={rs.props}"


# ---------------------------------------------------------------------------
# AC3 — per-character room position is tracked.
# ---------------------------------------------------------------------------


class TestCurrentRoomTracked:
    @pytest.mark.asyncio
    async def test_transition_sets_acting_character_current_room(
        self, session_handler_factory
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd, _ENTRANCE)
        assert _actor_character(sd).current_room is None

        await _move(handler, sd, to=_ROOM_2)

        assert _actor_character(sd).current_room == _ROOM_2, (
            "A room-graph transition must set the acting character's current_room; "
            f"current_room={_actor_character(sd).current_room!r}"
        )

    @pytest.mark.asyncio
    async def test_current_room_follows_multi_turn_crawl(self, session_handler_factory) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd, _ENTRANCE)

        await _move(handler, sd, to=_ROOM_2)
        await _move(handler, sd, to=_ROOM_3, action="I climb to the ossuary.")

        assert _actor_character(sd).current_room == _ROOM_3, (
            "current_room must reflect the room the character currently stands in "
            f"across the crawl; current_room={_actor_character(sd).current_room!r}"
        )


# ---------------------------------------------------------------------------
# AC5 — OTEL watcher visibility: a room.discovered span distinguishes first-discovery
# from re-entry (genuine exploration vs backtracking).
# ---------------------------------------------------------------------------


class TestRoomDiscoveryOtelSpan:
    @pytest.mark.asyncio
    async def test_first_discovery_emits_span_newly_true(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd, _ENTRANCE)

        otel_capture.clear()
        await _move(handler, sd, to=_ROOM_2)

        spans = _finished(otel_capture, _DISCOVERED_SPAN)
        assert spans, (
            f"No {_DISCOVERED_SPAN!r} span on first discovery — the GM panel cannot "
            "confirm the room axis advanced (only the region axis emits today). "
            f"Spans: {[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("room_id") == _ROOM_2, f"span must carry room_id; attrs={attrs}"
        assert attrs.get("newly_discovered") is True, (
            f"first discovery must flag newly_discovered=True; attrs={attrs}"
        )
        assert "discovered_count" in attrs, f"span must carry discovered_count; attrs={attrs}"
        assert attrs.get("character") == _ACTOR, f"span must name the character; attrs={attrs}"

    @pytest.mark.asyncio
    async def test_reentry_emits_span_newly_false(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        sd.snapshot.discovered_rooms.extend([_ROOM_2, _ROOM_3])
        _place_actor(sd, _ROOM_3)

        otel_capture.clear()
        await _move(handler, sd, to=_ROOM_2, action="I go back to the crypt.")

        spans = _finished(otel_capture, _DISCOVERED_SPAN)
        assert spans, (
            f"Backtracking into a known room must still emit {_DISCOVERED_SPAN!r} "
            "(so the panel sees the move), flagged as re-entry. "
            f"Spans: {[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("newly_discovered") is False, (
            "re-entering a known room must flag newly_discovered=False so the panel "
            f"distinguishes exploration from backtracking; attrs={attrs}"
        )


# ---------------------------------------------------------------------------
# Region-mode regression — the room axis must NOT be written under region navigation
# (the working region axis is owned by the map handler; do not conflate the two).
# ---------------------------------------------------------------------------


class TestRegionModeUnaffected:
    @pytest.mark.asyncio
    async def test_region_mode_transition_does_not_write_room_axis(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_region_mode(sd)  # default navigation; NOT room-graph
        _place_actor(sd, _ENTRANCE)

        otel_capture.clear()
        await _move(handler, sd, to=_ROOM_2, action="I travel to the next region.")

        assert sd.snapshot.discovered_rooms == [], (
            "Region-mode traversal must not write the room axis; "
            f"discovered_rooms={sd.snapshot.discovered_rooms}"
        )
        assert sd.snapshot.room_states == {}, (
            f"Region-mode traversal must not seed room_states; room_states={sd.snapshot.room_states}"
        )
        assert _actor_character(sd).current_room is None, (
            "Region-mode traversal must not set current_room; "
            f"current_room={_actor_character(sd).current_room!r}"
        )
        assert not _finished(otel_capture, _DISCOVERED_SPAN), (
            "Region-mode traversal must not emit a room.discovered span"
        )


# ---------------------------------------------------------------------------
# AC4 + AC6 — wiring/integration: a multi-turn crawl through the REAL narration-apply
# seam, then a save→reload leg (ADR-115 serialization), proving forensics/reload sees
# the real dungeon. This is the reproduction of the playtest finding end-to-end.
# ---------------------------------------------------------------------------


class TestReloadRoundTrip:
    @pytest.mark.asyncio
    async def test_in_dungeon_snapshot_survives_save_reload(self, session_handler_factory) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd, seed_entrance=False)
        _place_actor(sd, _ENTRANCE)

        # Multi-turn traversal through the real handler seam: entrance -> r2 -> r3.
        await _move(handler, sd, to=_ROOM_2)
        await _move(handler, sd, to=_ROOM_3, action="I climb to the ossuary.")

        # Pre-reload: the live snapshot must already carry the room axis.
        assert _ROOM_2 in sd.snapshot.discovered_rooms
        assert _ROOM_3 in sd.snapshot.discovered_rooms
        assert _ROOM_3 in sd.snapshot.room_states
        assert _actor_character(sd).current_room == _ROOM_3

        # Save -> reload leg (ADR-115): the persisted snapshot serializes to JSON and
        # rehydrates. Old saves load empty (default_factory), so the write must precede
        # the save — which it does here. Forensics/reload must see the real dungeon.
        reloaded = GameSnapshot.model_validate_json(sd.snapshot.model_dump_json())

        assert _ROOM_2 in reloaded.discovered_rooms, (
            "discovered_rooms must survive save->reload; "
            f"reloaded.discovered_rooms={reloaded.discovered_rooms}"
        )
        assert _ROOM_3 in reloaded.discovered_rooms
        assert _ROOM_3 in reloaded.room_states, (
            f"room_states must survive save->reload; keys={list(reloaded.room_states)}"
        )
        reloaded_actor = next(c for c in reloaded.characters if c.core.name == _ACTOR)
        assert reloaded_actor.current_room == _ROOM_3, (
            "current_room must survive save->reload (no longer empty after a cross); "
            f"current_room={reloaded_actor.current_room!r}"
        )
