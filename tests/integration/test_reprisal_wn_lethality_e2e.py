"""Story 102-1 (RED) — heavy_metal reprisal kill emits WN lethality spans (AC5b proof).

The 90-3 AC5b free-play playtest (heavy_metal/long_foundry, PC Vesska, slug
2026-06-10-long_foundry) measured the gap this story closes: when the PC died
to the opponent's reprisal, the session emitted ``hp_depletion.resolved`` +
``post_resolution_lethality.applied`` (verdict=dead) and NOTHING module-scoped
— no ``wwn.*`` span — so the GM panel could not show WN lethality engaged on
the combat half of AC5b.

This is the live-shaped proof on the REAL heavy_metal pack (``ruleset: wwn``,
lethality ``pc: dead``): seat the real Blade-work combat through the production
seating seam, drive one player strike through the production dice seam, let the
forced-hit reprisal drop the 1-HP PC, and assert the WN downed seam ran FOR THE
PC — ``wwn.mortal_injury.declared`` actor=PC alongside the generic verdict.

Fixture lineage: seating + pack loading from
tests/integration/test_wwn_heavy_metal_combat.py; reprisal forcing from
tests/integration/test_opponent_reprisal_e2e.py.

DETERMINISM: one arg-dispatching randint fake governs the whole path (dice.py,
downed_seam.py and damage_roll.py all roll via the shared ``random`` module):
(1, 20) → 20 — the reprisal to-hit always HITS (PC AC is 10; 20 + mods clears
it; a save roll, if one ever happens here, simply succeeds and rolls no table);
every other range → its MINIMUM — the player's synthesized attack draws the
equipped 2d6 blade and deals 2 (the 10-HP opponent survives at 8, so the
player's own strike can never resolve the fight or trip the opponent-side downed
seam), any trauma die rolls low (the
scene stays non-traumatic), and the opponent's 1d6 reprisal damage deals 1
(exactly enough to drop the 1-HP PC). The player's own d20 uses the thrown
``face=[20]``, never rng (base=4 → DC 18; 20 + STR-12 mod clears it).

Skips cleanly when sidequest-content is not on disk. ``otel_capture`` is
re-exported from tests/integration/conftest.py.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

PLAYER = "Vesska"
OPPONENT = "The Collector's Blade"
# De-nativized WWN combat (108-3/108-8, ADR-143): the WN round owns the action
# set — the committed action is the synthesized WN "attack" (the native
# committed_blow beat was stripped). The PC is armed with a 2d6 blade
# (resolve_damage priority 2), so the synthesized attack deals 2 at min — exactly
# the pre-strip committed_blow 2d6 override, so the opponent still survives the
# player's swing and the lethal beat stays the opponent's reprisal. Story 125-8.
_STRIKE_BEAT = "attack"

SPAN_WWN_MORTAL_INJURY = "wwn.mortal_injury.declared"
SPAN_GENERIC_LETHALITY = "encounter.post_resolution_lethality"

# BLOCKED on epic-152: this proof needs the opponent's reprisal to KILL the 1-HP
# PC, but under de-nativized WWN combat the opponent attack is skipped
# (``_resolve_opponent_reprisal`` requires an authored strike beat in
# ``cdef.beats``, stripped by 108-3; 108-8 synthesized only the PLAYER's attack,
# so ``wn_round`` logs ``opponent_reprisal_skipped reason=no_strike_beat`` and
# the PC is never dropped). Fixing it needs production opponent-attack synthesis —
# out of scope for 125-8 (test-debt only, AC3). Loud-skip + linked story per AC1;
# see session Delivery Findings.
_OPPONENT_ATTACK_BLOCKED = (
    "epic-152: WN opponent attack/reprisal skipped under de-nativized WWN combat "
    "(no_strike_beat — opponent strike beat never synthesized; 108-8 did only the "
    "player). Production gap; 125-8 is test-debt only (AC3). See Delivery Findings."
)


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _make_dying_pc(name: str):
    """A 1-HP Warrior at AC 10: any connecting reprisal damage kills. Six
    ability scores populated because WWN seating rolls initiative off
    character.stats (same shape as test_wwn_heavy_metal_combat.py)."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name=name,
        description="A blade-bearer one wound from the dark.",
        personality="grim",
        # Armed with a 2d6 blade so the synthesized WN attack resolves weapon dice
        # (resolve_damage priority 2) — heavy_metal ships no unarmed_damage floor.
        # 2d6 → 2 at min reproduces the pre-108-3 committed_blow override (125-8).
        inventory=Inventory(
            items=[
                {
                    "id": "blade_2d6",
                    "name": "Heavy Blade",
                    "category": "weapon",
                    "equipped": True,
                    "damage": {"dice": "2d6", "bonus": 0},
                }
            ]
        ),
        hp={"current": 1, "max": 12, "base_max": 12},
        armor_class=10,
    )
    return Character(
        core=core,
        char_class="Warrior",
        race="Human",
        backstory="—",
        stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
    )


def _reprisal_hits_all_else_min(a: int, b: int) -> int:
    """(1, 20) → 20 (reprisal to-hit HITS); anything else → minimum."""
    return 20 if (a, b) == (1, 20) else a


