"""PLAYTEST BUG (caverns_and_claudes/beneath_sunden, WWN, 2026-06-27, solo "Groucho"):

A delver in an unlit region with the −2 darkness penalty said **"take up a torch."**
The IntentRouter classified that as an ``equip`` (draw/wield a carried item), NOT an
``environment_clock`` ``mode="relight"`` (which only fires on "I *light* a torch").
So the torch's ``equipped`` flag flipped, but the ``light`` survival-clock pool stayed
at its floor (0), no torch charge was consumed, and the −2 ``environment_clock``
darkness ``Status`` persisted — while the narrator improvised "the torch catches,
torchlight blooms." Stored snapshot: ``light 0/24`` + a live darkness status + **3
torches still carried (none consumed)**. The phantom −2 then taints every WWN roll,
including the combat resolution that playtest existed to verify. The lie-detector
firing: narration says lit, engine says dark.

ROOT-CAUSE FIX: equipping an item tagged ``light_source`` while the PC is in the dark
(the ``light`` pool is at/below its floor) deterministically drives a relight — refill
the pool to max, consume one torch charge, clear the darkness penalty, emit the
``light.relit`` span — instead of depending on a Haiku pass to disambiguate
"take up a torch" (equip) from "light a torch" (relight). A spare ``light_source``
taken up while already lit is NOT wastefully burned (gate on in-the-dark).

These are behavior + OTEL-span tests on ``run_equip_dispatch``. RED until the equip
handler drives the relight.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems.environment_clock import (
    DARKNESS_STATUS_SOURCE,
    run_environment_clock_dispatch,
)
from sidequest.agents.subsystems.equip import run_equip_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.resource_pool import ResourcePool, ResourceThreshold
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

SPAN_LIGHT_RELIT = "light.relit"


# ---------------------------------------------------------------------------
# Fixtures (content-free; mirrors test_environment_clock.py + test_equip_dispatch.py)
# ---------------------------------------------------------------------------


def _tag_all() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _torch(*, equipped: bool = False, charges: int = 3) -> dict:
    """A real light source: the dedicated ``light_source`` tag + a ``quantity``
    charge count (one item = one relight to max), matching the WWN content torch."""
    return {
        "id": "torch",
        "name": "Torch",
        "description": "A pitch-soaked torch.",
        "category": "gear",
        "tags": ["light", "light_source", "consumable", "essential"],
        "equipped": equipped,
        "quantity": charges,
        "state": "Carried",
    }


def _armor(*, equipped: bool = False) -> dict:
    return {
        "id": "leather_armor",
        "name": "Leather Armor",
        "description": "Boiled leather.",
        "category": "armor",
        "tags": [],
        "equipped": equipped,
        "quantity": 1,
        "state": "Carried",
    }


def _snap(light_current: float, items: list[dict], *, name: str = "Delver") -> GameSnapshot:
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.resources["light"] = ResourcePool(
        name="light",
        label="Light",
        current=light_current,
        min=0.0,
        max=6.0,
        voluntary=False,
        decay_per_turn=0.0,
        thresholds=[
            ResourceThreshold(at=0.0, event_id="dark", narrator_hint="the dark closes in"),
        ],
    )
    snap.characters.append(
        Character(
            core=CreatureCore(
                name=name,
                description="A torch-bearing delver.",
                personality="cautious",
                inventory=Inventory(items=list(items)),
            ),
            char_class="Warrior",
            race="Human",
            backstory="Descends into the dark.",
        )
    )
    return snap


def _equip_dispatch(item: str, *, action: str = "equip") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="equip",
        params={"item": item, "action": action},
        idempotency_key="equip_1",
        confidence=1.0,
        visibility=_tag_all(),
    )


def _tick_dispatch() -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="environment_clock",
        params={"region": "entrance", "lit": False, "character_name": "Delver"},
        idempotency_key="environment_clock_1",
        confidence=1.0,
        visibility=_tag_all(),
    )


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-equip-relight")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _darkness_statuses(snap: GameSnapshot) -> list:
    core = snap.find_creature_core("Delver")
    assert core is not None
    return [s for s in core.statuses if s.source == DARKNESS_STATUS_SOURCE]


# ---------------------------------------------------------------------------
# 1 — "take up a torch" in the dark: equip a light_source clears darkness +
#     refills the light pool + consumes a charge + fires light.relit.
# ---------------------------------------------------------------------------


def test_equip_torch_in_dark_relights_and_clears_penalty(capture_spans):
    snap = _snap(0.0, [_torch(charges=3)])  # dark, 3 torches
    # The unlit tick stamps the −2 darkness penalty (the playtest's turn-3 state).
    asyncio.run(run_environment_clock_dispatch(_tick_dispatch(), snapshot=snap))
    assert _darkness_statuses(snap), "precondition: darkness penalty is present"

    out = asyncio.run(
        run_equip_dispatch(_equip_dispatch("Torch"), snapshot=snap, player_name="Delver")
    )

    light = snap.resources["light"]
    assert light.current == light.max, "equipping a torch in the dark refills the light pool"
    assert not _darkness_statuses(snap), "the −2 darkness penalty is cleared"
    # One charge consumed (3 → 2); the torch is also now equipped.
    torch = next(it for it in snap.find_creature_core("Delver").inventory.items
                 if it.get("id") == "torch")
    assert torch["quantity"] == 2
    assert torch["equipped"] is True
    assert out.data.get("relit") is True
    # The light.relit lie-detector span fired (GM-panel verification).
    assert [s for s in capture_spans.get_finished_spans() if s.name == SPAN_LIGHT_RELIT]


# ---------------------------------------------------------------------------
# 2 — equipping a NON-light-source in the dark must NOT relight (no free light).
# ---------------------------------------------------------------------------


def test_equip_armor_in_dark_does_not_relight(capture_spans):
    snap = _snap(0.0, [_armor()])
    asyncio.run(run_environment_clock_dispatch(_tick_dispatch(), snapshot=snap))
    assert _darkness_statuses(snap)

    asyncio.run(
        run_equip_dispatch(_equip_dispatch("Leather Armor"), snapshot=snap, player_name="Delver")
    )

    assert snap.resources["light"].current == 0.0, "armor is not a light source — no relight"
    assert _darkness_statuses(snap), "darkness penalty persists (armor gives no light)"
    assert not [s for s in capture_spans.get_finished_spans() if s.name == SPAN_LIGHT_RELIT]


# ---------------------------------------------------------------------------
# 3 — equipping a spare torch while ALREADY lit must NOT waste a charge.
# ---------------------------------------------------------------------------


def test_equip_torch_while_lit_does_not_waste_a_charge(capture_spans):
    snap = _snap(6.0, [_torch(charges=3)])  # pool full → already lit, not in the dark
    assert not _darkness_statuses(snap)

    asyncio.run(
        run_equip_dispatch(_equip_dispatch("Torch"), snapshot=snap, player_name="Delver")
    )

    torch = next(it for it in snap.find_creature_core("Delver").inventory.items
                 if it.get("id") == "torch")
    assert torch["quantity"] == 3, "a spare torch taken up while lit is not burned"
    assert snap.resources["light"].current == 6.0, "the burning torch is not reset"
    assert torch["equipped"] is True, "the torch is still equipped"
    assert not [s for s in capture_spans.get_finished_spans() if s.name == SPAN_LIGHT_RELIT]


# ---------------------------------------------------------------------------
# 4 — unequip of a light source never relights (only equip-in-the-dark does).
# ---------------------------------------------------------------------------


def test_unequip_torch_in_dark_does_not_relight(capture_spans):
    snap = _snap(0.0, [_torch(equipped=True, charges=3)])
    asyncio.run(run_environment_clock_dispatch(_tick_dispatch(), snapshot=snap))
    assert _darkness_statuses(snap)

    asyncio.run(
        run_equip_dispatch(
            _equip_dispatch("Torch", action="unequip"), snapshot=snap, player_name="Delver"
        )
    )

    assert snap.resources["light"].current == 0.0
    assert _darkness_statuses(snap), "taking a torch OFF does not produce light"
    assert not [s for s in capture_spans.get_finished_spans() if s.name == SPAN_LIGHT_RELIT]
