"""Story 71-15 — RED: per-transition trope tick + item resource depletion
on room-graph movement (ADR-055).

Wire-first behavior tests. They drive the production turn seam
(``WebSocketSessionHandler._execute_narration_turn`` →
``_apply_narration_result_to_snapshot``'s ``if result.location:`` branch)
with a narrator result that CHANGES the acting character's location, and
assert that — in room-graph navigation mode only — a room transition:

1. advances a progressing trope once via the existing ``tick_tropes``
   machinery and emits a transition-tick OTEL span (AC1 / AC5);
2. does not compound the existing per-turn tick (AC2 — no double-tick);
3. is idempotent on re-entry of the same room (AC3);
4. decrements a movement-consumed item's ``uses_remaining`` once, marking
   it exhausted at zero without deleting it (AC4), and
5. emits a resource-depletion OTEL span carrying before/after (AC5).

A region-mode regression guard proves none of this fires under region
navigation.

ADR-055's "Implementation status § Dark" lists both behaviors as unwired
and the 2026-05-28 amendment confirms it against HEAD. Every behavior
test here is RED until story 71-15 lands.

----------------------------------------------------------------------
CONTRACT NOTES — pinned by TEA (Fezzik); see session Design Deviations.
The context doc's technical approach was built on a factual error and is
corrected here:

- **Seam.** The context pins ``process_room_entry`` (room_movement.py:59)
  as the "idempotent, records-visited-room" transition hook. It is NOT —
  it is a chassis/intimate-confrontation auto-fire hook that early-returns
  for non-chassis rooms and records a *cooldown stamp*, not
  ``discovered_rooms``. The promised room-graph movement functions
  (``validate_room_transition`` / ``apply_validated_move``) never landed.
  The real transition seam is ``_apply_narration_result_to_snapshot``'s
  ``if result.location:`` branch (narration_apply.py:1734), where a move is
  ``old_loc != result.location``.

- **Room-graph signal.** The apply seam has no world context today, so the
  gate mechanism is a Dev decision. These tests signal room-graph mode two
  ways at once (seed ``snapshot.discovered_rooms`` AND flip any loaded
  world's ``cartography.navigation_mode``) so the suite is robust to either
  choice. Region-mode tests set neither.

- **No-double-tick rule.** A transition turn advances a progressing trope
  by exactly ONE tick's worth — the transition tick is the room-graph
  equivalent of the per-turn tick, not additive (Architect's reading,
  context Assumptions).

- **Exhausted state.** At ``uses_remaining == 0`` the item dict carries
  ``exhausted=True`` and stays in inventory (no silent delete).

- **Movement-consumed items.** Any inventory item with a finite (non-None)
  ``uses_remaining`` depletes one per transition (torch model).
  Ability-charge depletion is out of scope (context Assumptions).

- **Span names (contract).** ``room.transition_tick`` (carries the advanced
  trope ids) and ``item.resource_depleted`` (carries ``item`` / ``before`` /
  ``after``). Dev may rename; if so, update these assertions in the same PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.session import TropeState
from sidequest.genre.models.world import NavigationMode
from sidequest.telemetry.setup import init_tracer
from tests.server.conftest import _build_turn_context_for_test

# Trope ids/rates from the frozen fixture pack
# (tests/fixtures/packs/test_genre/tropes.yaml). ruin_fever's first beat
# threshold is 0.20, so seeding at 0.10 keeps a single tick below it.
_TROPE_ID = "ruin_fever"
_TROPE_RATE = 0.0125
_TROPE_SEED = 0.10

_ACTOR = "Rux"  # the character the session_handler_factory seeds
_OLD_ROOM = "The Entrance"
_NEW_ROOM = "The Crypt"

_TRANSITION_TICK_SPAN = "room.transition_tick"
_DEPLETION_SPAN = "item.resource_depleted"

_TRANSITION_SPAN_NAMES = {_TRANSITION_TICK_SPAN, _DEPLETION_SPAN}


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
    """Orchestrator that narrates a benign turn and (optionally) emits a
    ``location`` field. ``location`` distinct from the actor's current room
    is a room transition; equal is a re-entry; ``None`` is a no-move turn.
    """

    return AsyncMock(
        return_value=NarrationTurnResult(
            narration="Footsteps echo on wet stone.",
            location=location,
            is_degraded=False,
            agent_duration_ms=1,
        )
    )


def _seed_progressing_trope(sd) -> None:
    sd.snapshot.active_tropes.append(
        TropeState(id=_TROPE_ID, status="progressing", progress=_TROPE_SEED, beats_fired=0)
    )


def _seed_torch(sd, uses_remaining: int) -> None:
    """Seed a movement-consumed light source into Rux's inventory.

    Runtime inventory items are plain dicts (``Inventory.items: list[dict]``).
    ``uses_remaining`` is seeded from ``CatalogItem.resource_ticks`` in
    chargen_loadout.py:66; here we set it directly.
    """

    sd.snapshot.characters[0].core.inventory.items.append(
        {
            "id": "torch",
            "name": "Torch",
            "category": "gear",
            "uses_remaining": uses_remaining,
        }
    )


def _torch(sd) -> dict:
    items = sd.snapshot.characters[0].core.inventory.items
    return next(i for i in items if i.get("id") == "torch")


def _enter_room_graph_mode(sd) -> None:
    """Put the session into room-graph navigation, robust to either gate
    mechanism Dev may choose (snapshot state container OR world cartography).
    """

    # discovered_rooms is the room-graph state container, populated only by
    # init_room_graph_location (room_graph mode). A non-empty list is the
    # state-side signal that we are crawling a room graph.
    if _OLD_ROOM not in sd.snapshot.discovered_rooms:
        sd.snapshot.discovered_rooms.append(_OLD_ROOM)
    # Pack-side signal: flip any loaded world's navigation mode.
    for world in getattr(sd.genre_pack, "worlds", {}).values():
        cart = getattr(world, "cartography", None)
        if cart is not None:
            cart.navigation_mode = NavigationMode.room_graph


def _place_actor(sd) -> None:
    sd.snapshot.character_locations[_ACTOR] = _OLD_ROOM


def _finished(otel_capture, name: str):
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# AC1 / AC5 — a room-graph transition emits a transition-tick span and
# advances the progressing trope.
# ---------------------------------------------------------------------------


class TestTransitionTickSpan:
    @pytest.mark.asyncio
    async def test_room_graph_transition_emits_transition_tick_span(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd)
        _seed_progressing_trope(sd)
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_NEW_ROOM)

        otel_capture.clear()
        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I descend the stair.", ctx)

        spans = _finished(otel_capture, _TRANSITION_TICK_SPAN)
        assert spans, (
            f"No {_TRANSITION_TICK_SPAN!r} span on a room-graph transition — "
            "the GM panel cannot see that traversal advanced the clock. "
            f"Spans: {[s.name for s in otel_capture.get_finished_spans()]}"
        )

    @pytest.mark.asyncio
    async def test_transition_tick_span_names_the_advanced_trope(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd)
        _seed_progressing_trope(sd)
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_NEW_ROOM)

        otel_capture.clear()
        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I descend the stair.", ctx)

        spans = _finished(otel_capture, _TRANSITION_TICK_SPAN)
        assert spans, f"{_TRANSITION_TICK_SPAN!r} span did not fire"
        attrs = dict(spans[0].attributes or {})
        # The advanced trope id must be discoverable on the span (exact
        # attribute key is implementer's choice; the id must appear).
        flat = " ".join(str(v) for v in attrs.values())
        assert _TROPE_ID in flat, (
            f"transition-tick span must carry the advanced trope id "
            f"{_TROPE_ID!r}; attrs={attrs}"
        )


# ---------------------------------------------------------------------------
# AC1 + AC2 — the transition advances the trope by exactly one tick's worth
# (no double-tick against the per-turn tick).
# ---------------------------------------------------------------------------


class TestNoDoubleTick:
    @pytest.mark.asyncio
    async def test_transition_turn_advances_trope_exactly_once(
        self, session_handler_factory
    ) -> None:
        from sidequest.game.trope_tuning import PROGRESSION_RATE_MULTIPLIER

        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd)
        _seed_progressing_trope(sd)
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_NEW_ROOM)

        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I descend the stair.", ctx)

        trope = next(t for t in sd.snapshot.active_tropes if t.id == _TROPE_ID)
        # Exactly one tick's delta — the transition tick is the room-graph
        # equivalent of the per-turn tick, not additive. A naive additive
        # wiring would land at _TROPE_SEED + 2 * delta.
        one_tick = _TROPE_RATE * PROGRESSION_RATE_MULTIPLIER
        expected = _TROPE_SEED + one_tick
        assert trope.progress == pytest.approx(expected, abs=1e-6), (
            f"progress={trope.progress}, expected≈{expected} (one tick). "
            f"A double-advance (≈{_TROPE_SEED + 2 * one_tick}) means the "
            "transition tick compounded the per-turn tick."
        )


# ---------------------------------------------------------------------------
# AC3 — idempotent re-entry: re-narrating the current room is not a
# transition, so nothing ticks or depletes.
# ---------------------------------------------------------------------------


class TestIdempotentReEntry:
    @pytest.mark.asyncio
    async def test_reentering_same_room_fires_no_transition_spans(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd)
        _seed_progressing_trope(sd)
        _seed_torch(sd, uses_remaining=3)
        # Narrator emits the room the actor is already in — not a move.
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_OLD_ROOM)

        otel_capture.clear()
        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I look around the room.", ctx)

        names = {s.name for s in otel_capture.get_finished_spans()}
        assert not (names & _TRANSITION_SPAN_NAMES), (
            "Re-entering the current room must not fire transition-tick or "
            f"depletion spans; saw {names & _TRANSITION_SPAN_NAMES}."
        )
        assert _torch(sd)["uses_remaining"] == 3, (
            "Re-entry must not deplete the torch; "
            f"uses_remaining={_torch(sd)['uses_remaining']}"
        )


# ---------------------------------------------------------------------------
# AC4 / AC5 — movement-consumed item depletes once per transition, emits a
# depletion span, and is marked exhausted (not deleted) at zero.
# ---------------------------------------------------------------------------


class TestItemDepletion:
    @pytest.mark.asyncio
    async def test_torch_decrements_once_on_transition(
        self, session_handler_factory
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd)
        _seed_torch(sd, uses_remaining=3)
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_NEW_ROOM)

        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I press deeper.", ctx)

        assert _torch(sd)["uses_remaining"] == 2, (
            "Torch must burn one use per room-graph transition; "
            f"uses_remaining={_torch(sd)['uses_remaining']} (expected 2)."
        )

    @pytest.mark.asyncio
    async def test_depletion_span_carries_before_and_after(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd)
        _seed_torch(sd, uses_remaining=3)
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_NEW_ROOM)

        otel_capture.clear()
        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I press deeper.", ctx)

        spans = _finished(otel_capture, _DEPLETION_SPAN)
        assert spans, (
            f"No {_DEPLETION_SPAN!r} span on depletion — the GM panel cannot "
            "verify the torch burned. Spans: "
            f"{[s.name for s in otel_capture.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("before") == 3, f"span.before must be 3; attrs={attrs}"
        assert attrs.get("after") == 2, f"span.after must be 2; attrs={attrs}"
        flat = " ".join(str(v) for v in attrs.values())
        assert "torch" in flat, f"depletion span must name the item; attrs={attrs}"

    @pytest.mark.asyncio
    async def test_torch_marked_exhausted_at_zero_not_deleted(
        self, session_handler_factory
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _enter_room_graph_mode(sd)
        _place_actor(sd)
        _seed_torch(sd, uses_remaining=1)
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_NEW_ROOM)

        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I take the last step.", ctx)

        torch = _torch(sd)  # must still be in inventory — no silent delete
        assert torch["uses_remaining"] == 0, (
            f"Torch must hit 0 uses; uses_remaining={torch['uses_remaining']}"
        )
        assert torch.get("exhausted") is True, (
            "Torch at zero uses must be flagged exhausted (not silently "
            f"deleted); item={torch}"
        )


# ---------------------------------------------------------------------------
# Region-mode regression — none of this fires under region navigation.
# ---------------------------------------------------------------------------


class TestRegionModeUnaffected:
    @pytest.mark.asyncio
    async def test_region_mode_location_change_fires_nothing(
        self, session_handler_factory, otel_capture
    ) -> None:
        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        # NOTE: no _enter_room_graph_mode — default region navigation.
        _place_actor(sd)
        _seed_progressing_trope(sd)
        _seed_torch(sd, uses_remaining=3)
        sd.orchestrator.run_narration_turn = _moving_orchestrator(_NEW_ROOM)

        otel_capture.clear()
        ctx = _build_turn_context_for_test(sd)
        await handler._execute_narration_turn(sd, "I travel to the next region.", ctx)

        names = {s.name for s in otel_capture.get_finished_spans()}
        assert not (names & _TRANSITION_SPAN_NAMES), (
            "Region-mode traversal must not fire room-graph transition "
            f"side-effects; saw {names & _TRANSITION_SPAN_NAMES}."
        )
        assert _torch(sd)["uses_remaining"] == 3, (
            "Region-mode movement must not deplete the torch; "
            f"uses_remaining={_torch(sd)['uses_remaining']}"
        )
