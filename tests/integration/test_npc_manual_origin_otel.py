"""Wiring + OTEL for the ``manual_origin`` provenance marker — Story 72-3.

Epic 72 (NPC Identity Hardening): a Monster-Manual-authored NPC (ADR-059)
must be attributable as such — both as a field on the canonical ``Npc`` in
``snapshot.npcs`` *and* on the OTEL span the GM panel reads, so Keith's
lie-detector can tell whether the Manual actually seeded an NPC or the
narrator improvised one with the same name. (Dev-side observability — not a
player-facing surface.)

Two things are proven here, by driving the *real* production seams rather
than asserting on source text (CLAUDE.md "No Source-Text Wiring Tests"):

1. **Wiring (AC3):** ``monster_manual_inject.inject()`` — the production
   per-turn injection path — bundles MM patches into a ``WorldStatePatch``
   and applies it. After it runs, the materialized ``snapshot.npcs`` entries
   must carry ``manual_origin=True``. This is the integration test the suite
   requires: the marker reaches canonical state through the production path,
   not just through a hand-built patch.
2. **OTEL (AC4):** the per-NPC materialization span ``npc.spawn_disposition``
   (already fired in ``_npc_from_patch``, story 72-5) carries a
   ``manual_origin`` attribute so the GM panel can attribute the NPC. A
   narrator-path patch fires the same span with ``manual_origin=False`` (E1).

Same harness shape as ``test_npc_spawn_disposition_otel.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.monster_manual import EntryState, ManualEncounter, ManualNpc, MonsterManual
from sidequest.game.session import GameSnapshot, NpcPatch, WorldStatePatch
from sidequest.game.turn import TurnManager
from sidequest.server.dispatch import monster_manual_inject
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


class _FakeSessionData:
    """Minimal stand-in for ``_SessionData`` — only ``monster_manual`` is
    touched by ``inject``. Everything else stays unset so the test fails
    loudly if the seam grows a new dependency."""

    def __init__(self, manual: MonsterManual | None) -> None:
        self.monster_manual = manual


def _manual_with(
    npcs: list[ManualNpc] | None = None, encounters: list[ManualEncounter] | None = None
) -> MonsterManual:
    return MonsterManual(
        genre="mutant_wasteland",
        world="flickering_reach",
        npcs=list(npcs or []),
        encounters=list(encounters or []),
    )


def _human(name: str) -> ManualNpc:
    return ManualNpc(
        data={"name": name, "role": "scavenger", "culture": "Scrapborn"},
        name=name,
        role="scavenger",
        culture="Scrapborn",
        state=EntryState.AVAILABLE,
    )


def _creature_encounter(*, enemy_name: str, tier: int = 2, hp: int = 9) -> ManualEncounter:
    return ManualEncounter(
        data={
            "enemies": [
                {
                    "name": enemy_name,
                    "class": "salt_burrower",
                    "tier": tier,
                    "hp": hp,
                    "role": "burrowing ambusher",
                    "morale": "steady",
                }
            ]
        },
        label=f"1x {enemy_name} (tier {tier})",
        tier=tier,
        state=EntryState.AVAILABLE,
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        characters=[],
        turn_manager=TurnManager(),
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


async def _events_for(
    captured: list[dict], field_value: str, *, timeout_s: float = 1.0
) -> list[dict]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        hits = [
            evt
            for evt in captured
            if evt.get("event_type") == "state_transition"
            and evt.get("fields", {}).get("field") == field_value
        ]
        if hits:
            return hits
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Expected state_transition with field={field_value!r} within {timeout_s}s; "
        f"captured: {[(e.get('event_type'), e.get('fields', {}).get('field')) for e in captured]}"
    )


# ---------------------------------------------------------------------------
# AC3 (wiring) + AC4 (OTEL) — production inject path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_creature_marks_manual_origin_field_and_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``monster_manual_inject.inject()`` materializes a Manual
    creature into ``snapshot.npcs`` with ``manual_origin=True`` (AC3 wiring)
    AND the ``npc.spawn_disposition`` materialization span carries
    ``manual_origin=True`` (AC4)."""
    captured = await _setup(monkeypatch, "test-manual-origin-creature")

    sd = _FakeSessionData(
        _manual_with(encounters=[_creature_encounter(enemy_name="Salt Burrower", tier=2, hp=9)])
    )
    snap = _snapshot()

    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    await asyncio.sleep(0)

    # AC3 wiring: the marker reached canonical state via the production path.
    assert count == 1
    creature = next(n for n in snap.npcs if n.core.name == "Salt Burrower")
    assert creature.manual_origin is True, (
        "MM-injected creature reached snapshot.npcs without manual_origin — "
        "the injection seam silently erased authorship"
    )

    # AC4 OTEL: the per-NPC materialization span attributes the provenance.
    events = await _events_for(captured, "npc.spawn_disposition")
    evt = next(e for e in events if e["fields"]["npc_name"] == "Salt Burrower")
    assert evt["fields"]["manual_origin"] is True


@pytest.mark.asyncio
async def test_inject_human_marks_manual_origin_field_and_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Manual *human* (no creature-shape field) is still manual-origin —
    ``manual_origin`` is orthogonal to ``is_creature``. The field and the
    span attribute must both read True even though the human spawns neutral."""
    captured = await _setup(monkeypatch, "test-manual-origin-human")

    sd = _FakeSessionData(_manual_with(npcs=[_human("Krag")]))
    snap = _snapshot()

    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    await asyncio.sleep(0)

    krag = next(n for n in snap.npcs if n.core.name == "Krag")
    assert krag.manual_origin is True
    assert int(krag.disposition) == 0  # human still spawns neutral (72-5 untouched)

    events = await _events_for(captured, "npc.spawn_disposition")
    evt = next(e for e in events if e["fields"]["npc_name"] == "Krag")
    assert evt["fields"]["manual_origin"] is True


# ---------------------------------------------------------------------------
# E1 (span level) — narrator-path patch is not manual-origin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrator_patch_span_manual_origin_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A narrator-emitted patch (no MM marker) materializes through the same
    span with ``manual_origin=False`` — the lie-detector can tell narrator
    improv from Manual authorship."""
    captured = await _setup(monkeypatch, "test-manual-origin-narrator")

    snap = _snapshot()
    snap.apply_world_patch(
        WorldStatePatch(npcs_present=[NpcPatch(name="Shopkeeper", role="merchant")])
    )
    await asyncio.sleep(0)

    keep = next(n for n in snap.npcs if n.core.name == "Shopkeeper")
    assert keep.manual_origin is False

    events = await _events_for(captured, "npc.spawn_disposition")
    evt = next(e for e in events if e["fields"]["npc_name"] == "Shopkeeper")
    assert evt["fields"]["manual_origin"] is False
