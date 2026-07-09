"""Task 7 WIRING (ADR-096 v2, Track C2) — the load-bearing test THIS story owns.

The 165-2 WN tactical facts + adjudicators shipped INERT (zero production
callers). 165-3 wires them. This proves the wire is live: a real
``dispatch_dice_throw`` on a WN-bound pack must REACH ``_enforce_tactical_reach``
on the strike path — i.e. ``game.tactical`` is reached from the production dice
seam, not merely from a unit test (the SM's non-negotiable, CLAUDE.md "Verify
Wiring, Not Just Existence" + "No Source-Text Wiring Tests").

Strategy: spy on ``dice._enforce_tactical_reach`` (module-global, so a bare-name
call inside ``dispatch_dice_throw`` resolves to the spy) and assert the real
dispatch invoked it. Beat/grid/timing-agnostic and refactor-stable — it survives
moving the call site and fails cleanly (spy never called) when unwired. The spy
returns ``None`` (== enforcement skipped) so dispatch proceeds unchanged;
behaviour is covered by the unit suite (test_dice_tactical_enforcement.py).

Content-gated like its dispatch siblings (skips when sidequest-content is absent).
Reuses the proven heavy_metal dispatch harness rather than rebuilding it.
"""

from __future__ import annotations

import contextlib

import pytest

from tests.integration.test_wwn_heavy_metal_dispatch import (
    _build_caster,
    _discover_caster_and_damage_spell,
    _has_real_content,
    _load_heavy_metal,
)


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_dispatch_dice_throw_invokes_tactical_reach_enforcement(monkeypatch):
    import sidequest.server.dispatch.dice as dice_mod
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.dice import DiceDispatchError, dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return None  # None == enforcement skipped; dispatch proceeds unchanged

    # raising=False: the helper does not exist yet (RED). In RED the spy is
    # installed but the unwired dispatch never calls it -> `assert calls` fails
    # cleanly. In GREEN the wired dispatch calls the module-global -> the spy.
    monkeypatch.setattr(dice_mod, "_enforce_tactical_reach", _spy, raising=False)

    pack = _load_heavy_metal()
    assert pack.rules.ruleset == "wwn", "heavy_metal must be bound ruleset: wwn"
    class_display, _spell = _discover_caster_and_damage_spell(pack)

    attacker = "Sael"
    caster = _build_caster(pack, attacker, class_display=class_display)

    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(caster)
    opponent = "The Collector's Blade"
    snap.character_locations[attacker] = "The Antechamber"
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=attacker,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
        allow_synthetic_opponent=True,
    )
    assert enc is not None, "seating combat must produce an encounter"
    snap.encounter = enc
    # Pin the unseeded WN sealed-round initiative so the attacker acts first.
    enc.initiative = [
        InitiativeEntry(token_id=attacker, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]
    monkeypatch.setattr("random.randint", lambda a, b: a)

    # Fire a WN strike (the reach gate lives on the attack path). A weaponless
    # caster's strike may not fully resolve damage — we only assert the reach
    # gate was REACHED, which the plan places before damage resolution.
    # A weaponless caster's strike may not fully resolve damage; the reach gate
    # fires before damage resolution, so suppressing the late error is safe.
    with contextlib.suppress(DiceDispatchError):
        dispatch_dice_throw(
            payload=DiceThrowPayload(
                request_id="req-165-3-wiring",
                throw_params=ThrowParams(
                    velocity=(0.0, 5.0, -2.0),
                    angular=(1.0, 1.0, 1.0),
                    position=(0.5, 0.5),
                ),
                face=[15],  # a d20 attack roll
                beat_id="attack",
            ),
            rolling_player_id="player-sael",
            character_name=attacker,
            character_stats=dict(caster.stats),
            encounter=enc,
            pack=pack,
            genre_slug="heavy_metal",
            session_id="hm-165-3-wiring",
            round_number=1,
            room_broadcast=[].append,
            snapshot=snap,
        )

    assert calls, (
        "dispatch_dice_throw did not call _enforce_tactical_reach on a WN strike — "
        "C1's reach gate is not wired into the production dice seam (the wiring "
        "test this story owns; 165-2 shipped the adjudicators inert)"
    )
