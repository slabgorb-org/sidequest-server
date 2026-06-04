"""RED tests: Bug A + Bug B — intent_router/equip spans must carry
turn_number, and the pre-narrator pass must be wrapped in a
timings.phase("intent_router_pass") context.

Diagnosis summary (from pre-fix read-only audit):
  Bug A: intent_router.dispatch_bank / equip.resolved / equip.unresolved spans
         omit turn_number → dashboard grid attributes them to turn 0 (prior
         column) instead of the current turn.
  Bug B: the pre-narrator pass call site in websocket_session_handler.py has
         no ``with timings.phase("intent_router_pass"):`` wrapper → the
         Timeline pipeline has no intent_router stage.

These tests drive the production code paths (not span hand-construction)
and assert both bugs are fixed.

OTEL principal (CLAUDE.md): span assertions, not source-text grep.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.agents.subsystems.equip import run_equip_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.telemetry.phase_timing import PhaseTimings

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _item(name: str, *, equipped: bool = False) -> dict:
    return {
        "id": f"narrator:{name.lower().replace(' ', '_')}",
        "name": name,
        "description": "a thing",
        "category": "armor",
        "equipped": equipped,
        "quantity": 1,
        "state": "Carried",
    }


def _character(name: str = "Dorothy", items: list[dict] | None = None) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="placeholder",
            personality="brave",
            inventory=Inventory(items=list(items or [])),
        ),
        char_class="Farmgirl",
        race="Human",
        backstory="From Kansas.",
    )


def _snapshot_with_turn(turn_number: int) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.turn_manager = TurnManager()
    # Advance to the desired interaction count.
    for _ in range(turn_number - 1):
        snap.turn_manager.record_interaction()
    return snap


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _equip_dispatch(item: str, *, key: str = "eq1") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="equip",
        params={"item": item, "action": "equip"},
        idempotency_key=key,
        confidence=1.0,
        visibility=_open_viz(),
    )


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-intent-router-otel")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _spans_named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# Bug A — equip.resolved span must carry turn_number
# ---------------------------------------------------------------------------


def test_equip_resolved_span_carries_turn_number(capture_spans):
    """equip.resolved must include turn_number matching
    snapshot.turn_manager.interaction so the dashboard attributes the span
    to the correct turn column (Bug A fix).

    Drives ``run_equip_dispatch`` through the real path with a snapshot
    whose turn counter is at 7.
    """
    EXPECTED_TURN = 7
    snap = _snapshot_with_turn(EXPECTED_TURN)
    char = _character(items=[_item("Silver Shoes")])
    snap.characters.append(char)

    asyncio.run(
        run_equip_dispatch(
            _equip_dispatch("Silver Shoes"),
            snapshot=snap,
            player_name="Dorothy",
        )
    )

    resolved = _spans_named(capture_spans, "equip.resolved")
    assert resolved, "equip.resolved span must fire on a successful equip"
    attr = resolved[0].attributes or {}
    assert "turn_number" in attr, (
        "equip.resolved span must include turn_number attribute (Bug A: "
        "missing turn_number causes the dashboard to attribute the span to "
        "the wrong turn column)"
    )
    assert attr["turn_number"] == EXPECTED_TURN, (
        f"equip.resolved turn_number must equal snapshot.turn_manager.interaction "
        f"({EXPECTED_TURN}); got {attr['turn_number']!r}"
    )


# ---------------------------------------------------------------------------
# Bug A — equip.unresolved span must carry turn_number
# ---------------------------------------------------------------------------


def test_equip_unresolved_span_carries_turn_number(capture_spans):
    """equip.unresolved must include turn_number for the same reason."""
    EXPECTED_TURN = 3
    snap = _snapshot_with_turn(EXPECTED_TURN)
    char = _character(items=[_item("Sensible Shoes")])
    snap.characters.append(char)

    asyncio.run(
        run_equip_dispatch(
            _equip_dispatch("Ruby Slippers"),  # item not found → unresolved
            snapshot=snap,
            player_name="Dorothy",
        )
    )

    unresolved = _spans_named(capture_spans, "equip.unresolved")
    assert unresolved, "equip.unresolved span must fire on item-not-found"
    attr = unresolved[0].attributes or {}
    assert "turn_number" in attr, "equip.unresolved span must include turn_number attribute (Bug A)"
    assert attr["turn_number"] == EXPECTED_TURN, (
        f"equip.unresolved turn_number must be {EXPECTED_TURN}; got {attr['turn_number']!r}"
    )


# ---------------------------------------------------------------------------
# Bug A — intent_router.dispatch_bank span must carry turn_number
# ---------------------------------------------------------------------------


def test_dispatch_bank_span_carries_turn_number(capture_spans):
    """intent_router.dispatch_bank must include turn_number in its span
    attributes (Bug A: the dashboard reads turn_number to grid events per turn).

    Drives run_dispatch_bank with an equip dispatch + snapshot at turn 5.
    """
    EXPECTED_TURN = 5
    snap = _snapshot_with_turn(EXPECTED_TURN)
    char = _character(items=[_item("Silver Shoes")])
    snap.characters.append(char)

    package = DispatchPackage(
        turn_id="t-bank-test",
        per_player=[
            PlayerDispatch(
                player_id="Dorothy",
                raw_action="lace on the silver shoes",
                dispatch=[_equip_dispatch("Silver Shoes")],
            )
        ],
        confidence_global=0.95,
    )

    asyncio.run(
        run_dispatch_bank(
            package,
            context={"snapshot": snap, "player_name": "Dorothy", "npcs_present": []},
        )
    )

    bank_spans = _spans_named(capture_spans, "intent_router.dispatch_bank")
    assert bank_spans, "intent_router.dispatch_bank span must fire during run_dispatch_bank"
    attr = bank_spans[0].attributes or {}
    assert "turn_number" in attr, (
        "intent_router.dispatch_bank span must include turn_number attribute (Bug A: "
        "the dashboard uses turn_number to grid spans per turn column)"
    )
    assert attr["turn_number"] == EXPECTED_TURN, (
        f"intent_router.dispatch_bank turn_number must equal "
        f"snapshot.turn_manager.interaction ({EXPECTED_TURN}); "
        f"got {attr['turn_number']!r}"
    )


# ---------------------------------------------------------------------------
# Off-by-one fix (DRIVER 2026-06-04): the dispatch bank runs BEFORE
# record_interaction() bumps the counter, but turn_complete emits
# turn_id=interaction AFTER the bump. So a span stamped with the raw
# pre-increment interaction lands one grid column to the LEFT — the
# just-completed turn shows DARK for intent_router/inventory even though the
# router resolved an intent. The caller now threads the EFFECTIVE turn number
# (interaction+1 for a player turn) through the pass → bank → subsystem/equip
# spans so they match the turn_complete column.
# ---------------------------------------------------------------------------


def test_dispatch_bank_turn_number_prefers_context_override(capture_spans):
    """When the caller threads an explicit ``turn_number`` in the bank context
    (the effective post-increment turn id), the dispatch_bank span carries THAT
    value, not the snapshot's pre-increment interaction."""
    snap = _snapshot_with_turn(3)  # pre-increment interaction == 3
    char = _character(items=[_item("Silver Shoes")])
    snap.characters.append(char)

    package = DispatchPackage(
        turn_id="t-override",
        per_player=[
            PlayerDispatch(
                player_id="Dorothy",
                raw_action="lace on the silver shoes",
                dispatch=[_equip_dispatch("Silver Shoes")],
            )
        ],
        confidence_global=0.95,
    )

    asyncio.run(
        run_dispatch_bank(
            package,
            context={
                "snapshot": snap,
                "player_name": "Dorothy",
                "npcs_present": [],
                # Effective turn = interaction+1, the value turn_complete emits.
                "turn_number": 4,
            },
        )
    )

    bank = _spans_named(capture_spans, "intent_router.dispatch_bank")
    assert bank, "dispatch_bank span must fire"
    assert (bank[0].attributes or {}).get("turn_number") == 4, (
        "dispatch_bank must use the threaded effective turn_number (4), not the "
        f"snapshot's pre-increment interaction (3); got {(bank[0].attributes or {}).get('turn_number')!r}"
    )


