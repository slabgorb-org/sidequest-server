"""Story 73-1: end-to-end resolution + opposed-dice mechanics for the converted
``negotiation`` and ``scandal`` confrontations (tea_and_murder).

Two concerns, both requiring the real engine path:

1. RESOLVABILITY (AC-4) — each confrontation's terminal ``push`` beat ends it on
   ANY outcome tier (closes the 59-8 frozen-dial soft-lock). Driven through
   ``apply_beat`` directly against the REAL pack, mirroring
   ``tests/integration/test_glenross_social_duel_concede.py``.

2. OPPOSED DICE + OTEL (AC-1 / AC-5) — the converted cdef routes through
   ``_resolve_opposed_check_branch``: the opponent's dial advances from ITS OWN
   server-rolled d20 and the ``encounter.opposed_roll_resolved`` lie-detector
   span fires (captured via the ``otel_capture`` fixture from this package's
   conftest). This is the mechanical inverse of the playtest bug — no
   narrator-fiat delta.

RED before the conversion: ``scandal`` was ``beat_selection`` 5/8 with a
non-resolving ``weather_it``, so the resolution and opposed-span assertions fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.beat_kinds import apply_beat
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)

SPAN_OPPOSED = "encounter.opposed_roll_resolved"
_ALL_TIERS = [RollOutcome.Fail, RollOutcome.CritFail, RollOutcome.Tie, RollOutcome.Success]


def _pack():
    return load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")


# ── AC-4: terminal push resolves on every tier (apply_beat, real content) ───


def _push_beat(ctype: str, beat_id: str):
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None, f"tea_and_murder must define a {ctype} confrontation"
    beat = next((b for b in cdef.beats if b.id == beat_id), None)
    assert beat is not None, f"{ctype} must define a {beat_id!r} beat"
    return beat


def _deadlocked_encounter(
    ctype: str, player_metric: str, opponent_metric: str
) -> StructuredEncounter:
    """Both sides seated, dials at 0 — the deadlock the voluntary exit must break."""
    return StructuredEncounter(
        encounter_type=ctype,
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name=player_metric, current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name=opponent_metric, current=0, starting=0, threshold=7),
        actors=[
            EncounterActor(name="Inspector Pryce", role="participant", side="player"),
            EncounterActor(name="The Other", role="participant", side="opponent"),
        ],
    )


def test_scandal_weather_it_carries_resolution_flag():
    """AC-4 content invariant: ``weather_it`` is a declarative resolver."""
    assert _push_beat("scandal", "weather_it").resolution is True


@pytest.mark.parametrize("outcome", _ALL_TIERS)
def test_scandal_resolves_on_weather_it(outcome: RollOutcome):
    """AC-4: weathering the storm is a voluntary exit — it ends the scandal on
    ANY tier. RED before the conversion: ``weather_it`` lacked ``resolution:
    true``, so Fail / CritFail / Tie left the player soft-locked."""
    enc = _deadlocked_encounter("scandal", "containment", "exposure")
    result = apply_beat(enc, enc.actors[0], _push_beat("scandal", "weather_it"), outcome, turn=1)
    assert result.resolved is True, f"weather_it must resolve the scandal on {outcome}"
    assert enc.resolved is True
    assert enc.outcome == "resolution_beat:weather_it"


def test_negotiation_walk_away_carries_resolution_flag():
    """AC-4 (regression guard): ``walk_away`` is already a declarative resolver."""
    assert _push_beat("negotiation", "walk_away").resolution is True


@pytest.mark.parametrize("outcome", _ALL_TIERS)
def test_negotiation_resolves_on_walk_away(outcome: RollOutcome):
    """AC-4 (regression guard): taking one's leave ends the negotiation on any
    tier — ``walk_away`` carries ``resolution: true`` and must keep it."""
    enc = _deadlocked_encounter("negotiation", "leverage", "leverage")
    result = apply_beat(enc, enc.actors[0], _push_beat("negotiation", "walk_away"), outcome, turn=1)
    assert result.resolved is True, f"walk_away must resolve the negotiation on {outcome}"
    assert enc.resolved is True
    assert enc.outcome == "resolution_beat:walk_away"


# ── AC-1 / AC-5: opposed dice path runs — opponent dial advances + span fires ─


def _live_encounter(
    ctype: str,
    player_metric: str,
    opponent_metric: str,
    opponent_name: str,
    stats: dict[str, int],
) -> StructuredEncounter:
    """A live opposed_check encounter with both actors carrying stats so the
    opposed-check modifier resolves cleanly regardless of opponent_default_stats."""
    return StructuredEncounter(
        encounter_type=ctype,
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name=player_metric, current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name=opponent_metric, current=0, starting=0, threshold=7),
        structured_phase=EncounterPhase.Setup,
        actors=[
            EncounterActor(
                name="Inspector Pryce",
                role="participant",
                side="player",
                per_actor_state={"stats": dict(stats)},
            ),
            EncounterActor(
                name=opponent_name,
                role="participant",
                side="opponent",
                per_actor_state={"stats": dict(stats)},
            ),
        ],
    )


def _drive_opposed(snap, *, beat_id, opponent_name, monkeypatch):
    """Player rolls low (3 → Fail); the opponent rolls high (server 18 → Success
    on a strike). Routes through _resolve_opposed_check_branch."""
    from sidequest.server import narration_apply as _na

    monkeypatch.setattr(_na, "_roll_d20_server_side", lambda: 18)
    result = NarrationTurnResult(
        narration="",
        beat_selections=[
            BeatSelection(actor=opponent_name, beat_id=beat_id, outcome=RollOutcome.Success),
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Inspector Pryce",
        pack=_pack(),
        opposed_player_d20=3,
        opposed_player_beat_id=beat_id,
        opposed_player_actor="Inspector Pryce",
        from_explicit_action=True,
        room=room_for(snap),
    )


def test_negotiation_opponent_dial_advances_on_opposed_roll(monkeypatch, otel_capture):
    """AC-1 + AC-5: the real negotiation cdef routes through the opposed_check
    branch — the OPPONENT's leverage dial advances from HER OWN roll and the
    ``encounter.opposed_roll_resolved`` span fires. Under beat_selection the
    branch never ran, the span never fired, and the opponent dial could not move
    from a dice roll."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.encounter = _live_encounter(
        "negotiation",
        "leverage",
        "leverage",
        "Mrs. Galbraith",
        {"Cunning": 12, "Nerve": 12},
    )

    _drive_opposed(
        snap, beat_id="persuade", opponent_name="Mrs. Galbraith", monkeypatch=monkeypatch
    )

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_OPPOSED]
    assert spans, "the opposed_check lie-detector span must fire for negotiation"
    assert snap.encounter is not None
    assert snap.encounter.player_metric.current == 0, "player rolled Fail — no advance"
    assert snap.encounter.opponent_metric.current > 0, (
        "the opponent's strike landed on her own roll — her leverage dial must "
        f"advance, not freeze at 0; got {snap.encounter.opponent_metric.current}"
    )


def test_scandal_opponent_dial_advances_on_opposed_roll(monkeypatch, otel_capture):
    """AC-1 + AC-5: the real scandal cdef routes through the opposed_check branch
    — the OPPONENT's exposure dial advances from HER OWN roll and the
    ``encounter.opposed_roll_resolved`` span fires."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    snap.encounter = _live_encounter(
        "scandal",
        "containment",
        "exposure",
        "Miss Crane",
        {"Cunning": 12, "Pride": 12, "Nerve": 12},
    )

    _drive_opposed(snap, beat_id="deflect", opponent_name="Miss Crane", monkeypatch=monkeypatch)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_OPPOSED]
    assert spans, "the opposed_check lie-detector span must fire for scandal"
    assert snap.encounter is not None
    assert snap.encounter.player_metric.current == 0, "player rolled Fail — no advance"
    assert snap.encounter.opponent_metric.current > 0, (
        "the opponent's strike landed on her own roll — her exposure dial must "
        f"advance, not freeze at 0; got {snap.encounter.opponent_metric.current}"
    )
