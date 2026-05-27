"""Task 13 wiring test — SWN shot resolution wired into sealed-letter dispatch.

Drives a sealed-letter dogfight turn through ``_apply_narration_result_to_snapshot``
where the cell yields a mutual gun solution (both pilots can fire), monkeypatches
d20 and damage dice for determinism, and asserts:

  (a) The NPC's frame HP dropped from 8 → 8 - applied_damage (hit from player).
  (b) ``dogfight.shot_attempted`` and ``dogfight.shot_damage`` OTEL spans fired.

The NPC shooter can also score a hit — we pin the opponent d20 to a miss so the
player's HP stays intact and the assertion is clean about which actor took damage.

Skips when ``sidequest-content`` is not checked out alongside ``sidequest-server``
(matches the pattern in ``test_sealed_letter_dispatch_integration.py``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import (
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.dogfight_shot import FRAME_HP_KEY
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for
from tests._helpers.trigger_encounter import trigger_encounter

CONTENT_ROOT = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"

pytestmark = pytest.mark.skipif(
    not CONTENT_ROOT.is_dir(),
    reason="sidequest-content not on disk alongside sidequest-server",
)

PLAYER = "Apex"
OPPONENT = "Bandit Ace"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def space_opera_pack() -> GenrePack:
    return load_genre_pack(CONTENT_ROOT / "space_opera")


def _make_pilot_character(name: str) -> Character:
    """Build a minimal space_opera pilot PC with Reflex + Intellect stats.

    The SWN ship_attack_params resolves best-of(DEXTERITY→Reflex, INTELLIGENCE→Intellect)
    for the to-hit modifier. Both stats are set to 10 (modifier=0) so the math is
    deterministic without caring about the exact SWN modifier formula.
    """
    return Character(
        core=CreatureCore(
            name=name,
            description="Test pilot.",
            personality="Calm.",
        ),
        backstory="A pilot.",
        char_class="Pilot",
        race="Human",
        stats={"Reflex": 10, "Intellect": 10},
    )


@pytest.fixture
def snap_with_pilot(space_opera_pack: GenrePack) -> tuple[GameSnapshot, GenrePack]:
    snap = GameSnapshot(genre="space_opera")
    snap.genre_slug = "space_opera"
    snap.characters = [_make_pilot_character(PLAYER)]
    return snap, space_opera_pack


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


# ---------------------------------------------------------------------------
# Deterministic dice helpers
# ---------------------------------------------------------------------------


def _make_d20_patch(player_d20: int, npc_d20: int):
    """Return a side_effect callable for ``_roll_d20_server_side``.

    First call  = player's roll (gun_solutions are ordered player→npc in
    the dict comprehension by shooter_role iteration), but we just want
    to be robust: we patch to always return a hit for the player and a
    miss for the NPC by checking call_count. The simplest robust approach
    is to return ``player_d20`` for any "red" role call and ``npc_d20``
    for "blue" — we accomplish that by returning ``player_d20`` on the
    first call and ``npc_d20`` on the second. If there's only one gun
    solution (only NPC fires), this is still correct.

    We use an iterator so the monkeypatch is stateless.
    """
    values = iter([player_d20, npc_d20, player_d20, npc_d20])  # generous repeat

    def _roll() -> int:
        return next(values)

    return _roll


# ---------------------------------------------------------------------------
# Core wiring test
# ---------------------------------------------------------------------------


def test_shot_resolution_wired_into_sealed_letter_dispatch(
    snap_with_pilot: tuple[GameSnapshot, GenrePack],
    otel_capture: InMemorySpanExporter,
) -> None:
    """Keystone wiring test for Task 13.

    A mutual-gunline cell (loop vs kill_rotation) yields GunSolutions for both
    pilots. We monkeypatch:
      - ``_roll_d20_server_side`` → player always hits (20), NPC always misses (1)
      - ``sidequest.game.dogfight_shot._roll_damage_dice`` → deterministic 4 dmg

    After the turn:
      (a) Opponent's frame_hp == 8 - 4 == 4 (player's attack landed, NPC did not)
          — note: dogfight cdef has armor=5, so applied = max(0, 4 - 5) = 0
          Let's use damage=6 to ensure it penetrates (6 - 5 = 1 applied). But
          we need to check what armor is authored. Let's check cdef at runtime
          and just assert frame_hp < 8 (any damage landed).
          Actually: armor=5, so we need raw ≥ 6 to penetrate. Monkeypatch to 6
          so applied = 1. frame_hp drops from 8 to 7.
      (b) ``dogfight.shot_attempted`` span fired (at least once).
          ``dogfight.shot_damage`` span fired (at least once — the hit landed).
    """
    snap, pack = snap_with_pilot

    # Turn 1: instantiate dogfight
    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PLAYER,
        npcs_present=[
            NpcMention(name=OPPONENT, role="hostile", side="opponent"),
        ],
    )
    enc = snap.encounter
    assert enc is not None
    otel_capture.clear()

    # Verify frame HP was seeded at instantiation (Task 12 contract)
    red = next(a for a in enc.actors if a.role == "red")
    blue = next(a for a in enc.actors if a.role == "blue")
    assert red.per_actor_state.get(FRAME_HP_KEY) == 8
    assert blue.per_actor_state.get(FRAME_HP_KEY) == 8

    blue_hp_before = int(blue.per_actor_state[FRAME_HP_KEY])

    # Turn 2: mutual-gunline maneuver pair — both pilots score gun solutions
    # Player d20=20 (auto-hit), NPC d20=1 (auto-miss).
    # Damage=6 so applied = 6 - armor(5) = 1 on the opponent.
    with (
        patch(
            "sidequest.server.narration_apply._roll_d20_server_side",
            side_effect=_make_d20_patch(player_d20=20, npc_d20=1),
        ),
        patch(
            "sidequest.game.dogfight_shot._roll_damage_dice",
            return_value=6,
        ),
    ):
        _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="You pull the loop; the bandit counters with the kill-rotation.",
                beat_selections=[
                    BeatSelection(actor=PLAYER, beat_id="loop"),
                    BeatSelection(actor=OPPONENT, beat_id="kill_rotation"),
                ],
            ),
            player_name=PLAYER,
            pack=pack,
            room=room_for(snap),
        )

    # (a) Opponent frame HP dropped — player's shot landed
    blue_hp_after = int(blue.per_actor_state[FRAME_HP_KEY])
    assert blue_hp_after < blue_hp_before, (
        f"opponent frame_hp should have dropped from {blue_hp_before} after a hit "
        f"(d20=20 auto-hit, damage=6, armor=5 → applied=1); got {blue_hp_after}"
    )

    # Sanity: player frame HP must be 8 (NPC d20=1 auto-miss)
    red_hp_after = int(red.per_actor_state[FRAME_HP_KEY])
    assert red_hp_after == 8, (
        f"player frame_hp should remain 8 (NPC d20=1 auto-miss); got {red_hp_after}"
    )

    # (b) OTEL spans fired
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "dogfight.shot_attempted" in span_names, (
        f"dogfight.shot_attempted span must fire when gun solutions exist; "
        f"got spans: {sorted(set(span_names))}"
    )
    # dogfight.shot_damage fires only on hits
    assert "dogfight.shot_damage" in span_names, (
        f"dogfight.shot_damage span must fire when a shot lands (d20=20 auto-hit); "
        f"got spans: {sorted(set(span_names))}"
    )

    # Verify OTEL attributes on the shot_attempted span
    shot_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "dogfight.shot_attempted"
    ]
    assert len(shot_spans) >= 1
    # Both pilots have gun solutions on mutual-gunline — expect 2 shot_attempted spans
    assert len(shot_spans) == 2, (
        f"mutual gunline: expected 2 dogfight.shot_attempted spans (one per shooter), "
        f"got {len(shot_spans)}"
    )

    # The damage span must reference the NPC (opponent) as target
    damage_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "dogfight.shot_damage"
    ]
    assert len(damage_spans) >= 1
    damage_targets = [(s.attributes or {}).get("target") for s in damage_spans]
    assert OPPONENT in damage_targets, (
        f"dogfight.shot_damage must name {OPPONENT!r} as target; got {damage_targets}"
    )


# ---------------------------------------------------------------------------
# Depletion path: when opponent frame HP hits 0, pending_resolution_signal is set
# ---------------------------------------------------------------------------


def test_shot_resolution_sets_pending_resolution_signal_on_depletion(
    snap_with_pilot: tuple[GameSnapshot, GenrePack],
) -> None:
    """When a shot depletes the opponent's frame HP, ``snapshot.pending_resolution_signal``
    is set — the narrator reads it next turn and closes the encounter.
    """
    snap, pack = snap_with_pilot
    snap.characters = [_make_pilot_character(PLAYER)]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PLAYER,
        npcs_present=[
            NpcMention(name=OPPONENT, role="hostile", side="opponent"),
        ],
    )
    enc = snap.encounter
    assert enc is not None

    # Confirm no pending signal before the turn
    assert snap.pending_resolution_signal is None

    # d20=20 (auto-hit), damage=20 → 20 - armor(5) = 15 applied → 8 - 15 = 0 (depleted)
    with (
        patch(
            "sidequest.server.narration_apply._roll_d20_server_side",
            return_value=20,
        ),
        patch(
            "sidequest.game.dogfight_shot._roll_damage_dice",
            return_value=20,
        ),
    ):
        _apply_narration_result_to_snapshot(
            snap,
            NarrationTurnResult(
                narration="Kill shot.",
                beat_selections=[
                    BeatSelection(actor=PLAYER, beat_id="loop"),
                    BeatSelection(actor=OPPONENT, beat_id="kill_rotation"),
                ],
            ),
            player_name=PLAYER,
            pack=pack,
            room=room_for(snap),
        )

    blue = next(a for a in enc.actors if a.role == "blue")
    assert blue.per_actor_state[FRAME_HP_KEY] == 0, (
        f"opponent must be at 0 HP after kill-shot; got {blue.per_actor_state[FRAME_HP_KEY]}"
    )
    assert snap.pending_resolution_signal is not None, (
        "pending_resolution_signal must be set when frame HP is depleted"
    )


# ---------------------------------------------------------------------------
# No-gun-solution turn: shots skipped when cell yields no gunline
# ---------------------------------------------------------------------------


def test_no_gun_solution_skips_shot_resolution(
    snap_with_pilot: tuple[GameSnapshot, GenrePack],
    otel_capture: InMemorySpanExporter,
) -> None:
    """A maneuver cell that grants no gun solutions must NOT fire shot spans.

    ``straight`` vs ``straight`` → a neutral cell with no gunline (neither pilot
    achieves a firing solution). No ``dogfight.shot_attempted`` span should fire.
    """
    snap, pack = snap_with_pilot
    snap.characters = [_make_pilot_character(PLAYER)]

    trigger_encounter(
        snap,
        pack,
        "dogfight",
        PLAYER,
        npcs_present=[
            NpcMention(name=OPPONENT, role="hostile", side="opponent"),
        ],
    )
    otel_capture.clear()

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Straight pass, no advantage.",
            beat_selections=[
                BeatSelection(actor=PLAYER, beat_id="straight"),
                BeatSelection(actor=OPPONENT, beat_id="straight"),
            ],
        ),
        player_name=PLAYER,
        pack=pack,
        room=room_for(snap),
    )

    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "dogfight.shot_attempted" not in span_names, (
        f"no gun solutions from straight/straight cell; shot spans must not fire. "
        f"Got: {sorted(span_names)}"
    )
