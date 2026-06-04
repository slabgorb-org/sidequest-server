"""End-to-end wiring for the milestone → level-up engine (ADR-021 track 1).

Story 82-6. ``award_turn_xp`` already accumulates ``core.xp`` every turn and
emits a ``component=progression`` watcher event — but nothing ever drives a
*level-up* from that accumulation, so ``core.level`` is frozen at 1 forever.
This is the gap: a runtime engine (``apply_level_ups``) that consumes the
live accumulator, bumps the level when a milestone threshold is crossed, and
emits an OTEL/watcher event the GM panel can read (mirroring
``SPAN_DISPOSITION_SHIFT`` / the ``award_turn_xp`` progression emit).

Same harness shape as ``tests/integration/test_disposition_otel_wiring.py``.

RED: ``apply_level_ups`` / ``LevelUp`` do not exist yet — this module fails
to import on current ``develop``.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.progression import ProgressionConfig
from sidequest.server.dispatch.encounter_lifecycle import LevelUp, apply_level_ups
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub

# Field name on the progression state_transition watcher event. Mirrors
# ``disposition.shift`` (component=disposition) and the existing
# ``award_turn_xp`` emit (component=progression).
LEVEL_UP_FIELD = "progression.level_up"


def _make_pc(name: str, *, xp: int = 0, level: int = 1) -> Character:
    core = CreatureCore(
        name=name,
        description="x",
        personality="x",
        inventory=Inventory(),
        hp=HpPool(current=10, max=10, base_max=10),
        xp=xp,
        level=level,
    )
    return Character(core=core, char_class="Fighter", race="Human", backstory=f"{name}")


def _progression(*, per_level: int, max_level: int) -> ProgressionConfig:
    return ProgressionConfig(
        milestone_categories=["combat"],
        milestones_per_level=per_level,
        max_level=max_level,
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
async def test_level_up_mutates_level_and_publishes_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A character with accumulation past the ceiling levels up and the
    engine emits a typed ``state_transition`` with ``component=progression``
    and ``field=progression.level_up`` carrying the before/after levels, so
    the GM panel can confirm the subsystem engaged."""
    captured = await _setup(monkeypatch, "test-levelup-wiring")

    # Seed accumulation far past any sane threshold so the result caps at
    # max_level regardless of the engine's xp→milestone conversion ratio.
    pc = _make_pc("Rux", xp=100_000, level=1)
    snapshot = GameSnapshot(genre_slug="caverns_and_claudes", characters=[pc])

    apply_level_ups(snapshot, _progression(per_level=3, max_level=5))
    await asyncio.sleep(0)

    assert snapshot.characters[0].core.level == 5, "must clamp to max_level"

    evt = await _wait_for_event(captured, LEVEL_UP_FIELD)
    assert evt["component"] == "progression"
    assert evt["fields"]["character_name"] == "Rux"
    assert evt["fields"]["before"] == 1
    assert evt["fields"]["after"] == 5


@pytest.mark.asyncio
async def test_level_up_returns_player_facing_delta_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 (data contract): the engine returns a structured advancement
    delta — character, before, after, and the driver — so a player-facing
    surface can render *what changed and why*, not just a silent stat bump.
    Distinct from the GM/OTEL emit above."""
    await _setup(monkeypatch, "test-levelup-delta")

    pc = _make_pc("Rux", xp=100_000, level=1)
    snapshot = GameSnapshot(genre_slug="caverns_and_claudes", characters=[pc])

    deltas = apply_level_ups(snapshot, _progression(per_level=3, max_level=5))

    assert deltas, "a crossing must return at least one LevelUp delta"
    delta = deltas[0]
    assert isinstance(delta, LevelUp)
    assert delta.character_name == "Rux"
    assert delta.before == 1
    assert delta.after == 5
    assert delta.before < delta.after
    assert delta.driver, "delta must name its driver (e.g. 'milestone')"


@pytest.mark.asyncio
async def test_no_crossing_is_silent_no_level_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A character below the first threshold must NOT level up and must NOT
    emit a level-up event — no phantom advancement (the lie-detector must
    only fire on a real crossing)."""
    captured = await _setup(monkeypatch, "test-levelup-noop")

    pc = _make_pc("Rux", xp=0, level=1)
    snapshot = GameSnapshot(genre_slug="caverns_and_claudes", characters=[pc])

    deltas = apply_level_ups(snapshot, _progression(per_level=3, max_level=5))
    await asyncio.sleep(0.05)

    assert snapshot.characters[0].core.level == 1
    assert deltas == []
    level_up_events = [e for e in captured if e.get("fields", {}).get("field") == LEVEL_UP_FIELD]
    assert level_up_events == [], f"unexpected level-up event: {level_up_events}"


@pytest.mark.asyncio
async def test_unconfigured_progression_never_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pack that doesn't author progression (per_level==0, the default)
    must no-op cleanly even with huge accumulation — No Silent Fallbacks /
    no ZeroDivisionError, no phantom level-up."""
    captured = await _setup(monkeypatch, "test-levelup-unconfigured")

    pc = _make_pc("Rux", xp=100_000, level=1)
    snapshot = GameSnapshot(genre_slug="caverns_and_claudes", characters=[pc])

    deltas = apply_level_ups(snapshot, _progression(per_level=0, max_level=0))
    await asyncio.sleep(0.05)

    assert snapshot.characters[0].core.level == 1
    assert deltas == []
    assert not [e for e in captured if e.get("fields", {}).get("field") == LEVEL_UP_FIELD]
