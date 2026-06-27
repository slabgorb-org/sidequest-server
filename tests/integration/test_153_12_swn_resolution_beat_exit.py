"""Story 153-12 (RED) — SWN ✦ resolution beat offers a non-lethal confrontation exit.

PLAYTEST BUG (aureate_span / space_opera, SWN — session 2026-06-21-aureate_span):
under ``hp_depletion`` combat a ✦-marked **resolution beat** (``retreat`` / "Fall
Back") that succeeds does NOT end the confrontation. The player commits Fall Back,
receives ``ENCOUNTER_BEAT_APPLIED beat=retreat tier=CritSuccess`` — and stays fully
trapped: no ``encounter.resolved`` fires, ``in_conflict`` stays True, the beat pool is
still offered, and the seated opponent keeps reprising. ✦ is decorative under WN.

ROOT CAUSE (verified): under a Without-Number binding + ``win_condition=hp_depletion``
``dice._apply_committed_player_beat`` routes EVERY committed beat through
``_resolve_wn_committed_action`` (ADR-143 — the native beat engine is REMOVED from the
WN combat path). That helper resolves ONLY via the strike→HP channel + ``check_hp_
depletion``; it has no resolution-beat branch. A ``push`` / ``retreat`` beat carries no
``strike`` damage channel, so ``hp_removed=0``, nobody hits 0 HP, ``resolved=False`` —
and the native ``apply_beat`` resolution branch (``beat_kinds.apply_beat``, "ungated by
hp_depletion ON PURPOSE", which sets ``enc.resolved`` for a resolution beat) never runs.

FIX DIRECTION (Dev): teach the WN-native path that a **succeeded** ✦ resolution beat
(Success / CritSuccess) ends the confrontation as a NON-LETHAL exit — the WN SRD
Disengage / Full Retreat result. Do NOT invent a native dial/lethality mechanic to gate
it (SOUL "Bind the Ruleset, Don't Balance It" / ADR-143); a **failed** attempt (Fail /
CritFail) leaves combat live. The end must be distinct from an hp-depletion win/loss —
the engagement dissolved, nobody was defeated — and must emit the ``encounter.resolved``
watcher span so the GM panel can tell a resolution-beat exit from an HP kill.

These tests drive the **REAL space_opera pack** through ``dispatch_dice_throw`` (the
production seam: find_confrontation "combat" → personal Firefight, ``beat_selection`` +
``win_condition: hp_depletion``). The sealed-round tests carry a persisted P4
initiative order (the production path the playtest hit); one test omits it to pin the
shared legacy-immediate path too, so the fix must live in the shared
``_resolve_wn_committed_action`` rather than only in ``run_wn_round``.

Skips gracefully when sidequest-content is not on disk. ``otel_capture`` is re-exported
from ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import re

import pytest

from tests._helpers.genre_paths import PackNotFound, find_pack_path

# Matches the capture: Sable (Officer, AC 13) vs Pelä-menäy (Vaal-Kesh agent, 48 HP).
PLAYER = "Sable"
OPPONENT = "Pela"
RETREAT_BEAT = "retreat"

# space_opera SWN attribute codes (canonical Without Number: STR/DEX/CON/INT/
# WIS/CHA). The ✦ retreat beat's ``stat_check`` is DEX.
SWN_STATS = {
    "STR": 10,
    "DEX": 12,
    "INT": 10,
    "WIS": 10,
    "CON": 10,
    "CHA": 10,
}

# d20 faces with tier guaranteed regardless of the beat DC (game/dice.py): nat 20 →
# CritSuccess, nat 1 → CritFail. The tier gate (AC1/AC2) is the subtle requirement;
# the crit extremes pin it unambiguously without depending on the computed DC.
CRIT_SUCCESS_FACE = 20
CRIT_FAIL_FACE = 1

# Under hp_depletion the dials are inert placeholders the init seam parks at a 1e6
# sentinel threshold; HP is the authoritative track. A correct non-lethal exit must
# come from the resolution-beat path, NOT a dial crossing (AC4).
HP_DEPLETION_SENTINEL = 1_000_000

# An hp-depletion win/loss outcome — the resolution-beat exit must NOT record one (AC3).
_HP_WIN_OUTCOMES = {"player_victory", "opponent_victory"}
# The non-lethal exit must MARK itself (AC5): the native engine stamps
# ``resolution_beat:<id>``; a faithful WN disengage label is equally acceptable.
_RESOLUTION_EXIT_RE = re.compile(r"resolution_beat|disengage|retreat|withdraw|fall.?back", re.I)


# ---------------------------------------------------------------------------
# Pack + fixture helpers (inline — no cross-test coupling)
# ---------------------------------------------------------------------------


def _load_space_opera_pack():
    from sidequest.genre.loader import load_genre_pack

    try:
        path = find_pack_path("space_opera")
    except PackNotFound:
        return None
    return load_genre_pack(path)


def _personal_combat_cdef(pack):
    """The REAL personal-combat (Firefight) ConfrontationDef (type ``combat``;
    the ship block is ``ship_combat``). None if the pack shape changes — fail loud."""
    return next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )


def _retreat_beat(cdef):
    """The authored ✦ Fall Back beat — id ``retreat``, kind ``push``, ``resolution: true``."""
    return next((b for b in cdef.beats if b.id == RETREAT_BEAT), None)


def _make_snapshot(*, player_hp: int = 20, opponent_hp: int = 48):
    """Snapshot with a player Character (armed, AC 13) and an opponent NPC at full HP.

    Both seeded with comfortable HP so the *only* way this confrontation can end is the
    resolution beat — a stray strike must not resolve it first, and the opponent's
    reprisal must not down the 20-HP player.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    player_core = CreatureCore(
        name=PLAYER,
        description="Station-side officer",
        personality="steady",
        inventory=Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}]),
        hp={"current": player_hp, "max": player_hp, "base_max": player_hp},
        armor_class=13,
    )
    player = Character(
        core=player_core,
        char_class="Officer",
        race="Coreworlder",
        backstory="Ex-Hegemonic command staff.",
        stats=dict(SWN_STATS),
    )
    opponent_core = CreatureCore(
        name=OPPONENT,
        description="Vaal-Kesh agent",
        personality="relentless",
        inventory=Inventory(),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=13,
    )

    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(player)
    snap.npcs.append(Npc(core=opponent_core))
    return snap


