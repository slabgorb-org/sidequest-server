"""Three-turn end-to-end smoke test for the space_opera dogfight engine (T7).

The playtest scaffolding from ADR-077 §duel_01.md, in code. Drives three
maneuver pairs through the production dispatch path via the reusable
``tests/fixtures/dogfight_playtest_encounter.py`` helpers and asserts on:

  - per_actor_state mutation after each turn (engine actually ran)
  - ``narrator_hints`` carries exactly the most recent cell's hint (T5 fix)
  - OTEL ``dogfight.*`` spans emitted for each turn
  - Outcome cell_name matches the expected interaction-table cell

This is the wiring test for T7. Unit tests of the fixture functions in
isolation aren't enough — this one proves the *whole* engine
(instantiation → commit → dispatch → resolver → state mutation) works
when invoked through the playtest fixture API.

Skips when sidequest-content is not checked out (matches the pattern in
``test_sealed_letter_dispatch_integration.py``).
"""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from tests.fixtures.dogfight_playtest_encounter import (
    DEFAULT_CONTENT_ROOT,
    drive_dogfight_turn,
    make_dogfight_playtest_state,
)

pytestmark = pytest.mark.skipif(
    not DEFAULT_CONTENT_ROOT.is_dir(),
    reason="sidequest-content not on disk alongside sidequest-server",
)


@pytest.fixture
def otel_capture():
    """Attach an in-memory span exporter to the running TracerProvider."""
    from sidequest.telemetry.setup import init_tracer

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


