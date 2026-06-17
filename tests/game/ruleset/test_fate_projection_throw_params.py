"""Story 126-7 (ADR-148): build_fate_roll_payload echoes the thrower's gesture.

When a player physically throws, the FATE_ROLL broadcast must carry the SAME
``throw_params`` the player threw so every spectator replays the identical tumble
(and the tray snaps to the authoritative dice). NPC rolls have no client gesture,
so the projection defaults to the synthesized ``_DEFAULT_FATE_THROW`` (the 125-4
groundwork — preserved, not reverted).

The outcome is built via the existing rng ``resolve_action`` so this test
isolates the projection change (the new ``throw_params=`` override) from the
Task-2 ``resolve_action_from_faces`` symbol.
"""

from __future__ import annotations

import random

from sidequest.game.ruleset.fate_projection import _DEFAULT_FATE_THROW, build_fate_roll_payload
from sidequest.game.ruleset.fate_resolution import Opposition, resolve_action
from sidequest.protocol.dice import ThrowParams


def _outcome():
    return resolve_action(
        skill_rating=2, opposition=Opposition(value=0, kind="passive"), rng=random.Random(0)
    )


def test_defaults_to_synthesized_throw_for_npc():
    payload = build_fate_roll_payload(_outcome(), seed=42)
    assert payload.throw_params == _DEFAULT_FATE_THROW
    assert payload.seed == 42


def test_echoes_player_throw_params_when_supplied():
    tp = ThrowParams(velocity=(0.0, 5.0, -2.0), angular=(1.0, 0.0, 0.0), position=(0.3, 0.7))
    payload = build_fate_roll_payload(_outcome(), seed=42, throw_params=tp)
    assert payload.throw_params == tp
    assert payload.throw_params != _DEFAULT_FATE_THROW  # the thrower's gesture, not the default