def _make_encounter(*, with_initiative: bool = True):
    """Firefight StructuredEncounter: player vs opponent, hp_depletion combat.

    ``with_initiative`` seats a persisted P4 order with the PLAYER first — the
    production sealed-round path the aureate_span playtest hit (and PC-first means a
    successful retreat resolves at the player's own slot, before the opponent acts).
    Omitting it exercises the legacy immediate-resolution path (no persisted order).
    """
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )
    from sidequest.protocol.models import InitiativeEntry

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=HP_DEPLETION_SENTINEL
        ),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=HP_DEPLETION_SENTINEL
        ),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=PLAYER, role="combatant", side="player"),
            EncounterActor(name=OPPONENT, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
        initiative=(
            [
                InitiativeEntry(token_id=PLAYER, value=9),
                InitiativeEntry(token_id=OPPONENT, value=3),
            ]
            if with_initiative
            else []
        ),
    )
    return enc


def _dispatch_retreat(snap, enc, pack, *, face: int, request_id: str = "retreat-req-1"):
    """Commit one ``retreat`` (Fall Back) Main Action through the production dice seam."""
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    return dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id=request_id,
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[face],
            beat_id=RETREAT_BEAT,
        ),
        rolling_player_id="player-sable",
        character_name=PLAYER,
        character_stats=dict(SWN_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="space_opera",
        session_id="retreat-session",
        round_number=1,
        room_broadcast=None,
        snapshot=snap,
    )


def _require_pack():
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")
    cdef = _personal_combat_cdef(pack)
    assert cdef is not None, "space_opera must expose a personal 'combat' (Firefight) confrontation"
    assert cdef.win_condition == "hp_depletion", (
        f"the Firefight must be hp_depletion combat (got {cdef.win_condition!r}) — this "
        "story's bug is scoped to the WN-native hp_depletion resolution path"
    )
    beat = _retreat_beat(cdef)
    assert beat is not None, (
        "space_opera Firefight must author the ✦ 'retreat' (Fall Back) resolution beat — "
        f"available beats: {[b.id for b in cdef.beats]}"
    )
    assert getattr(beat, "resolution", False) is True, (
        "the 'retreat' beat must carry resolution=true (the ✦ marker the engine reads to "
        "end the confrontation); without it the fix has no signal to gate on"
    )
    return pack


# ---------------------------------------------------------------------------
# AC1 — a SUCCEEDED ✦ resolution beat ends the confrontation (non-lethal exit).
# ---------------------------------------------------------------------------


