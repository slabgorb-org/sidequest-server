"""Regression — playtest 2026-06-10 (barsoom-2): a WWN Shock kill was invisible
on every surface, so a mechanically-correct resolution read as a fabricated win.

The repro: Tarkas (Warrior L1, Iron Longsword ``shock: 2 / shock_ac: 15``) vs
The White Ape (AC 12) at 3/10 HP. The player's strike CritFailed (natural 1),
but WWN Shock chips damage ON A MISS vs Melee AC <= shock_ac — chip 2, plus the
Warrior Killing Blow rider (+1 at level 1, SRD §1.5.18 applies it to Shock) =
exactly 3. Ape 3→0, ``check_hp_depletion`` correctly resolved
``player_victory``. Faithful WWN — but:

1. The shock chip's HP-removed was DISCARDED at the dispatch layer (sibling of
   the #794 hit-path fix), so the persisted ``ENCOUNTER_BEAT_APPLIED`` recorded
   ``opponent_hp_removed=0`` on a beat that removed 3 HP — post-hoc forensics
   read "zero-damage CritFail resolves player_victory" as the engine
   fabricating a win.
2. The player-beat hp_depletion close never stamped
   ``pending_resolution_signal`` nor appended a MECHANICAL TRUTH directive
   (the reprisal close got both in the 2026-06-07 fix) — the narrator saw
   "strike CritFail" and improvised a player DEFEAT over a resolved victory.

These tests drive the EXACT production seam (``dispatch_dice_throw`` on the
real heavy_metal pack) and pin: (1) the shock chip lands in the persisted
beat event (``opponent_hp_removed`` total + ``shock_hp_removed`` attribution),
(2) a shock watcher event fires (text-log/GM-timeline visibility), (3) the
resolution stamps the narrator signal + directive, and (4) none of it is
fabricated when no shock applies.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_STRIKE_BEAT = "strike"
_OPPONENT = "The White Ape"
_ATTACKER = "Tarkas"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _make_warrior(*, shock_weapon: bool) -> object:
    """WWN Warrior with an equipped longsword.

    ``shock_weapon=True`` carries the barsoom Iron Longsword damage spec
    (``shock: 2 / shock_ac: 15``) on the item dict (damage-spec resolution
    priority 2 — the item-dict path, so the test does not depend on a
    world-tier catalog lookup). ``False`` carries a plain 1d8 with no shock.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    damage: dict = {"dice": "1d8", "bonus": 0}
    if shock_weapon:
        damage |= {"shock": 2, "shock_ac": 15}
    core = CreatureCore(
        name=_ATTACKER,
        description="A Green Martian of the hordes.",
        personality="grim",
        inventory=Inventory(
            items=[
                {
                    "id": "sword_iron",
                    "name": "Iron Longsword",
                    "category": "weapon",
                    "equipped": True,
                    "damage": damage,
                }
            ]
        ),
        hp={"current": 1, "max": 10, "base_max": 10},
    )
    return Character(
        core=core,
        char_class="Warrior",
        race="Green Martian",
        backstory="—",
        stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
    )


