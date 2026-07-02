"""ADR-153 §3 state graph — the state machine through the PRODUCTION apply
path (158-40, AC-5 + AC-8 wiring).

These tests drive ``_apply_narration_result_to_snapshot`` (not the resolver
directly), on a tmp_path state-graph rewrite of ``swn_test_pack`` (see
``tests/_helpers/state_graph_fixture.py``): merge + tail_chase registered,
merge ``[straight, loop]`` → ``next_state: tail_chase``, tail_chase
``[loop, loop]`` rewritten to the extend-and-return trigger.

Coverage:
  - instantiation stamps the entry ``dogfight_state`` (plain fixture pack —
    back-compat single-table def must stamp too)
  - a turn resolves against the CURRENT state's table and advances
    ``dogfight_state`` per the cell's ``next_state``; the NEXT turn provably
    resolves from the new state's table (distinguishing narration hint)
  - extend-and-return walks the graph back to merge
  - ``dogfight.state_transition`` OTEL spans fire with from/to attributes,
    in walk order
  - a cell transitioning to a state with no registered table fails LOUD at
    the apply seam (CLAUDE.md No Silent Fallbacks — never silently stay)

RED: the state-graph pack cannot even load (``interaction_tables`` /
``next_state`` rejected by ``extra="forbid"``), and the plain-pack
instantiation test fails on the missing ``dogfight_state`` field. Every
failure message points at the Plan 4 Task 1-5 surfaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.fixture_packs import SWN_TEST_PACK, TEST_WORLD, load_fixture_pack
from tests._helpers.session_room import room_for
from tests._helpers.state_graph_fixture import make_state_graph_pack
from tests._helpers.trigger_encounter import trigger_encounter

STATE_TRANSITION_SPAN = "dogfight.state_transition"


def _make_pilot(name: str) -> Character:
    """Minimal SWN PC (mirrors test_sealed_letter_dispatch_integration) —
    Reflex/Intellect 10 so to-hit arithmetic is deterministic."""
    return Character(
        core=CreatureCore(name=name, description="Test pilot.", personality="Calm."),
        backstory="A pilot.",
        char_class="Pilot",
        race="Human",
        stats={"Reflex": 10, "Intellect": 10},
    )


def _graph_snap(tmp_path: Path, **builder_kwargs: str) -> tuple[GameSnapshot, GenrePack]:
    pack = load_genre_pack(make_state_graph_pack(tmp_path, **builder_kwargs))
    snap = GameSnapshot(genre=SWN_TEST_PACK)
    snap.genre_slug = SWN_TEST_PACK
    snap.world_slug = TEST_WORLD
    snap.characters = [_make_pilot("Vega")]
    return snap, pack


def _seat_dogfight(snap: GameSnapshot, pack: GenrePack) -> None:
    trigger_encounter(
        snap,
        pack,
        "dogfight",
        "Vega",
        npcs_present=[NpcMention(name="Iron Fang", role="ace", side="opponent")],
    )
    assert snap.encounter is not None


def _drive_turn(snap: GameSnapshot, pack: GenrePack, red: str, blue: str) -> None:
    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration=f"Vega flies {red}; Iron Fang answers with {blue}.",
            beat_selections=[
                BeatSelection(actor="Vega", beat_id=red, outcome=RollOutcome.Success),
                BeatSelection(actor="Iron Fang", beat_id=blue, outcome=RollOutcome.Success),
            ],
        ),
        player_name="Vega",
        pack=pack,
        room=room_for(snap),
    )


@pytest.fixture
def otel_capture():
    """In-memory span exporter (mirrors the sealed-letter integration suite)."""
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


def _transition_spans(exporter: InMemorySpanExporter) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == STATE_TRANSITION_SPAN]


# ---------------------------------------------------------------------------
# Instantiation stamps the entry state (AC-5, back-compat single-table def)
# ---------------------------------------------------------------------------


def test_dogfight_state_stamped_at_instantiation() -> None:
    """A freshly-seated dogfight starts in its entry state. Driven on the
    UNMODIFIED fixture pack: even a legacy single-table def must stamp
    ``dogfight_state`` (via the back-compat auto-registration), so resume
    and the apply seam have one shape for old and new content."""
    pack = load_fixture_pack(SWN_TEST_PACK)
    snap = GameSnapshot(genre=SWN_TEST_PACK)
    snap.genre_slug = SWN_TEST_PACK
    snap.world_slug = TEST_WORLD
    snap.characters = [_make_pilot("Vega")]
    _seat_dogfight(snap, pack)

    assert snap.encounter is not None
    assert snap.encounter.dogfight_state == "merge", (
        f"instantiation must stamp the entry state, got {snap.encounter.dogfight_state!r}"
    )


# ---------------------------------------------------------------------------
# The graph moves — and the NEXT turn provably uses the new state's table
# ---------------------------------------------------------------------------


def test_turn_advances_state_and_next_turn_uses_new_table(
    tmp_path: Path, otel_capture: InMemorySpanExporter
) -> None:
    """AC-5/AC-8 keystone. Turn 1 resolves the merge ``[straight, loop]``
    cell (authored ``next_state: tail_chase``) → ``dogfight_state`` advances
    and the transition span fires. Turn 2's ``[straight, straight]`` must
    then resolve from the TAIL_CHASE table — proven by the narration hint,
    which differs between the two tables' ``[straight, straight]`` cells
    ('Pursuer closes on fleeing target' vs merge's 'Clean merge')."""
    snap, pack = _graph_snap(tmp_path)
    _seat_dogfight(snap, pack)
    enc = snap.encounter
    assert enc is not None
    assert enc.dogfight_state == "merge"

    otel_capture.clear()
    _drive_turn(snap, pack, red="straight", blue="loop")

    assert enc.dogfight_state == "tail_chase", (
        f"the [straight, loop] cell transitions to tail_chase, got {enc.dogfight_state!r}"
    )
    spans = _transition_spans(otel_capture)
    assert len(spans) == 1, f"expected exactly one transition span, got {len(spans)}"
    assert spans[0].attributes.get("from_state") == "merge"
    assert spans[0].attributes.get("to_state") == "tail_chase"

    # Turn 2: same maneuver pair, different table — the hint proves which
    # table resolved (value-specific: merge's cell says 'rip past each other').
    _drive_turn(snap, pack, red="straight", blue="straight")
    hints = " ".join(enc.narrator_hints)
    assert "faster in a straight line" in hints, (
        f"turn 2 must resolve from the tail_chase table's [straight, straight] "
        f"cell, got hints: {hints!r}"
    )
    assert "rip past each other" not in hints, (
        "turn 2 resolved from the MERGE table — the state machine did not "
        "select the current state's table"
    )
    # No transition authored on that cell — the duel stays in tail_chase.
    assert enc.dogfight_state == "tail_chase"


def test_extend_and_return_walks_back_to_merge(
    tmp_path: Path, otel_capture: InMemorySpanExporter
) -> None:
    """AC-8: merge → tail_chase (cell transition), then the extend-and-return
    trigger cell breaks the engagement → back to merge, with both
    ``dogfight.state_transition`` spans in walk order."""
    snap, pack = _graph_snap(tmp_path)
    _seat_dogfight(snap, pack)
    enc = snap.encounter
    assert enc is not None

    otel_capture.clear()
    _drive_turn(snap, pack, red="straight", blue="loop")  # merge -> tail_chase
    assert enc.dogfight_state == "tail_chase"
    _drive_turn(snap, pack, red="loop", blue="loop")  # E&R trigger -> merge
    assert enc.dogfight_state == "merge", (
        "extend-and-return must transition the graph back to merge, not just "
        f"reset the descriptor — got {enc.dogfight_state!r}"
    )

    walks = [
        (s.attributes.get("from_state"), s.attributes.get("to_state"))
        for s in _transition_spans(otel_capture)
    ]
    assert walks == [("merge", "tail_chase"), ("tail_chase", "merge")], (
        f"transition spans must record the walk in order, got {walks!r}"
    )


# ---------------------------------------------------------------------------
# Fail loud on a dangling transition (AC-5, No Silent Fallbacks)
# ---------------------------------------------------------------------------


def test_unknown_next_state_fails_loud(tmp_path: Path) -> None:
    """A cell transitioning to a state with no registered table must raise at
    the apply seam — never silently stay in the current state (a silently
    ignored transition is exactly the 'winging it' class of bug the GM panel
    exists to catch)."""
    snap, pack = _graph_snap(tmp_path, transition_target="ghost_state")
    _seat_dogfight(snap, pack)

    with pytest.raises(ValueError, match="ghost_state"):
        _drive_turn(snap, pack, red="straight", blue="loop")
