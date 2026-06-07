"""Task 15 — end-to-end OTEL span ordering proof for the SWN dogfight engine.

THE CORE ASSERTION: the span sequence fires IN ORDER across the full
instantiate → commit → cell_resolve → shot_defer → DICE_THROW → shot_resolve
→ hp_depletion flow. Individual spans are already asserted by the per-piece
tests (Tasks 13/14); what THIS test adds is the ordered span proof — the GM
panel "lie detector" over the whole production path.

Two tests:
  1. ``test_otel_span_ordering_instantiate_to_shot``:
     Drives a mutual-gunline turn (loop/kill_rotation), simulates the player's
     DICE_THROW with a monkeypatched high d20 + damage, then asserts the span
     sequence fires in the required order:
       confrontation_started ≺ maneuver_committed ≺ cell_resolved
                             ≺ shot_attempted ≺ shot_damage

  2. ``test_otel_span_ordering_includes_encounter_resolved_on_depletion``:
     Same flow but with damage high enough to deplete the opponent's frame HP
     to 0 in one shot. Asserts ``encounter.resolved`` (source=hp_depletion)
     fires AFTER ``shot_attempted`` and ``shot_damage``.

Reuses:
  - ``make_dogfight_playtest_state`` from tests/fixtures/dogfight_playtest_encounter.py
    (production-path instantiation with a seeded player Character, real content).
  - OTEL capture fixture pattern from test_dogfight_playtest_smoke.py.
  - ``_roll_d20_server_side`` + ``_roll_damage_dice`` monkeypatches from
    test_dogfight_shot_wiring.py (Tasks 13/14).

Skips when ``sidequest-content`` is not checked out alongside the server repo
(same sentinel as every other integration test in this suite).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fixtures.dogfight_playtest_encounter import (
    DEFAULT_CONTENT_ROOT,
    make_dogfight_playtest_state,
)

pytestmark = [
    pytest.mark.skipif(
        not DEFAULT_CONTENT_ROOT.is_dir(),
        reason="sidequest-content not on disk alongside sidequest-server",
    ),
    pytest.mark.skip(
        reason="content-coupled: references dogfight weapon 'multifocal_laser' that "
        "migrated to world-tier inventory (epic 94); rewrite against fixtures — story 94-4"
    ),
]

# Maneuver pair that yields a mutual gunline (both pilots get a gun solution).
# Confirmed in test_dogfight_shot_wiring.py and test_dogfight_playtest_smoke.py.
_GUN_SOLUTION_RED_MANEUVER = "loop"
_GUN_SOLUTION_BLUE_MANEUVER = "kill_rotation"

# frame_hp = 8 (per space_opera rules.yaml opponent/player_default_stats).
_EXPECTED_FRAME_HP = 8

# Damage needed to one-shot the opponent: 9 > 8. multifocal_laser has
# armor_piercing=20 which fully negates armor=5, so effective armor=0 and
# all damage applies. 9 damage > 8 HP => frame_hp hits 0.
_ONE_SHOT_DAMAGE = 9


# ---------------------------------------------------------------------------
# OTEL capture fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture():
    """Attach an in-memory exporter to the running TracerProvider."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _idx(names: list[str], suffix: str) -> int:
    """Return the index of the FIRST span name ending with ``suffix``.

    Raises AssertionError with a clear message if the span is absent —
    making the ordering assertion fail loudly rather than producing a
    confusing ``StopIteration``.
    """
    for i, name in enumerate(names):
        if name.endswith(suffix):
            return i
    raise AssertionError(
        f"Expected a span ending with {suffix!r} in the OTEL trace but none fired.\n"
        f"Spans captured: {names}"
    )