@pytest.mark.skip(reason=_OPPONENT_ATTACK_BLOCKED)
@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_heavy_metal_reprisal_kill_emits_wwn_mortal_injury_for_pc(otel_capture, monkeypatch):
    """The AC5b combat-half proof: a PC dying to the Blade-work reprisal on the
    real wwn-bound heavy_metal pack (lethality pc=dead) must emit
    ``wwn.mortal_injury.declared`` with actor=PC, alongside the generic
    ``encounter.post_resolution_lethality`` lethal_down decision.

    SKIPPED (125-8): needs the opponent reprisal to kill the PC — the
    no_strike_beat production gap owned by epic-152 (see _OPPONENT_ATTACK_BLOCKED)."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    pack = _load_heavy_metal()
    assert pack.rules is not None and pack.rules.ruleset == "wwn", (
        f"heavy_metal must be bound ruleset: wwn; got "
        f"{pack.rules.ruleset if pack.rules else None!r}"
    )
    assert (
        pack.lethality_policy is not None and pack.lethality_policy.verdicts_on_zero_hp.pc == "dead"
    ), "heavy_metal's lethality policy must verdict a 0-HP PC `dead` (lethal genre)"

    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(_make_dying_pc(PLAYER))
    snap.character_locations[PLAYER] = "The Antechamber"

    # Seat the real Blade-work combat via the production seam.
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"
    snap.encounter = enc
    # Story 102-4: the WN sealed round resolves in the PERSISTED initiative
    # order, and the seam's real 1d8+DEX roll is unseeded — pin the order this
    # test's choreography assumes (the PC swings, the opponent's answer kills)
    # so the dying PC is never randomly downed before their own slot.
    from sidequest.protocol.models import InitiativeEntry

    enc.initiative = [
        InitiativeEntry(token_id=PLAYER, value=9),
        InitiativeEntry(token_id=OPPONENT, value=2),
    ]

    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="hm-102-1-reprisal-kill",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=_STRIKE_BEAT,
        ),
        rolling_player_id="player-vesska",
        character_name=PLAYER,
        character_stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="hm-102-1-session",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    # ── Preconditions (pass today): the reprisal kills and resolves ────────
    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current <= 0, (
        f"precondition: the forced-hit reprisal must drop the 1-HP PC; hp={player_core.hp.current}"
    )
    assert enc.resolved and enc.outcome == "opponent_victory", (
        f"precondition: the kill must resolve against the player; "
        f"resolved={enc.resolved} outcome={enc.outcome}"
    )

    finished = otel_capture.get_finished_spans()
    span_names = [s.name for s in finished]

    # ── The story's assertion: WN lethality engaged for the DYING PC ──────
    mortal = [s for s in finished if s.name == SPAN_WWN_MORTAL_INJURY]
    assert len(mortal) == 1, (
        f"a PC dying to the reprisal on the real wwn-bound heavy_metal pack must "
        f"declare exactly one WN Mortal Injury — this is the AC5b combat-half "
        f"lie-detector the 2026-06-10 long_foundry playtest found missing; "
        f"got spans: {span_names}"
    )
    mattrs = dict(mortal[0].attributes or {})
    assert mattrs.get("actor") == PLAYER, (
        f"the mortal-injury actor must be the dying PC; attrs={mattrs}"
    )
    # sq-playtest #239 (death dual-status): the WN lethality span STILL fires
    # (GM-panel proof WN lethality engaged) but is marked superseded — the genre
    # policy already ruled this PC terminally dead, so the dying-window STATUS is
    # not appended (see the status-coherence assertion below).
    assert mattrs.get("superseded_by_terminal") is True, (
        f"a PC the genre policy already ruled terminally dead must declare the WN "
        f"Mortal Injury span as superseded_by_terminal; attrs={mattrs}"
    )

    # ── And the generic genre verdict still applied beside it ─────────────
    generic = [s for s in finished if s.name == SPAN_GENERIC_LETHALITY]
    assert len(generic) == 1, (
        f"the generic lethality decision must still fire once; got {len(generic)}"
    )
    gattrs = dict(generic[0].attributes or {})
    assert gattrs.get("decision") == "lethal_down" and gattrs.get("verdict") == "dead", (
        f"heavy_metal's pc=dead verdict must hold the PC down; attrs={gattrs}"
    )
    # sq-playtest #239: ONE coherent status. The terminally-dead PC carries the
    # incapacitating "Downed — ... (mortally wounded)" verdict and NO coexisting
    # non-terminal "dies in N rounds unless stabilized" window (the contradiction
    # the playtest found 3× — Zeppo/Chico/Mercutio). The real WWN dying window is
    # deferred to story 106-5 (unactionable in solo per gm-decisions).
    statuses = player_core.statuses
    assert any(s.text.startswith("Downed") and s.incapacitating for s in statuses), (
        f"the dead PC must carry the incapacitating Downed verdict; "
        f"statuses={[(s.text, s.incapacitating) for s in statuses]}"
    )
    assert not any("dies in" in s.text and "unless stabilized" in s.text for s in statuses), (
        f"the terminally-dead PC must NOT also carry a stabilizable 'dies in N rounds' "
        f"window (superseded for coherence, #239); statuses={[s.text for s in statuses]}"
    )