def test_subsystem_span_carries_threaded_turn_number(capture_spans):
    """The per-subsystem span (intent_router.subsystem) must carry turn_number
    so the GM-panel grid lights the subsystem on the correct turn column —
    without it the row inherits the prior turn via arrival-order fallback."""
    snap = _snapshot_with_turn(3)
    char = _character(items=[_item("Silver Shoes")])
    snap.characters.append(char)

    package = DispatchPackage(
        turn_id="t-sub",
        per_player=[
            PlayerDispatch(
                player_id="Dorothy",
                raw_action="lace on the silver shoes",
                dispatch=[_equip_dispatch("Silver Shoes")],
            )
        ],
        confidence_global=0.95,
    )

    asyncio.run(
        run_dispatch_bank(
            package,
            context={
                "snapshot": snap,
                "player_name": "Dorothy",
                "npcs_present": [],
                "turn_number": 4,
            },
        )
    )

    subs = _spans_named(capture_spans, "intent_router.subsystem")
    assert subs, "intent_router.subsystem span must fire per dispatch"
    assert (subs[0].attributes or {}).get("turn_number") == 4, (
        "intent_router.subsystem must carry the threaded turn_number (4); got "
        f"{(subs[0].attributes or {}).get('turn_number')!r}"
    )


def test_equip_resolved_turn_number_prefers_explicit_param(capture_spans):
    """``run_equip_dispatch(turn_number=N)`` stamps N on equip.resolved,
    overriding the snapshot's pre-increment interaction (the off-by-one)."""
    snap = _snapshot_with_turn(3)  # interaction == 3
    char = _character(items=[_item("Silver Shoes")])
    snap.characters.append(char)

    asyncio.run(
        run_equip_dispatch(
            _equip_dispatch("Silver Shoes"),
            snapshot=snap,
            player_name="Dorothy",
            turn_number=4,  # effective post-increment turn
        )
    )

    resolved = _spans_named(capture_spans, "equip.resolved")
    assert resolved, "equip.resolved span must fire"
    assert (resolved[0].attributes or {}).get("turn_number") == 4, (
        "equip.resolved must use the explicit turn_number (4), not interaction (3); "
        f"got {(resolved[0].attributes or {}).get('turn_number')!r}"
    )