def _drive_gunline_turn_and_resolve(
    otel_capture: InMemorySpanExporter,
    *,
    npc_d20: int = 1,  # NPC d20 held at narration time (server-rolled)
    player_d20_face: int = 20,  # player face delivered at "DICE_THROW" time
    damage: int = 3,  # monkeypatched _roll_damage_dice return
) -> tuple[object, object, object]:
    """Build a dogfight, drive one gunline turn, simulate the DICE_THROW.

    Returns (encounter, red_actor, blue_actor) with per_actor_state mutated
    and all OTEL spans captured in ``otel_capture``.

    Exactly mirrors the pattern in test_dogfight_shot_wiring.py (Task 13),
    but expressed as a shared helper so both ordering tests share the same
    flow without duplication.
    """
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
    from sidequest.game.dogfight_shot import (
        frame_hp_resolver,
        resolve_dogfight_shots,
    )
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    snap, _cdef, pack = make_dogfight_playtest_state(
        player_pilot_name="Maverick",
        opponent_pilot_name="Vulture",
    )
    enc = snap.encounter
    assert enc is not None

    red = next(a for a in enc.actors if a.role == "red")
    blue = next(a for a in enc.actors if a.role == "blue")

    # --- Precondition: frame_hp seeded at instantiation ---
    assert red.per_actor_state.get("frame_hp") == _EXPECTED_FRAME_HP, (
        f"red frame_hp should be {_EXPECTED_FRAME_HP} at instantiation; "
        f"got {red.per_actor_state.get('frame_hp')!r}"
    )
    assert blue.per_actor_state.get("frame_hp") == _EXPECTED_FRAME_HP, (
        f"blue frame_hp should be {_EXPECTED_FRAME_HP} at instantiation; "
        f"got {blue.per_actor_state.get('frame_hp')!r}"
    )

    otel_capture.clear()

    # --- Narration turn: loop/kill_rotation → mutual gunline, player deferred ---
    with (
        patch(
            "sidequest.server.narration_apply._roll_d20_server_side",
            return_value=npc_d20,
        ),
        patch(
            "sidequest.game.dogfight_shot._roll_damage_dice",
            return_value=damage,
        ),
    ):
        applied = _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="Maverick pulls the loop; Vulture commits the kill rotation.",
                beat_selections=[
                    BeatSelection(actor=red.name, beat_id=_GUN_SOLUTION_RED_MANEUVER),
                    BeatSelection(actor=blue.name, beat_id=_GUN_SOLUTION_BLUE_MANEUVER),
                ],
            ),
            player_name=red.name,
            pack=pack,
            room=room_for(snap),
        )

    # Precondition: the stash is set (mutual gunline → player deferred).
    from sidequest.game.dogfight_shot import PendingDogfightShot

    assert isinstance(applied.pending_dogfight_shot, PendingDogfightShot), (
        "loop/kill_rotation must yield a deferred player shot; precondition failed — "
        "content changed or player-shot deferral path broken"
    )

    # --- Simulate DICE_THROW: resolve all shots ---
    pending = applied.pending_dogfight_shot
    d20_by_shooter = {pending.player_shooter_role: player_d20_face, **pending.held_npc_d20s}

    with patch("sidequest.game.dogfight_shot._roll_damage_dice", return_value=damage):
        resolve_dogfight_shots(
            encounter=enc,
            gun_solutions=pending.gun_solutions,
            d20_by_shooter=d20_by_shooter,
            edge_resolver=frame_hp_resolver(enc),
        )

    return enc, red, blue


# ---------------------------------------------------------------------------
# Test 1: ordered span sequence from confrontation_started through shot_damage
# ---------------------------------------------------------------------------


