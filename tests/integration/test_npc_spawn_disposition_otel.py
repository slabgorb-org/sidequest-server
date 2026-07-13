"""Wiring for the NPC spawn-disposition OTEL event (story 72-5).

Epic 72 (NPC Identity Hardening) DEEP-DIVE: an NPC's disposition could
"materialize from nowhere" with no GM-panel trace. A narrator-invented
person must spawn *neutral* (0), and a genuine Monster Manual creature
must still spawn *hostile* (-20) — and either way the spawn value must
emit a ``npc.spawn_disposition`` ``state_transition`` so the GM panel
can verify the default fired rather than trusting the narrator's prose.

Same shape as ``test_disposition_otel_wiring.py``: drive the *real*
materialization seams (``monster_manual_inject.inject`` → ``_npc_from_patch``
→ ``green_room.admit()`` → ``emit_npc_spawn_disposition``, and
``resolve_status_target`` → ``_promote_pool_member_to_npc``), assert both the
resulting ``Npc.disposition`` and the routed watcher event. The patch-path
drives were rewritten onto the MM inject path when the legacy
``WorldStatePatch.npcs_present`` lane was removed (Green Room follow-up,
2026-07-11) — the MM inject is the production spawn path for patches now.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot
from sidequest.server.dispatch import monster_manual_inject
from sidequest.server.narration_apply import resolve_status_target
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub
from tests.integration.test_npc_manual_origin_otel import (
    _creature_encounter,
    _FakeSessionData,
    _human,
    _manual_with,
)


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
async def test_narrator_invented_npc_spawns_neutral_with_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC3/AC4: a narrator-invented pool member promoted to an ``Npc``
    spawns at disposition 0 (neutral, ADR-020) and emits a
    ``npc.spawn_disposition`` event tagged ``default_neutral`` with the
    ``pool_origin`` so the GM panel can confirm the invented NPC was not
    born hostile."""
    captured = await _setup(monkeypatch, "test-spawn-disposition-invented")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[NpcPoolMember(name="Wexley", drawn_from="narrator_invented")],
    )

    promoted = resolve_status_target(snapshot, actor_name="Wexley", turn_num=3, trigger="test")
    await asyncio.sleep(0)

    assert promoted is not None
    assert int(promoted.disposition) == 0  # neutral, not born hostile

    evt = await _wait_for_event(captured, "npc.spawn_disposition")
    assert evt["component"] == "disposition"
    assert evt["fields"]["npc_name"] == "Wexley"
    assert evt["fields"]["disposition"] == 0
    assert evt["fields"]["provenance"] == "default_neutral"
    assert evt["fields"]["is_creature"] is False
    assert evt["fields"]["pool_origin"] == "Wexley"


@pytest.mark.asyncio
async def test_creature_patch_spawns_hostile_with_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2/AC3: a genuine Monster Manual creature patch (carries a
    creature-shape field) driven through the production inject path still
    spawns hostile (-20) and emits a ``npc.spawn_disposition`` event tagged
    ``default_creature_hostile``. The fix must not neutralize intentional
    creature hostility."""
    captured = await _setup(monkeypatch, "test-spawn-disposition-creature")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
    )
    sd = _FakeSessionData(
        _manual_with(encounters=[_creature_encounter(enemy_name="Chalk Moth", tier=2, hp=6)])
    )
    monster_manual_inject.inject(
        sd,  # type: ignore[arg-type]  # duck-typed _SessionData stand-in
        snapshot,
        current_location="The Dome",
        in_combat=True,
    )
    await asyncio.sleep(0)

    spawned = next(n for n in snapshot.npcs if n.core.name == "Chalk Moth")
    assert int(spawned.disposition) == -20  # creatures stay born-hostile

    evt = await _wait_for_event(captured, "npc.spawn_disposition")
    assert evt["component"] == "disposition"
    assert evt["fields"]["npc_name"] == "Chalk Moth"
    assert evt["fields"]["disposition"] == -20
    assert evt["fields"]["provenance"] == "default_creature_hostile"
    assert evt["fields"]["is_creature"] is True


@pytest.mark.asyncio
async def test_person_patch_spawns_neutral_with_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC3: a Manual *person* patch (no creature-shape field) driven
    through the production inject path spawns neutral (0) and emits a
    ``default_neutral`` span — guarding the boundary that a person carrying
    no creature field is never dragged to -20."""
    captured = await _setup(monkeypatch, "test-spawn-disposition-person")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
    )
    sd = _FakeSessionData(_manual_with(npcs=[_human("Shopkeeper")]))
    monster_manual_inject.inject(
        sd,  # type: ignore[arg-type]  # duck-typed _SessionData stand-in
        snapshot,
        current_location="The Dome",
        in_combat=False,
    )
    await asyncio.sleep(0)

    spawned = next(n for n in snapshot.npcs if n.core.name == "Shopkeeper")
    assert int(spawned.disposition) == 0  # neutral spawn

    evt = await _wait_for_event(captured, "npc.spawn_disposition")
    assert evt["component"] == "disposition"
    assert evt["fields"]["npc_name"] == "Shopkeeper"
    assert evt["fields"]["disposition"] == 0
    assert evt["fields"]["provenance"] == "default_neutral"
    assert evt["fields"]["is_creature"] is False
