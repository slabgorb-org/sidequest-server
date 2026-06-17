"""End-to-end wiring for the advance_encounter_beat OTEL watcher event.

Story 71-28 (OTEL-coverage split): the ``advance_encounter_beat`` WRITE
tool sets ``tool.encounter.beat_from`` / ``beat_to`` / ``reason`` on its
dispatch span (``tool.write.advance_encounter_beat``), and the raw span
closes through the watcher — but there is **no** ``SPAN_ROUTES`` entry for
that span name, so the translator emits only ``agent_span_close`` and the
GM panel's ``state_transition`` tab never sees a beat advance. The GM is
blind to whether encounter beats are advancing or stuck (the lie-detector
has a hole exactly where a subsystem decision happens).

The gap is invisible to ``test_routing_completeness.py`` because the
dispatch span name is *constructed dynamically* in ``tool_dispatch_span``
(``tool.{cat}.{name}``) — it is not a module-level ``SPAN_*`` constant, so
the static lint never required a routing decision for it.

These tests pin the production path the GREEN phase must complete:
``default_registry.dispatch`` opens the dispatch span, the tool sets its
attributes, ``WatcherSpanProcessor`` translates it through
``SPAN_ROUTES[tool.write.advance_encounter_beat]``, and the hub publishes a
typed ``state_transition`` with ``component=encounter`` carrying the beat
transition.

Same harness as ``test_combat_otel_wiring.py`` — the shared
``watcher_setup`` + ``wait_for_state_transition`` from
``tests/integration/conftest.py`` (story 71-34): a local ``TracerProvider``
+ ``WatcherSpanProcessor`` with ``spans_module.tracer`` monkeypatched so the
dispatch span (which resolves its tracer via ``Span.open`` →
``spans.tracer()``) lands on the test's processor.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import (
    advance_encounter_beat as _advance_encounter_beat_module,  # noqa: F401  (force tool registration)
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterMetric, StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.telemetry.spans import SPAN_ROUTES
from tests.integration.conftest import wait_for_state_transition, watcher_setup

_DISPATCH_SPAN = "tool.write.advance_encounter_beat"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _character(name: str) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(core=core, backstory="bs", char_class="Delver", race="Human")


def _encounter(*, beat: int = 2, encounter_type: str = "brawl") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=EncounterMetric(name="momentum", current=2, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=1, threshold=10),
        beat=beat,
    )


def _snapshot(encounter: StructuredEncounter | None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=[_character("Alice")],
        npcs=[],
        encounter=encounter,
    )


class _FakeRepo:
    """Minimal SaveRepository stand-in: the tool loads the snapshot to read
    the encounter and saves it back. Persistence is not the system under
    test here (the span-routing pipeline is) — the combat wiring test uses
    no repository at all. This supplies the encounter and absorbs the save."""

    def __init__(self, snapshot: GameSnapshot) -> None:
        self._snapshot = snapshot

    def load(self) -> SimpleNamespace:
        return SimpleNamespace(snapshot=self._snapshot)

    def save(self, snapshot: GameSnapshot) -> None:
        self._snapshot = snapshot


def _ctx(repo: _FakeRepo) -> ToolContext:
    # otel_span is a placeholder — Registry.dispatch swaps in the real
    # dispatch span via dataclasses.replace before calling the handler.
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Alice",
        turn_number=1,
        repository=repo,  # type: ignore[arg-type]
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


# ---------------------------------------------------------------------------
# Watcher harness — shared via tests/integration/conftest.py (story 71-34)
# ---------------------------------------------------------------------------


async def _wait_for_beat_event(captured: list[dict], *, timeout_s: float = 1.0) -> dict:
    """Poll for the typed beat-advance event by its *behavioral* signature —
    a ``state_transition`` whose ``fields`` carry ``beat_from`` and
    ``beat_to``. Matching on payload content (not a magic ``field`` label)
    keeps the test decoupled from whatever discriminator string the GREEN
    phase chooses. Thin wrapper over the shared ``wait_for_state_transition``."""
    return await wait_for_state_transition(
        captured,
        lambda evt: "beat_from" in evt.get("fields", {}) and "beat_to" in evt.get("fields", {}),
        timeout_s=timeout_s,
        describe="carrying beat_from/beat_to",
    )


async def _dispatch(ctx: ToolContext, arguments: dict) -> None:
    await default_registry.dispatch(
        ToolUseBlock(id="t1", name="advance_encounter_beat", arguments=arguments),
        ctx,
    )
    # Hub broadcast hops through run_coroutine_threadsafe — yield so the
    # queued publish coroutine drains before we poll.
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# AC1 — a routing decision exists for the dispatch span (runtime registry,
# not source-text). This is the "registry dispatch" wiring pattern blessed
# by CLAUDE.md, and a tripwire that fails the instant the route is missing.
# ---------------------------------------------------------------------------


def test_advance_encounter_beat_dispatch_span_is_routed() -> None:
    """``tool.write.advance_encounter_beat`` must have a SPAN_ROUTES entry so
    the watcher emits a typed event instead of bare agent_span_close."""
    assert _DISPATCH_SPAN in SPAN_ROUTES, (
        f"{_DISPATCH_SPAN!r} has no routing decision — GM panel is blind to "
        "encounter-beat advances. Add a SPAN_ROUTES entry."
    )
    route = SPAN_ROUTES[_DISPATCH_SPAN]
    assert route.event_type == "state_transition"
    assert route.component == "encounter"


# ---------------------------------------------------------------------------
# AC3 — the load-bearing wiring test: full path tool call → dispatch span →
# WatcherSpanProcessor → SPAN_ROUTES extraction → hub publish.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_encounter_beat_publishes_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-advance (+1) from beat 2 must reach the hub as a typed
    state_transition carrying the beat transition and encounter context."""
    captured = await watcher_setup(monkeypatch, "test-beat-advance-wiring")
    ctx = _ctx(_FakeRepo(_snapshot(_encounter(beat=2, encounter_type="brawl"))))

    await _dispatch(ctx, {"reason": "scene shifts"})

    evt = await _wait_for_beat_event(captured)
    assert evt["component"] == "encounter"
    assert evt["event_type"] == "state_transition"
    fields = evt["fields"]
    assert fields["beat_from"] == 2
    assert fields["beat_to"] == 3
    assert fields["reason"] == "scene shifts"
    # AC3: the extracted event must carry non-null encounter context. The
    # value is already in scope in the tool (it returns encounter_type in its
    # payload); GREEN must also set it as a span attribute for the route to
    # extract.
    assert fields["encounter_type"] == "brawl"


# ---------------------------------------------------------------------------
# Paranoia — explicit to_beat must be reflected verbatim, not auto-+1.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_to_beat_reflected_in_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``to_beat`` jump (2 → 7) must surface as beat_from=2,
    beat_to=7 in the typed event — proving the route reads the tool's
    after-mutation attributes, not a stale or hard-coded delta."""
    captured = await watcher_setup(monkeypatch, "test-beat-advance-explicit")
    ctx = _ctx(_FakeRepo(_snapshot(_encounter(beat=2, encounter_type="duel"))))

    await _dispatch(ctx, {"to_beat": 7, "reason": "smash cut"})

    evt = await _wait_for_beat_event(captured)
    fields = evt["fields"]
    assert fields["beat_from"] == 2
    assert fields["beat_to"] == 7
    assert fields["encounter_type"] == "duel"