def test_otel_span_ordering_instantiate_to_shot(
    otel_capture: InMemorySpanExporter,
) -> None:
    """THE CORE ASSERTION: the dogfight OTEL spans fire in the required order.

    Drives the full production path:
      instantiate → loop/kill_rotation maneuver commit → cell resolve
      → player-shot stash (Task 14 deferral) → DICE_THROW resolution

    Captures ALL spans across instantiation + narration + DICE_THROW simulation
    and asserts the ordered sequence:

      dogfight.confrontation_started
        ≺ dogfight.maneuver_committed  (x2, red then blue)
        ≺ dogfight.cell_resolved
        ≺ dogfight.shot_attempted      (post-DICE_THROW, player d20=20 → hit)
        ≺ dogfight.shot_damage         (player hit → damage applied)

    This ordering is the engine's behavioral contract: cell resolution
    (sealed-letter table) MUST precede shot resolution (dice), and shot
    resolution MUST emit shot_damage only after shot_attempted confirms a hit.
    A failure here means a span fired out of sequence — the engine's execution
    order regressed regardless of whether individual per-piece tests pass.

    New value over per-piece tests: ordering ACROSS the full flow. The smoke
    test (T7) checks span presence per-turn but doesn't capture across the
    DICE_THROW boundary; Tests 13/14 check individual phases. This test is the
    only one that asserts the complete instantiate-to-shot ordering.
    """
    enc, red, blue = _drive_gunline_turn_and_resolve(
        otel_capture,
        npc_d20=1,  # NPC misses → only player hit matters for damage assertion
        player_d20_face=20,  # player auto-hit
        damage=3,  # 3 damage, armor negated by AP=20, so 3 applied → HP = 8-3 = 5
    )

    names = [s.name for s in otel_capture.get_finished_spans()]

    # --- Core ordering assertion ---
    # Must find each required span; _idx raises AssertionError with names if absent.
    i_started = _idx(names, "confrontation_started")
    i_committed = _idx(names, "maneuver_committed")
    i_cell = _idx(names, "cell_resolved")
    i_attempted = _idx(names, "shot_attempted")
    i_damage = _idx(names, "shot_damage")

    assert i_started < i_committed, (
        f"confrontation_started (idx={i_started}) must precede maneuver_committed "
        f"(idx={i_committed}); span order: {names}"
    )
    assert i_committed < i_cell, (
        f"maneuver_committed (idx={i_committed}) must precede cell_resolved "
        f"(idx={i_cell}); span order: {names}"
    )
    assert i_cell < i_attempted, (
        f"cell_resolved (idx={i_cell}) must precede shot_attempted (idx={i_attempted}); "
        f"span order: {names}\n"
        "FAILURE = shot resolution fired BEFORE the sealed-letter cell resolved — "
        "the engine's instantiate→commit→resolve→shot ordering is broken"
    )
    assert i_attempted < i_damage, (
        f"shot_attempted (idx={i_attempted}) must precede shot_damage (idx={i_damage}); "
        f"span order: {names}\n"
        "FAILURE = damage span fired without a preceding shot_attempted — "
        "the two-pass shot resolution (hit-check then damage) is broken"
    )

    # --- Sanity: player shot landed (d20=20 auto-hit, all damage applies) ---
    blue_hp_after = int(blue.per_actor_state["frame_hp"])
    assert blue_hp_after < _EXPECTED_FRAME_HP, (
        f"opponent frame_hp must drop after player d20=20 auto-hit; "
        f"expected < {_EXPECTED_FRAME_HP}, got {blue_hp_after}. "
        f"If this fails the shot-resolution path itself is broken, not the ordering."
    )

    # --- Sanity: at least one maneuver_committed span per actor ---
    committed_spans = [n for n in names if n == "dogfight.maneuver_committed"]
    assert len(committed_spans) >= 2, (
        f"expected ≥2 maneuver_committed spans (one per actor); got {len(committed_spans)}: {names}"
    )


# ---------------------------------------------------------------------------
# Test 2: encounter.resolved fires AFTER shot spans on HP depletion
# ---------------------------------------------------------------------------


