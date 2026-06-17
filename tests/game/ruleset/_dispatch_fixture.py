"""Shared fixture for ruleset dispatch routing tests.

Builds a minimal pack+encounter+snapshot and calls dispatch_dice_throw so
the wiring test can patch get_ruleset_module and observe spy calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    RulesConfig,
)
from sidequest.protocol.dice import (
    DiceThrowPayload,
    ThrowParams,
)
from sidequest.server.dispatch.dice import DiceThrowOutcome, dispatch_dice_throw


def _pack_with_combat() -> object:
    """Minimal GenrePack-shaped stub with dual-dial confrontation.

    rules.ruleset = "dial" so get_ruleset_module resolves correctly.
    """
    cdef = ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "kick_door",
                    "label": "Kick Door",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STRENGTH",
                }
            ),
        ],
    )
    rules = MagicMock(spec=RulesConfig)
    rules.confrontations = [cdef]
    rules.ruleset = "dial"
    pack = MagicMock()
    pack.rules = rules
    return pack


def _make_encounter() -> StructuredEncounter:
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
            threshold=10,
        ),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[EncounterActor(name="Bob", role="combatant", side="player")],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _make_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test",
        world_slug="test",
        turn_manager=TurnManager(),
    )


def _throw(face: int = 13, beat_id: str = "kick_door") -> DiceThrowPayload:
    return DiceThrowPayload(
        request_id="req-fixture-1",
        throw_params=ThrowParams(
            velocity=(0.0, 5.0, -2.0),
            angular=(1.0, 1.0, 1.0),
            position=(0.5, 0.5),
        ),
        face=[face],
        beat_id=beat_id,
    )


def resolve_one_combat_beat() -> DiceThrowOutcome:
    """Call dispatch_dice_throw with a minimal combat beat.

    Used by test_dispatch_routing.py to patch get_ruleset_module and verify
    the dispatch routes through the bound module.
    """
    pack = _pack_with_combat()
    enc = _make_encounter()
    snap = _make_snapshot()

    return dispatch_dice_throw(
        payload=_throw(face=13),
        rolling_player_id="p1",
        character_name="Bob",
        character_stats={"STRENGTH": 16},
        encounter=enc,
        pack=pack,
        genre_slug="test",
        session_id="s",
        round_number=1,
        room_broadcast=None,
        snapshot=snap,
    )