@pytest.mark.asyncio
async def test_pre_pass_threads_effective_turn_number_to_spans(capture_spans):
    """End-to-end: execute_intent_router_pre_narrator_pass(turn_number=N) threads
    N through the bank so the dispatch_bank AND equip.resolved spans carry N —
    matching the turn_complete column rather than the pre-increment interaction."""
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        player_seats={"p1": "Dorothy"},
    )
    snap.turn_manager = TurnManager()  # interaction == 1 (pre-increment)
    snap.characters.append(_character(items=[_item("Silver Shoes")]))

    pack = MagicMock()
    pack.rules = MagicMock()
    pack.rules.confrontations = []
    pack.witnessed_acts = []

    package = DispatchPackage(
        turn_id="t-e2e",
        per_player=[
            PlayerDispatch(
                player_id="p1",
                raw_action="lace on the silver shoes",
                dispatch=[_equip_dispatch("Silver Shoes")],
            )
        ],
        confidence_global=0.9,
    )
    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="lace on the silver shoes",
        player_name="Dorothy",
        turn_number=2,  # interaction(1) + 1 — the value turn_complete will emit
    )

    bank = _spans_named(capture_spans, "intent_router.dispatch_bank")
    assert bank and (bank[0].attributes or {}).get("turn_number") == 2, (
        "pre-pass must thread turn_number=2 to the dispatch_bank span; got "
        f"{[(s.attributes or {}).get('turn_number') for s in bank]!r}"
    )
    resolved = _spans_named(capture_spans, "equip.resolved")
    assert resolved and (resolved[0].attributes or {}).get("turn_number") == 2, (
        "pre-pass must thread turn_number=2 down to equip.resolved; got "
        f"{[(s.attributes or {}).get('turn_number') for s in resolved]!r}"
    )


def test_effective_dispatch_turn_number_arithmetic():
    """The pure helper the caller uses: a player turn will run record_interaction
    (interaction+1 → the turn_complete column), the opening scene-set will not."""
    from sidequest.server.intent_router_pass import effective_dispatch_turn_number

    tm = TurnManager()  # interaction == 1
    # Player turn: record_interaction WILL fire → effective is interaction+1.
    assert effective_dispatch_turn_number(tm, is_opening_turn=False) == 2
    # Opening scene-set: record_interaction is skipped → effective is interaction.
    assert effective_dispatch_turn_number(tm, is_opening_turn=True) == 1


# ---------------------------------------------------------------------------
# Bug B — intent_router_pass must be wrapped in timings.phase("intent_router_pass")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intent_router_pass_records_phase_timing():
    """execute_intent_router_pre_narrator_pass must be wrapped in a
    timings.phase("intent_router_pass") context manager (Bug B: without this,
    the Timeline pipeline has no intent_router stage and the cell is dark).

    Drives execute_intent_router_pre_narrator_pass through a stubbed router
    (no SDK calls) against a PhaseTimings instance, then asserts
    "intent_router_pass" appears in the recorded phase totals.
    """
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        player_seats={"p1": "Dorothy"},
    )
    snap.turn_manager = TurnManager()

    # Minimal synthetic pack: rules with one confrontation def so the
    # confrontation vocabulary span fires, but the pack itself is not needed
    # for the phase-timing assertion.
    pack = MagicMock()
    pack.rules = MagicMock()
    pack.rules.confrontations = []
    pack.witnessed_acts = []

    # Stub router: returns an empty (zero-dispatch) package immediately.
    empty_package = DispatchPackage(
        turn_id="t-phase-test",
        per_player=[
            PlayerDispatch(
                player_id="p1",
                raw_action="look around",
                dispatch=[],
            )
        ],
        confidence_global=0.5,
    )
    router = MagicMock()
    router.decompose = AsyncMock(return_value=empty_package)

    timings = PhaseTimings(action_received_monotonic=0.0)

    # The production call site passes timings via turn_context.phase_timings.
    # Here we call the helper directly and pass timings through the same
    # mechanism (kwarg injection added by Bug B fix).
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="look around",
        player_name="Dorothy",
        phase_timings=timings,
    )

    phase_counts = timings.phase_call_counts
    assert "intent_router_pass" in phase_counts, (
        "execute_intent_router_pre_narrator_pass must record a "
        "'intent_router_pass' phase in PhaseTimings (Bug B: without this "
        "wrapper the Timeline pipeline has no intent_router stage). "
        f"Recorded phases: {sorted(phase_counts)}"
    )
    assert phase_counts["intent_router_pass"] == 1, (
        "intent_router_pass phase must be recorded exactly once per call; "
        f"got {phase_counts['intent_router_pass']}"
    )