def test_otel_span_ordering_includes_encounter_resolved_on_depletion(
    otel_capture: InMemorySpanExporter,
) -> None:
    """``encounter.resolved`` (source=hp_depletion) fires AFTER shot spans.

    Drives the same production path as Test 1, but monkeypatches damage to
    _ONE_SHOT_DAMAGE (9) which depletes the opponent's 8-HP frame to 0 in a
    single hit. The depletion check inside ``resolve_dogfight_shots`` → calls
    ``check_hp_depletion`` which emits ``encounter.resolved`` with
    source="hp_depletion".

    Core assertion (the new ordering proof over per-piece tests):
      shot_attempted ≺ shot_damage ≺ encounter.resolved

    This guarantees the engine doesn't prematurely end the encounter (firing
    encounter.resolved before the shot was actually resolved), AND proves the
    depletion-path OTEL wiring spans the full loop — not just "the span
    exists somewhere" but "it fires in the correct causal position."

    Attributes checked on the encounter.resolved span:
      - source = "hp_depletion"  (HP kill, not dial-based resolution)
      - encounter_type = "dogfight"
    """
    enc, red, blue = _drive_gunline_turn_and_resolve(
        otel_capture,
        npc_d20=1,  # NPC auto-miss so NPC doesn't also kill the player
        player_d20_face=20,  # player auto-hit
        damage=_ONE_SHOT_DAMAGE,  # 9 > frame_hp=8 → depletion
    )

    names = [s.name for s in otel_capture.get_finished_spans()]
    spans_by_name = {s.name: s for s in otel_capture.get_finished_spans()}

    # --- Core ordering assertions ---
    i_attempted = _idx(names, "shot_attempted")
    i_damage = _idx(names, "shot_damage")
    i_resolved = _idx(names, "encounter.resolved")

    assert i_attempted < i_damage, (
        f"shot_attempted (idx={i_attempted}) must precede shot_damage (idx={i_damage}); "
        f"spans: {names}"
    )
    assert i_damage < i_resolved, (
        f"shot_damage (idx={i_damage}) must precede encounter.resolved (idx={i_resolved}); "
        f"spans: {names}\n"
        "FAILURE = encounter resolved BEFORE (or concurrent with) the killing shot — "
        "check_hp_depletion fired out of sequence in resolve_dogfight_shots"
    )

    # --- Attribute check: source must be hp_depletion ---
    resolved_span = spans_by_name.get("encounter.resolved")
    assert resolved_span is not None  # already guaranteed by _idx above
    attrs = resolved_span.attributes or {}
    assert attrs.get("source") == "hp_depletion", (
        f"encounter.resolved must carry source='hp_depletion'; "
        f"got source={attrs.get('source')!r}. Full attrs: {dict(attrs)}"
    )
    assert attrs.get("encounter_type") == "dogfight", (
        f"encounter.resolved must carry encounter_type='dogfight'; "
        f"got encounter_type={attrs.get('encounter_type')!r}"
    )

    # --- Depletion effect: blue frame_hp must be 0 ---
    blue_hp_after = int(blue.per_actor_state["frame_hp"])
    assert blue_hp_after == 0, (
        f"opponent frame_hp must be 0 after {_ONE_SHOT_DAMAGE}-damage one-shot; got {blue_hp_after}"
    )

    # --- Encounter resolved flag set ---
    assert enc.resolved is True, (
        "enc.resolved must be True after HP depletion; "
        f"got {enc.resolved!r} — check_hp_depletion not mutating encounter state"
    )

    # --- The full ordering chain: started ≺ committed ≺ cell ≺ attempted ≺ damage ≺ resolved ---
    i_started = _idx(names, "confrontation_started")
    i_committed = _idx(names, "maneuver_committed")
    i_cell = _idx(names, "cell_resolved")

    assert i_started < i_committed < i_cell < i_attempted < i_damage < i_resolved, (
        f"Full span chain out of order.\n"
        f"  confrontation_started : {i_started}\n"
        f"  maneuver_committed    : {i_committed}\n"
        f"  cell_resolved         : {i_cell}\n"
        f"  shot_attempted        : {i_attempted}\n"
        f"  shot_damage           : {i_damage}\n"
        f"  encounter.resolved    : {i_resolved}\n"
        f"All spans: {names}"
    )
