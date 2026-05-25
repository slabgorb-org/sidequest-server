"""End-to-end wiring for NPC disposition shift OTEL events.

Sprint 3 cold-subsystem audit: ``disposition.shift`` was an
``Emitter.fire`` event attached to whatever span happened to be current
when ``apply_world_patch`` ran. Span events are not visible to
``WatcherSpanProcessor`` (which only sees ``on_end`` for spans), so the
GM panel never received affinity shifts. Promotion: ``Emitter.fire`` →
``Span.open`` plus a typed ``SPAN_ROUTES`` entry routing to
``state_transition`` with ``component=disposition``.

Same shape as ``test_combat_otel_wiring.py`` and
``test_inventory_wiring.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot, Npc, WorldStatePatch
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


def _make_pc(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        char_class="Fighter",
        race="Human",
        backstory=f"{name} test",
    )


def _make_npc(name: str, disposition: int) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        disposition=disposition,
    )


async def _setup(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    return captured


async def _wait_for_event(
    captured: list[dict], field_value: str, *, timeout_s: float = 1.0
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        for evt in captured:
            if (
                evt.get("event_type") == "state_transition"
                and evt.get("fields", {}).get("field") == field_value
            ):
                return evt
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Expected state_transition with field={field_value!r} within {timeout_s}s; "
        f"captured: {[(e.get('event_type'), e.get('fields', {}).get('field')) for e in captured]}"
    )


@pytest.mark.asyncio
async def test_npc_disposition_shift_publishes_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``apply_world_patch`` with ``npc_attitudes`` must emit a typed
    ``state_transition`` event with ``component=disposition`` and
    ``field=disposition.shift`` so the GM panel can render affinity
    drift."""
    captured = await _setup(monkeypatch, "test-disposition-shift-wiring")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npcs=[_make_npc("Bartender", disposition=10)],
    )
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Bartender": 15}))
    await asyncio.sleep(0)

    # NPC mutation actually happened. ``npc.disposition`` is a
    # ``Disposition`` after Story 50-10 — unwrap via ``int()`` to assert
    # the numeric value without coupling to wrapper equality semantics.
    assert int(snapshot.npcs[0].disposition) == 25

    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["component"] == "disposition"
    assert evt["fields"]["npc_name"] == "Bartender"
    assert evt["fields"]["delta"] == 15
    assert evt["fields"]["before"] == 10
    assert evt["fields"]["after"] == 25


@pytest.mark.asyncio
async def test_disposition_clamps_at_bounds_and_emits_actual_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delta that pushes past +100 must clamp; the watcher event
    must report the clamped (actual) ``after`` so the GM panel reflects
    state, not the raw delta."""
    captured = await _setup(monkeypatch, "test-disposition-clamp-wiring")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npcs=[_make_npc("Friend", disposition=95)],
    )
    snapshot.apply_world_patch(WorldStatePatch(npc_attitudes={"Friend": 50}))
    await asyncio.sleep(0)

    assert int(snapshot.npcs[0].disposition) == 100  # clamped
    evt = await _wait_for_event(captured, "disposition.shift")
    assert evt["fields"]["before"] == 95
    assert evt["fields"]["after"] == 100
    assert evt["fields"]["delta"] == 50  # original delta, not clamped
