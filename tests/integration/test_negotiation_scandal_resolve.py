"""Story 73-1 (re-authored 2026-06-17, Fate Contest binding): end-to-end
resolvability for the ``negotiation`` and ``scandal`` confrontations
(tea_and_murder), now **Fate Contests** (``resolution_mode: contest``, ADR-144).

The opposed-dice (``opposed_check``) mechanics this file originally exercised are
gone — those ``encounter.opposed_roll_resolved`` span tests were deleted with the
conversion. The Fate Contest opposed-4dF mechanics + ``fate.contest.*`` spans are
exercised in ``tests/server/dispatch/test_fate_contest.py``.

What survives (and what this file still pins): RESOLVABILITY (AC-4) — each
confrontation's terminal ``push`` beat ends it on ANY outcome tier (closes the
59-8 frozen-dial soft-lock). Driven through ``apply_beat`` directly against the
REAL pack, mirroring ``tests/integration/test_glenross_social_duel_concede.py``.
The voluntary push exit is a declarative resolver independent of which resolution
engine (dial / opposed / contest) drives the contested beats.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.confrontation import find_confrontation_def

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)

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
