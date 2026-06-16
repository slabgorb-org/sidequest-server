"""Wiring test: _apply_narration_result_to_snapshot fires scene-end through Session.

Branch map (from inspection of narration_apply.py 1135-1450):

- The line-1443 branch lives inside the legacy beat-application path, gated
  by ``if _legacy_beat_path:`` (set True at 1298 when sealed-letter does not
  apply and the encounter is non-resolved with at least one surviving beat
  selection). Outer scope:

      if enc is not None and not enc.resolved and gated_selections:   # 1162
          ...
          if cdef.resolution_mode == ResolutionMode.sealed_letter_lookup:
              ... _legacy_beat_path = False
          else:
              _legacy_beat_path = True

      if _legacy_beat_path:                                           # 1303
          turn_num = snapshot.turn_manager.interaction                # 1348
          for sel in selections:                                      # 1349
              ...
              result_apply = apply_beat(enc, actor, beat, tier,
                                        turn=turn_num)                # 1363
              ...
              if result_apply.resolved:                               # 1415
                  ... encounter_resolved span / state_transition ...
                  # Scratch sweep + (post-fix) clock advance
                  room.session.end_scene("scene_end", turn=turn_num)  # 1443
                  break

- ``result`` fields read by the gating branch:
  * result.beat_selections              (1148)
  * each sel.actor / sel.beat_id / sel.outcome (BeatSelection objects)

- ``snapshot.encounter`` fields the gating branch requires:
  * encounter is non-None and not resolved (1162)
  * encounter_type matches a confrontation in pack.rules.confrontations (1163)
  * actor named in the selection exists on the encounter (1350)

- ``turn_num`` is bound at line 1348 from
  ``snapshot.turn_manager.interaction`` and threaded through ``apply_beat``
  and the post-resolution sweep.

To drive the resolution path, we use the synthetic two-dial pack's
"attack" beat (kind=strike, base=2) on an opponent-side actor against an
``opponent_metric`` with threshold=2 — one Success-tier strike pushes
opponent_metric to threshold and ``apply_beat`` flips
``enc.resolved = True``, returning ``ApplyResult(resolved=True)``.
"""

from __future__ import annotations

from pathlib import Path

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.protocol.dice import RollOutcome
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.session_room import SessionRoom


def _make_room(tmp_path: Path) -> tuple[SessionRoom, GameSnapshot]:
    room = SessionRoom(slug="test_world", mode=GameMode.SOLO)
    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    room.bind_world(snapshot=snap, store=SqliteStore(tmp_path / "t.db"))
    return room, snap


def _resolve_ready_encounter() -> StructuredEncounter:
    """Encounter where one Success-tier opponent strike triggers resolution.

    Opponent metric threshold=2 → "Promo" applying the synthetic-pack
    "attack" beat (kind=strike, base=2) at Success tier pushes
    opponent_metric.current to 2 and flips enc.resolved=True inside
    apply_beat (per beat_kinds.py:460-464).
    """
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=2,
        ),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )


def test_narration_apply_advances_clock_on_scene_end(
    tmp_path,
    otel_capture,
    synthetic_two_dial_pack,
):
    """Narrator-beat-resolved encounter -> Session.end_scene -> clock advances.

    Drives the line-1443 branch end-to-end: opponent strike at
    Success tier pushes opponent_metric to threshold, apply_beat flips
    resolved=True, and the migrated code path calls
    ``room.session.end_scene("scene_end", turn=turn_num)`` instead of
    the local ``clear_scratch_on_scene_end`` import.
    """
    room, snap = _make_room(tmp_path)
    snap.encounter = _resolve_ready_encounter()
    assert snap.clock_t_hours == 0.0

    # Opponent-side beat: not subject to the SOUL "The Test" PC-consent
    # gate, so ``from_explicit_action`` can stay at its default False
    # (which mirrors the production websocket caller for
    # opponent-driven turns).
    result = NarrationTurnResult(
        narration="Promo lunges and overwhelms Sam.",
        beat_selections=[
            BeatSelection(
                actor="Promo",
                beat_id="attack",
                outcome=RollOutcome.Success,
            ),
        ],
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Sam",
        room=room,
        pack=synthetic_two_dial_pack,
    )

    # Anchor: the line-1443 branch was actually entered.
    assert snap.encounter.resolved, (
        "encounter must be flipped to resolved=True by apply_beat — "
        "if this fails the line-1443 branch was not entered and the "
        "wiring assertions below pass vacuously"
    )
    assert snap.encounter.outcome == "opponent_victory"

    # Front-door wiring: end_scene was called and advanced the clock.
    assert snap.clock_t_hours == 1.0
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "clock.advance" in span_names


def test_narration_apply_does_not_advance_clock_on_location_change(
    tmp_path,
    otel_capture,
):
    """Walking to a new room is not a scene end; clock must stay at 0.

    The location-change path at line 654 still calls
    ``clear_scratch_on_scene_end`` directly (no front-door migration
    there — location change is not a scene end semantically). The
    ``room=`` kwarg is threaded through so the function signature is
    satisfied, but the migrated branch is not reached.
    """
    room, snap = _make_room(tmp_path)
    snap.character_locations["Sam"] = "The Throat"  # old_loc must be truthy to take the sweep guard

    result = NarrationTurnResult(
        narration="They march on.",
        location="The Antechamber",
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Sam",
        room=room,
    )

    assert snap.clock_t_hours == 0.0
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "clock.advance" not in span_names
    # The scratch-sweep at line 654 still runs (clear_scratch_on_scene_end
    # is called directly), but the encounter.status_cleared span only fires
    # per cleared scene-bounded status. With a bare snapshot there are no
    # statuses to sweep — that's not a regression, it's truthful telemetry.
    # The scratch-sweep behavior is verified by tests/server/test_status_clear.py
    # against realistic fixtures.
