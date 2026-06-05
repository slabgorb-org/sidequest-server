"""Heavy Metal → WWN Story 1 — end-to-end combat wiring proof.

Drives the REAL heavy_metal pack (ruleset: wwn) through the production seating
seam (instantiate_encounter_from_trigger) and the production dice seam
(dispatch_dice_throw) with the converted Blade-work combat
(beat_selection / hp_depletion). Proves on the real bound pack:

  1. pack.rules.ruleset == "wwn";
  2. the opponent seats with hp/armor_class from opponent_default_stats;
  3. a strike beat ablates the opponent's HP through the HP channel;
  4. the state_patch.hp span fires (the GM-panel lie detector).

The strike beat under test ("committed_blow") carries a deterministic
damage_override (2d6) so the proof does not depend on weapon-catalog plumbing;
rng is pinned so the damage roll is deterministic.

Skips cleanly when sidequest-content is not present on disk.

The ``otel_capture`` fixture is re-exported from ``tests/integration/conftest.py``
(itself re-exporting from ``tests.server.conftest``).
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# Authored on heavy_metal Blade-work opponent_default_stats (rules.yaml, Task 2).
_OPPONENT_HP = 10
_OPPONENT_AC = 12
_STRIKE_BEAT = "committed_blow"  # strike, damage_override 2d6 (deterministic)


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _make_attacker(name: str):
    """Synthetic Warrior attacker. Story 1 has no classes.yaml yet (Story 2),
    so the attacker is built directly, not through CharacterBuilder."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name=name,
        description="A blade-bearer of a house that is ending.",
        personality="grim",
        inventory=Inventory(),
        hp={"current": 12, "max": 12, "base_max": 12},
    )
    # WWN seating rolls initiative (1d8 + DEX) off character.stats, so the six
    # ability scores must be populated even though Story 1 has no classes.yaml
    # to build through. STR 12 matches the character_stats passed to the dice
    # seam below; the rest are flat 10s.
    return Character(
        core=core,
        char_class="Warrior",
        race="Human",
        backstory="—",
        stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_combat_is_wwn_bound_and_ablates_hp(otel_capture, monkeypatch):
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    pack = _load_heavy_metal()

    # ── Assertion 0: the pack is bound ruleset: wwn ───────────────────────
    assert pack.rules is not None
    assert pack.rules.ruleset == "wwn", (
        f"heavy_metal must be bound ruleset: wwn; got {pack.rules.ruleset!r}"
    )

    # ── Snapshot + attacker ───────────────────────────────────────────────
    attacker_name = "Sael"
    opponent = "The Collector's Blade"
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(_make_attacker(attacker_name))
    snap.character_locations[attacker_name] = "The Antechamber"

    # ── Seat the real Blade-work combat via the production seam ────────────
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=attacker_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"
    snap.encounter = enc

    opponent_core = snap.find_creature_core(opponent)
    assert opponent_core is not None, (
        "opponent core must be reachable via find_creature_core — without it the "
        "strike has no defender HP to ablate"
    )
    assert opponent_core.armor_class == _OPPONENT_AC, (
        "opponent AC must come from opponent_default_stats"
    )
    assert opponent_core.hp.current == _OPPONENT_HP, (
        "opponent HP must come from opponent_default_stats"
    )

    # ── Pin rng: the damage faces are rolled by
    # ``damage_roll.generate_server_faces`` via ``random.randint`` (NOT in the
    # dice module). Pin to MIN (1 per die = 2 on 2d6) so the opponent survives
    # the ablation and the downed seam is not tripped (this story proves
    # ablation, not the kill path). The d20 attack uses the provided face=[20],
    # not rng. ─────────────────────────────────────────────────────────────
    monkeypatch.setattr("sidequest.server.dispatch.damage_roll.random.randint", lambda a, b: a)

    hp_before = opponent_core.hp.current

    # face high enough to clear the strike DC (base=4 → DC = 10 + 4*2 = 18;
    # face 20 + STR mod clears it). character_stats passes the attacker's STR.
    broadcasts: list[object] = []
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="hm-wwn-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=_STRIKE_BEAT,
        ),
        rolling_player_id="player-sael",
        character_name=attacker_name,
        character_stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="hm-wwn-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )

    # ── Assertion 1: HP ablated through the HP channel ────────────────────
    assert opponent_core.hp.current < hp_before, (
        f"committed_blow must ablate the opponent's HP on the real wwn pack; "
        f"before={hp_before} after={opponent_core.hp.current}"
    )

    # ── Assertion 2: state_patch.hp span fired (the lie detector) ─────────
    finished = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in finished, (
        f"the wwn combat spine must emit a state_patch.hp span (GM-panel lie "
        f"detector); got spans: {finished}"
    )