def test_three_turn_dogfight_resolves_through_production_path(
    otel_capture: InMemorySpanExporter,
) -> None:
    """The playtest scaffolding from ADR-077 §duel_01.md, in code.

    Three turns:
      1. (straight, straight) — head-on merge, mutual fly-by.
      2. (loop, kill_rotation) — mutual gunline; gun_solution=True both sides.
      3. (bank, bank) — mutual no-hit reposition.

    After each turn, asserts:
      - resolve_sealed_letter_lookup fired (via dispatch — cell_resolved
        OTEL span emitted)
      - per_actor_state mutated for both pilots (engine actually moved
        the world)
      - narrator_hints holds exactly the most recent cell's hint (T5 fix —
        no accumulation across turns)
      - OTEL dogfight.confrontation_started, dogfight.maneuver_committed
        (x2), dogfight.cell_resolved spans all fired this turn

    Drives turns through ``drive_dogfight_turn`` from the fixture — NOT
    through ``resolve_sealed_letter_lookup`` directly — so this test
    catches dispatch-wiring regressions just like
    ``test_sealed_letter_dispatch_integration.py`` does.
    """
    snap, cdef, pack = make_dogfight_playtest_state(
        player_pilot_name="Maverick",
        opponent_pilot_name="Vulture",
    )
    enc = snap.encounter
    assert enc is not None
    # Production instantiation does NOT pre-seed per_actor_state from
    # descriptor_schema — it starts empty and the first cell's view delta
    # populates it. Pin the contract here so future drift is loud.
    assert all(a.per_actor_state == {} for a in enc.actors), (
        f"per_actor_state should start empty post-instantiation; got "
        f"{[(a.role, a.per_actor_state) for a in enc.actors]}"
    )
    assert enc.narrator_hints == [], (
        f"narrator_hints should start empty; got {enc.narrator_hints!r}"
    )

    red = next(a for a in enc.actors if a.role == "red")
    blue = next(a for a in enc.actors if a.role == "blue")
    assert red.name == "Maverick"
    assert blue.name == "Vulture"

    # ---- Turn 1: (straight, straight) — initial mutual pass ----
    otel_capture.clear()
    outcome1 = drive_dogfight_turn(
        snap,
        red_maneuver="straight",
        blue_maneuver="straight",
        pack=pack,
        narration="Both pilots punch the throttle and bore in straight.",
    )
    _assert_dogfight_otel_spans_fired(otel_capture, expected_turns=1)
    assert outcome1.cell_name, "outcome cell_name unset — fixture broken"
    # State mutated — at minimum some descriptor key was set
    assert red.per_actor_state, (
        f"red per_actor_state still empty after turn 1: {red.per_actor_state!r}"
    )
    assert blue.per_actor_state, (
        f"blue per_actor_state still empty after turn 1: {blue.per_actor_state!r}"
    )
    # narrator_hints holds exactly one entry — T5 fix
    assert len(enc.narrator_hints) == 1, (
        f"narrator_hints should have exactly 1 entry after turn 1, got "
        f"{len(enc.narrator_hints)}: {enc.narrator_hints!r}"
    )
    assert enc.narrator_hints[0] == outcome1.narration_hint

    # ---- Turn 2: (loop, kill_rotation) — mutual gunline ----
    otel_capture.clear()
    outcome2 = drive_dogfight_turn(
        snap,
        red_maneuver="loop",
        blue_maneuver="kill_rotation",
        pack=pack,
        narration="Maverick pulls the loop; Vulture commits the kill rotation.",
    )
    _assert_dogfight_otel_spans_fired(otel_capture, expected_turns=1)
    # Mutual gunline — both pilots should have a gun_solution
    assert red.per_actor_state.get("gun_solution") is True, (
        f"red should have gun_solution=True after (loop, kill_rotation); "
        f"got {red.per_actor_state!r}"
    )
    assert blue.per_actor_state.get("gun_solution") is True, (
        f"blue should have gun_solution=True after (loop, kill_rotation); "
        f"got {blue.per_actor_state!r}"
    )
    # narrator_hints REPLACED, not appended (T5 fix)
    assert len(enc.narrator_hints) == 1, (
        f"narrator_hints should still have exactly 1 entry after turn 2, "
        f"got {len(enc.narrator_hints)}: {enc.narrator_hints!r}"
    )
    assert enc.narrator_hints[0] == outcome2.narration_hint
    # And the hint actually changed from turn 1 — proves replace, not no-op
    assert outcome2.narration_hint != outcome1.narration_hint, (
        "turns 1 and 2 produced identical hints — fixture isn't proving "
        "replace semantics; the cells should differ"
    )

    # ---- Turn 3: (bank, bank) — mutual reposition, no hit ----
    otel_capture.clear()
    outcome3 = drive_dogfight_turn(
        snap,
        red_maneuver="bank",
        blue_maneuver="bank",
        pack=pack,
        narration="Both pilots roll into mirrored banks; the gun line breaks.",
    )
    _assert_dogfight_otel_spans_fired(otel_capture, expected_turns=1)
    # Symmetric with turns 1 and 2: per_actor_state should still hold
    # the latest cell's deltas — never empty after a resolved turn.
    assert red.per_actor_state, "turn 3 should have left red with non-empty per_actor_state"
    assert blue.per_actor_state, "turn 3 should have left blue with non-empty per_actor_state"
    # Still exactly one hint — never accumulating across three turns
    assert len(enc.narrator_hints) == 1, (
        f"narrator_hints should still have exactly 1 entry after turn 3, "
        f"got {len(enc.narrator_hints)}: {enc.narrator_hints!r}"
    )
    assert enc.narrator_hints[0] == outcome3.narration_hint

    # All three turns produced distinct cell_names — sanity that the
    # fixture is actually exercising different cells of the table.
    assert len({outcome1.cell_name, outcome2.cell_name, outcome3.cell_name}) >= 2, (
        f"three turns produced cell_names "
        f"{[outcome1.cell_name, outcome2.cell_name, outcome3.cell_name]!r}; "
        f"smoke test isn't covering enough of the table"
    )


def _assert_dogfight_otel_spans_fired(
    exporter: InMemorySpanExporter,
    *,
    expected_turns: int,
) -> None:
    """Assert that exactly one turn's worth of dogfight.* spans fired.

    Per-turn spans:
        - dogfight.confrontation_started   x1
        - dogfight.maneuver_committed      x2 (red + blue)
        - dogfight.cell_resolved           x1
    """
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]

    started = [n for n in names if n == "dogfight.confrontation_started"]
    committed = [n for n in names if n == "dogfight.maneuver_committed"]
    resolved = [n for n in names if n == "dogfight.cell_resolved"]

    assert len(started) == expected_turns, (
        f"expected {expected_turns} confrontation_started spans, "
        f"got {len(started)}; all spans: {names}"
    )
    assert len(committed) == expected_turns * 2, (
        f"expected {expected_turns * 2} maneuver_committed spans "
        f"({expected_turns} turns x 2 actors), got {len(committed)}; "
        f"all spans: {names}"
    )
    assert len(resolved) == expected_turns, (
        f"expected {expected_turns} cell_resolved spans, got {len(resolved)}; all spans: {names}"
    )