def test_succeeded_resolution_beat_ends_confrontation():
    """AC1 / AC6: a CritSuccess Fall Back must END the confrontation. Today the
    WN-native path no-ops the push beat — ``enc.resolved`` stays False (RED)."""
    pack = _require_pack()
    snap = _make_snapshot()
    enc = _make_encounter(with_initiative=True)
    snap.encounter = enc
    assert enc.resolved is False, "precondition: the firefight is active before the throw"

    _dispatch_retreat(snap, enc, pack, face=CRIT_SUCCESS_FACE)

    assert enc.resolved is True, (
        "a succeeded ✦ resolution beat (Fall Back, CritSuccess) did NOT end the "
        "confrontation — enc.resolved is still False. The WN-native resolution path "
        "(_resolve_wn_committed_action) has no resolution-beat branch, so ✦ is decorative "
        f"under hp_depletion. enc.outcome={enc.outcome!r}"
    )


def test_succeeded_resolution_beat_closes_the_beat_pool():
    """AC1: once the exit fires the beat pool is no longer offered — a follow-up
    DICE_THROW on the now-resolved encounter is rejected (the player is out of combat).
    Today the encounter never resolves, so the re-throw is (wrongly) accepted (RED)."""
    from sidequest.server.dispatch.dice import DiceDispatchError

    pack = _require_pack()
    snap = _make_snapshot()
    enc = _make_encounter(with_initiative=True)
    snap.encounter = enc

    _dispatch_retreat(snap, enc, pack, face=CRIT_SUCCESS_FACE)

    assert enc.resolved is True, "precondition for the pool-closed check: the exit must fire"
    with pytest.raises(DiceDispatchError, match="active encounter"):
        _dispatch_retreat(snap, enc, pack, face=CRIT_SUCCESS_FACE, request_id="retreat-req-2")


# ---------------------------------------------------------------------------
# AC2 — a FAILED ✦ resolution beat leaves the confrontation active.
# ---------------------------------------------------------------------------


def test_failed_resolution_beat_keeps_confrontation_active():
    """AC2: a CritFail Fall Back must NOT end the confrontation — the player tried to
    disengage and could not; combat continues. The exit is gated on SUCCESS, NOT on the
    unconditional resolution=true flag (a faithful skill check, the ✦ beat has a DC)."""
    pack = _require_pack()
    snap = _make_snapshot()
    enc = _make_encounter(with_initiative=True)
    snap.encounter = enc

    _dispatch_retreat(snap, enc, pack, face=CRIT_FAIL_FACE)

    assert enc.resolved is False, (
        "a FAILED ✦ resolution beat (Fall Back, CritFail) ended the confrontation — the "
        "exit must be gated on a Success/CritSuccess outcome, not fire unconditionally on "
        f"the beat's resolution=true flag. enc.outcome={enc.outcome!r}"
    )


# ---------------------------------------------------------------------------
# AC3 / AC4 — the exit is NON-LETHAL and distinct from an hp-depletion win, and
# does NOT come from a native dial crossing.
# ---------------------------------------------------------------------------


