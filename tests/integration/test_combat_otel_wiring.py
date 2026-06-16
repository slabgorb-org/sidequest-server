"""End-to-end wiring for combat resolution OTEL events.

Sprint 3 cold-subsystem audit found combat resolution emitting zero
typed watcher events: ``encounter.edge_debit`` and
``encounter.composure_break`` were both pinned to ``FLAT_ONLY_SPANS``,
so the GM panel's ``state_transition`` tab couldn't show damage
application or composure breaks. This test pins the production path:
``apply_beat`` opens the spans, ``WatcherSpanProcessor`` translates
them through ``SPAN_ROUTES``, and the hub publishes
``state_transition`` events with ``component=combat``.

Same shape as ``test_inventory_wiring.py`` and ``test_npc_wiring.py``:
local ``TracerProvider`` + ``WatcherSpanProcessor`` + monkeypatched
``spans_module.tracer`` so the encounter helpers resolve to the
test's tracer regardless of the global provider state. That harness
(``watcher_setup`` + ``wait_for_state_transition``) is shared via
``tests/integration/conftest.py`` (story 71-34).
"""

from __future__ import annotations

import asyncio

import pytest

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.dice import RollOutcome
from tests.integration.conftest import wait_for_state_transition, watcher_setup


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )


def _core(name: str, *, current: int = 10, max_: int = 10) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="x",
        personality="x",
        hp=HpPool(current=current, max=max_, base_max=max_),
    )


def _strike(target_edge_delta: int) -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": "attack",
            "label": "attack",
            "kind": "strike",
            "base": 2,
            "stat_check": "STR",
            "target_edge_delta": target_edge_delta,
        }
    )


async def _wait_for_event(
    captured: list[dict], field_value: str, *, timeout_s: float = 1.0
) -> dict:
    """Poll ``captured`` for a ``state_transition`` whose ``fields.field``
    matches ``field_value`` (thin wrapper over the shared
    ``wait_for_state_transition`` harness)."""
    return await wait_for_state_transition(
        captured,
        lambda evt: evt.get("fields", {}).get("field") == field_value,
        timeout_s=timeout_s,
        describe=f"with field={field_value!r}",
    )


@pytest.mark.asyncio
async def test_apply_beat_target_edge_delta_publishes_edge_debit_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target-edge-delta beat must publish a typed state_transition
    with ``component=combat`` and ``field=encounter.edge_debit`` —
    proving ``apply_beat`` → ``encounter_edge_debit_span`` →
    ``WatcherSpanProcessor`` → ``SPAN_ROUTES[encounter.edge_debit]``
    is wired end-to-end."""
    captured = await watcher_setup(monkeypatch, "test-combat-edge-debit-wiring")

    enc = _enc()
    sam = enc.find_actor("Sam")
    cores = {"Sam": _core("Sam"), "Promo": _core("Promo", current=10)}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=3),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    # Yield to let any queued coroutine_threadsafe broadcasts land.
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "encounter.edge_debit")
    assert evt["component"] == "combat"
    assert evt["fields"]["source_actor"] == "Sam"
    assert evt["fields"]["target_actor"] == "Promo"
    assert evt["fields"]["debit_kind"] == "target"
    assert evt["fields"]["delta"] == -3
    assert evt["fields"]["before"] == 10
    assert evt["fields"]["after"] == 7
    assert evt["fields"]["beat_id"] == "attack"


@pytest.mark.asyncio
async def test_apply_beat_composure_break_publishes_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a beat drops the target's edge to zero, both
    ``encounter.edge_debit`` AND ``encounter.composure_break`` must
    reach the hub as typed state_transitions."""
    captured = await watcher_setup(monkeypatch, "test-combat-composure-break-wiring")

    enc = _enc()
    sam = enc.find_actor("Sam")
    cores = {"Sam": _core("Sam"), "Promo": _core("Promo", current=2)}

    apply_beat(
        enc,
        sam,
        _strike(target_edge_delta=5),  # overkill
        RollOutcome.Success,
        edge_resolver=cores.get,
    )
    await asyncio.sleep(0)

    debit = await _wait_for_event(captured, "encounter.edge_debit")
    assert debit["fields"]["after"] == 0  # clamped at 0

    brk = await _wait_for_event(captured, "encounter.composure_break")
    assert brk["component"] == "combat"
    assert brk["fields"]["char_name"] == "Promo"
    assert brk["fields"]["side"] == "target"
    assert brk["fields"]["beat_id"] == "attack"