def _seat_combat(snap, pack):
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=_ATTACKER,
        npcs_present=[NpcMention(name=_OPPONENT, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"
    snap.encounter = enc
    return enc


def _capture_dice_watcher(monkeypatch) -> list[dict]:
    import sidequest.server.dispatch.dice as dice_mod

    captured: list[dict] = []

    def _capture(event_type, fields, **kwargs):
        captured.append({"event_type": event_type, **fields})

    monkeypatch.setattr(dice_mod, "_watcher_publish", _capture)
    return captured


def _throw(snap, enc, pack, *, face: int) -> None:
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="shock-kill-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[face],
            beat_id=_STRIKE_BEAT,
        ),
        rolling_player_id="player-tarkas",
        character_name=_ATTACKER,
        character_stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="shock-kill-session",
        round_number=7,
        room_broadcast=(lambda _msg: None),
        snapshot=snap,
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_shock_kill_on_critfail_is_observable_end_to_end(monkeypatch):
    """The barsoom-2 repro: CritFail strike, shock chip 3 kills the 3-HP Other.

    Pins all three surfaces the playtest found lying:
    forensics (beat event carries the shock ablation), the GM timeline
    (a shock watcher event), and the narrator contract (resolution signal +
    MECHANICAL TRUTH directive so prose cannot render a defeat over a win).
    """
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    pack = _load_heavy_metal()
    assert pack.rules is not None and pack.rules.ruleset == "wwn"

    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="barsoom",
        turn_manager=TurnManager(interaction=7),
    )
    snap.characters.append(_make_warrior(shock_weapon=True))
    snap.character_locations[_ATTACKER] = "The Pit"

    enc = _seat_combat(snap, pack)
    opponent_core = snap.find_creature_core(_OPPONENT)
    assert opponent_core is not None
    # The playtest state: ape ground down to 3/10 before the final beat.
    opponent_core.hp.current = 3

    events = _capture_dice_watcher(monkeypatch)
    _throw(snap, enc, pack, face=1)  # natural 1 → CritFail

    # ── Mechanism pin: shock 2 + Killing Blow 1 == the ape's last 3 HP ──────
    assert opponent_core.hp.current == 0, (
        f"shock chip (2) + Killing Blow rider (1) must remove the ape's last "
        f"3 HP on the missed swing; got hp={opponent_core.hp.current}"
    )
    assert enc.resolved and enc.outcome == "player_victory"

    # ── Surface 1: persisted forensics carries the shock ablation ───────────
    beat_events = [e for e in events if e.get("op") == "beat_applied"]
    assert beat_events, f"no beat_applied watcher event published; got {events}"
    beat = beat_events[0]
    assert beat["opponent_hp_removed"] == 3, (
        "ENCOUNTER_BEAT_APPLIED.opponent_hp_removed must carry the TOTAL HP "
        f"removed from the opponent this beat (shock chip included); got "
        f"{beat['opponent_hp_removed']!r} — this is the exact blindness that "
        "made the playtest read a real shock kill as a fabricated win"
    )
    assert beat.get("shock_hp_removed") == 3, (
        "the beat event must attribute the shock-sourced share separately "
        f"(shock_hp_removed); got {beat.get('shock_hp_removed')!r}"
    )

    # ── Surface 2: the shock chip itself is a GM-timeline event ─────────────
    shock_events = [e for e in events if e.get("op") == "shock_chip_applied"]
    assert shock_events, (
        f"a WWN shock chip must publish a watcher event (GM timeline / text "
        f"log) — the live wwn.shock.applied span alone is grep-blind; got ops "
        f"{[e.get('op') for e in events]}"
    )
    assert shock_events[0]["chip"] == 3
    assert shock_events[0]["target"] == _OPPONENT

    # ── Surface 3: the narrator is TOLD the fight resolved, and why ─────────
    assert snap.pending_resolution_signal is not None, (
        "the player-beat hp_depletion close must stamp pending_resolution_signal "
        "(the reprisal close already does — 2026-06-07 fix); without it the "
        "narrator improvises, and in the playtest it narrated a player DEFEAT "
        "over a resolved player_victory"
    )
    assert snap.pending_resolution_signal.outcome == "player_victory"
    truth = [d for d in snap.next_turn_directives if "MECHANICAL TRUTH" in d]
    assert truth, (
        f"the resolution close must append a MECHANICAL TRUTH directive; got "
        f"directives {snap.next_turn_directives}"
    )
    assert any("player_victory" in d for d in truth)
    # The shock attribution: the narrator must know the kill came on a MISS so
    # the prose renders a shock kill, not an improvised triumphant strike (or
    # worse, a defeat).
    assert any("Shock" in d or "shock" in d for d in truth), (
        f"the directive must attribute the killing damage to weapon Shock on "
        f"the missed swing; got {truth}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_player_hit_that_does_not_kill_anchors_opponent_alive(monkeypatch):
    """Kill-overclaim anchor (evropi + barsoom, 2026-06-10): a player hit that
    damages but does NOT kill must append a MECHANICAL TRUTH directive carrying
    the target's real HP and that they are still standing — twice this playtest
    the narrator rendered unambiguous kill prose for an Other the engine
    correctly kept alive (2/14 evropi, 3/10 barsoom), because nothing anchored
    the post-strike HP."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    pack = _load_heavy_metal()
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="barsoom",
        turn_manager=TurnManager(interaction=7),
    )
    snap.characters.append(_make_warrior(shock_weapon=False))
    snap.character_locations[_ATTACKER] = "The Pit"

    enc = _seat_combat(snap, pack)
    opponent_core = snap.find_creature_core(_OPPONENT)
    assert opponent_core is not None and opponent_core.hp.current == 10

    # Pin damage faces to MIN: committed_blow damage_override 2d6 → 2, plus
    # the Warrior Killing Blow rider (+1 at L1) = 3. Opponent 10→7, ALIVE.
    monkeypatch.setattr("sidequest.server.dispatch.damage_roll.random.randint", lambda a, b: a)

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="alive-anchor-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],  # natural 20 → CritSuccess, guaranteed hit
            beat_id="committed_blow",
        ),
        rolling_player_id="player-tarkas",
        character_name=_ATTACKER,
        character_stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="alive-anchor-session",
        round_number=7,
        room_broadcast=(lambda _msg: None),
        snapshot=snap,
    )

    hp_after = opponent_core.hp.current
    assert 0 < hp_after < 10, (
        f"precondition: the pinned-min hit must wound but not kill; hp={hp_after}"
    )
    assert not enc.resolved

    alive_anchors = [
        d
        for d in snap.next_turn_directives
        if "MECHANICAL TRUTH" in d and _OPPONENT in d and "STILL STANDING" in d
    ]
    assert alive_anchors, (
        f"a damaging, non-killing player hit must anchor the target's "
        f"aliveness for the narrator (kill-overclaim family — evropi 2/14, "
        f"barsoom 3/10); directives={snap.next_turn_directives!r}"
    )
    assert any(f"{hp_after}/{opponent_core.hp.max}" in d for d in alive_anchors), (
        f"the anchor must state the target's REAL HP "
        f"({hp_after}/{opponent_core.hp.max}); got {alive_anchors!r}"
    )
    # No resolution directive — the fight is live.
    assert not any("RESOLVED" in d for d in snap.next_turn_directives)


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_no_shock_no_fabrication_on_plain_miss(monkeypatch):
    """Guard rail: a missed strike with a shock-less weapon fabricates nothing —
    no HP removed, no shock event, no resolution signal, encounter live."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    pack = _load_heavy_metal()
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="barsoom",
        turn_manager=TurnManager(interaction=7),
    )
    snap.characters.append(_make_warrior(shock_weapon=False))
    snap.character_locations[_ATTACKER] = "The Pit"

    enc = _seat_combat(snap, pack)
    opponent_core = snap.find_creature_core(_OPPONENT)
    assert opponent_core is not None
    opponent_core.hp.current = 3
    # Pin the reprisal's dice so the opponent's answer can't kill the 1-HP
    # attacker and resolve the encounter through the OTHER close.
    monkeypatch.setattr("sidequest.server.dispatch.damage_roll.random.randint", lambda a, b: a)

    events = _capture_dice_watcher(monkeypatch)
    _throw(snap, enc, pack, face=2)  # 2 + mods < DC → plain Fail, no nat-1

    assert opponent_core.hp.current == 3, "a plain miss with no shock must not ablate"
    beat_events = [e for e in events if e.get("op") == "beat_applied"]
    assert beat_events and beat_events[0]["opponent_hp_removed"] == 0
    assert beat_events[0].get("shock_hp_removed", 0) == 0
    assert not [e for e in events if e.get("op") == "shock_chip_applied"]
    assert snap.pending_resolution_signal is None
    truth = [d for d in snap.next_turn_directives if "MECHANICAL TRUTH" in d]
    assert not [d for d in truth if "RESOLVED" in d], (
        f"no resolution directive may fire on an unresolved encounter; got {truth}"
    )