def test_resolution_beat_exit_is_nonlethal_and_not_hp_depletion_win():
    """AC3 + AC4: the disengage ends the fight with NOBODY defeated — both combatants
    keep their HP — and the end is a resolution-beat exit, never an hp-depletion
    win/loss nor a dial-threshold crossing (no native dial was invented to gate it)."""
    pack = _require_pack()
    snap = _make_snapshot(player_hp=20, opponent_hp=48)
    enc = _make_encounter(with_initiative=True)
    snap.encounter = enc

    _dispatch_retreat(snap, enc, pack, face=CRIT_SUCCESS_FACE)

    assert enc.resolved is True, "precondition: the resolution beat must end the fight"

    # AC3 — non-lethal: nobody was defeated, both combatants still standing.
    pc_hp = snap.find_creature_core(PLAYER).hp.current
    opp_hp = snap.find_creature_core(OPPONENT).hp.current
    assert pc_hp > 0 and opp_hp > 0, (
        f"the disengage was not non-lethal — pc_hp={pc_hp}, opp_hp={opp_hp}. A resolution-beat "
        "exit dissolves the engagement; it must not down either combatant"
    )

    # AC3 — distinct from an hp-depletion win/loss for either party.
    outcome = enc.outcome or ""
    assert outcome not in _HP_WIN_OUTCOMES and "hp_depletion" not in outcome, (
        f"the resolution-beat exit recorded an hp-depletion win/loss outcome ({outcome!r}); "
        "the engagement dissolved — no combatant was defeated, so downstream handlers (loot, "
        "defeat consequences) must not see a win-condition end"
    )
    # AC5/AC3 — the exit marks itself as a resolution-beat / disengage (not a bare/blank end).
    assert _RESOLUTION_EXIT_RE.search(outcome), (
        f"the confrontation-end outcome {outcome!r} does not mark the resolution-beat exit "
        "path — the GM panel cannot distinguish a Fall Back disengage from any other end"
    )

    # AC4 — no native dial invented: the inert hp_depletion dials never moved or crossed.
    assert enc.player_metric.current == 0 and enc.opponent_metric.current == 0, (
        "a dial was advanced to force the exit — the hp_depletion dials are inert "
        "placeholders and the resolution-beat exit must NOT move them (ADR-143: bind the "
        f"ruleset, don't invent a dial). player={enc.player_metric.current}, "
        f"opponent={enc.opponent_metric.current}"
    )
    assert (
        enc.player_metric.current < enc.player_metric.threshold
        and enc.opponent_metric.current < enc.opponent_metric.threshold
    ), "the exit must not be a dial-threshold crossing — it is a resolution-beat disengage"


# ---------------------------------------------------------------------------
# AC5 — observability: the non-lethal exit emits the encounter.resolved span.
# ---------------------------------------------------------------------------


def test_resolution_beat_exit_emits_resolved_span(otel_capture):
    """AC5: the confrontation-end via a succeeded resolution beat must emit the
    ``encounter.resolved`` watcher span carrying a non-lethal / resolution-beat outcome,
    so the GM panel can tell a Fall Back disengage from an hp-depletion kill. The
    ``encounter.beat_applied`` span (which fired in the playtest) is asserted as a
    precondition that the beat path is live."""
    pack = _require_pack()
    snap = _make_snapshot()
    enc = _make_encounter(with_initiative=True)
    snap.encounter = enc

    _dispatch_retreat(snap, enc, pack, face=CRIT_SUCCESS_FACE)

    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "encounter.beat_applied" in names, (
        f"precondition: the retreat beat must apply through dispatch (the playtest saw "
        f"ENCOUNTER_BEAT_APPLIED) — spans: {names}"
    )

    resolved_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "encounter.resolved"
    ]
    assert resolved_spans, (
        "the succeeded ✦ resolution beat ended the confrontation WITHOUT emitting an "
        "'encounter.resolved' span — the GM panel (the lie detector) cannot see the "
        f"non-lethal exit fired (CLAUDE.md OTEL Observability Principle). spans: {names}"
    )
    outcome = (resolved_spans[-1].attributes or {}).get("outcome", "")
    assert _RESOLUTION_EXIT_RE.search(str(outcome)) and "hp_depletion" not in str(outcome), (
        f"the encounter.resolved span outcome {outcome!r} does not mark the resolution-beat "
        "exit path — it must be distinguishable from an hp-depletion win on the GM panel"
    )


# ---------------------------------------------------------------------------
# AC6 — the fix lives in the SHARED WN-native seam: the legacy immediate-resolution
# path (no persisted initiative) must exit too, not just the sealed round walk.
# ---------------------------------------------------------------------------


def test_succeeded_resolution_beat_exits_on_legacy_immediate_path():
    """AC6 (shared seam): a retreat with NO persisted initiative resolves on the legacy
    immediate path (_apply_committed_player_beat called directly, not via run_wn_round).
    It must end the confrontation too — proving the fix belongs in the shared
    ``_resolve_wn_committed_action`` and not only in the sealed-round walk."""
    pack = _require_pack()
    snap = _make_snapshot()
    enc = _make_encounter(with_initiative=False)
    snap.encounter = enc
    assert not enc.initiative, "precondition: legacy immediate path requires no persisted order"

    _dispatch_retreat(snap, enc, pack, face=CRIT_SUCCESS_FACE)

    assert enc.resolved is True, (
        "a succeeded ✦ resolution beat on the legacy immediate path did NOT end the "
        "confrontation — the resolution-beat exit must live in the shared WN-native helper "
        "(_resolve_wn_committed_action), reachable from BOTH the sealed round and the "
        f"immediate dispatch. enc.outcome={enc.outcome!r}"
    )
