"""Story 166-1 (RED) — flee beat + lethal opponent reprisal double-resolve the same round.

Playtest 2026-07-10 (mutant_wasteland/flickering_reach, slug f69d51db, turn 9,
"Wasteland Brawl" vs "Understory Hand"): Harpo committed a Flee beat at 2 HP; the
opponent's reprisal dropped him to 0 and resolved the encounter
``opponent_victory`` (lethality verdict=dead) — yet the settled turn narrated a
successful escape ("You are alive. Just barely, and wrongly, but alive.") over the
"Harpo has fallen" banner, the Flee's location move committed
(``character_locations`` → "Blind Reach — Canyon Rim"), and top-level
``player_dead`` stayed False. ADR-139 (Confrontation Integrity — win-condition
liveness, seated-actor HP durability, currently *partial*).

Forensic reconstruction (this suite's map of the production seams):

- The sealed WN round walk (``run_wn_round``) already SKIPS a downed player's
  slot (``wn_round.slot_skipped reason=actor_downed``) — the engine never
  double-applies the flee. But the skip is silent to the NARRATOR: unlike the
  ``dead_premise`` branch it appends no hint, and the dispatch replay text still
  carries "Flee → CritSuccess", so the narrator honors both the kill and the
  escape. The skip event also carries no ``beat_id``, so the GM panel cannot see
  WHICH committed beat was blocked (OTEL principle).
- On the LEGACY immediate-reprisal path (no persisted initiative — AWN warns and
  proceeds), the post-reprisal ``_emit_player_beat_resolution_close`` fires the
  failed-strike anchor for the 0-damage flee and appends "...STILL STANDING; the
  fight continues" AFTER the reprisal close already appended "the confrontation
  has RESOLVED" — two directly contradictory MECHANICAL TRUTH directives in one
  narrator prompt.
- ``GameSnapshot.player_dead`` ("P1-required: permadeath / death detection") is
  never written by ANY production path, and ``combat_player_dead_span``
  (``combat.player_dead``, telemetry/spans/combat.py) has no callers — the death
  flag desync the playtest observed is structural.
- ``_apply_narration_result_to_snapshot`` commits ``result.location`` with no
  death gate — the narrator's escape prose moved a mechanically dead PC.

Acceptance criteria under test (session 166-1):
  AC-1 liveness gate observable  → test_sealed_walk_blocked_flee_emits_gate_event_with_beat_id
  AC-4 narrator truth (sealed)   → test_sealed_walk_skipped_flee_surfaces_mechanical_truth_to_narrator
  AC-3 player_dead wiring (e2e)  → test_reprisal_kill_sets_player_dead_flag_end_to_end
  AC-3 player_dead wiring (unit) → test_lethal_verdict_sets_player_dead_and_emits_span
  AC-2 flee location guard       → test_dead_pc_location_move_does_not_commit
  AC-4 narrator truth (legacy)   → test_legacy_reprisal_kill_appends_no_fight_continues_directive
  AC-5 regression guard          → test_player_victory_clean_path_stays_coherent (GREEN today)

DETERMINISM: one arg-dispatching randint fake governs the whole path (dice.py,
wn_round.py, downed_seam.py and damage_roll.py all roll via the shared ``random``
module): (1, 20) → 20 — the reprisal/opponent to-hit always HITS (PC AC is 10);
every other range → its MINIMUM — the opponent's damage die deals its floor
(≥ 1, exactly enough to drop the 1-HP PC), trauma dice stay low. The player's own
d20 uses the thrown ``face=[20]``, never rng.

Fixture lineage: seating + dispatch shape from
tests/integration/test_reprisal_wn_lethality_e2e.py (heavy_metal, story 102-1);
narration-apply harness from tests/server/test_confrontation_location_change.py.
Skips cleanly when sidequest-content is not on disk. ``otel_capture`` is
re-exported from tests/integration/conftest.py.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

PLAYER = "Harpo"
OPPONENT = "Understory Hand"
GENRE = "mutant_wasteland"
ENCOUNTER_TYPE = "combat"  # rules.yaml: label "Wasteland Brawl", win_condition hp_depletion
FLEE_BEAT = "flee"  # authored cdef beat (kind: push, stat DEX, NO resolution marker)
ATTACK_BEAT = "attack"  # WN synthesized action (beat_filter.WN_ATTACK_BEAT_ID)

SPAN_COMBAT_PLAYER_DEAD = "combat.player_dead"

_NOT_RESOLVE_MARKERS = ("not resolve", "never resolved", "did not happen")
_FIGHT_CONTINUES_MARKERS = ("STILL STANDING", "the fight continues")


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_content(), reason="sidequest-content not on disk"
)


def _load_mutant_wasteland():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path(GENRE))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _make_pc(name: str, *, hp_current: int, hp_max: int = 10, armed: bool = False):
    """A wastelander at AC 10. ``hp_current=1`` puts them one reprisal floor-roll
    from death (the playtest shape); ``armed=True`` equips a blade whose +50
    bonus guarantees the player's own strike kills any seated Other (AC-5
    guard) — damage dice roll their forced minimum, the bonus does the work."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    items = []
    if armed:
        items.append(
            {
                "id": "scrap_cleaver",
                "name": "Scrap Cleaver",
                "category": "weapon",
                "equipped": True,
                "damage": {"dice": "2d6", "bonus": 50},
            }
        )
    core = CreatureCore(
        name=name,
        description="A scavenger of the Blind Reach.",
        personality="wary",
        inventory=Inventory(items=items),
        hp={"current": hp_current, "max": hp_max, "base_max": hp_max},
        armor_class=10,
    )
    return Character(
        core=core,
        char_class="Scavenger",
        race="Human",
        backstory="—",
        stats={"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
    )


_PC_STATS = {"STR": 12, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10}


def _make_opponent_npc():
    """The Understory Hand as a ROSTER NPC (the playtest shape: a scene-active
    antagonist, HP 9 — the session showed 'Them 3/9'). Seating resolves it via
    ``resolve_roster_npc`` instead of falling through to bestiary generics
    (flickering_reach authors none) and refusing to fabricate."""
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import Npc

    core = CreatureCore(
        name=OPPONENT,
        description="A tendril-limbed scavenger of the understory.",
        personality="predatory",
        inventory=Inventory(),
        hp={"current": 9, "max": 9, "base_max": 9},
        armor_class=12,
    )
    return Npc(core=core)


def _reprisal_hits_all_else_min(a: int, b: int) -> int:
    """(1, 20) → 20 (opponent to-hit always HITS); anything else → minimum."""
    return 20 if (a, b) == (1, 20) else a


def _seat_brawl(snap, pack):
    """Seat the real Wasteland Brawl through the production seating seam."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=ENCOUNTER_TYPE,
        player_name=PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, side="opponent")],
        genre_slug=GENRE,
    )
    assert enc is not None, "seating the Wasteland Brawl must produce an encounter"
    snap.encounter = enc
    return enc


def _snapshot_with_pc(*, hp_current: int, armed: bool = False):
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    snap = GameSnapshot(
        genre_slug=GENRE,
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=9),
    )
    snap.characters.append(_make_pc(PLAYER, hp_current=hp_current, armed=armed))
    snap.npcs.append(_make_opponent_npc())
    snap.character_locations[PLAYER] = "Blind Reach — The Crack"
    return snap


def _dispatch(snap, enc, pack, *, beat_id: str, request_id: str):
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
            face=[20],
            beat_id=beat_id,
        ),
        rolling_player_id="player-harpo",
        character_name=PLAYER,
        character_stats=dict(_PC_STATS),
        encounter=enc,
        pack=pack,
        genre_slug=GENRE,
        session_id="mw-166-1-session",
        round_number=9,
        room_broadcast=None,
        snapshot=snap,
    )


def _pin_initiative_opponent_first(enc):
    """The playtest shape: the Understory Hand acts before Harpo — the reprisal
    kills at the opponent's slot, then the walk reaches the dead fleer's slot."""
    from sidequest.protocol.models import InitiativeEntry

    enc.initiative = [
        InitiativeEntry(token_id=OPPONENT, value=12),
        InitiativeEntry(token_id=PLAYER, value=9),
    ]


def _install_watcher_recorder(monkeypatch) -> list[dict]:
    """Record every watcher payload published by the dice + wn_round seams while
    still forwarding to the real publisher. Patched where USED (each module binds
    ``publish_event`` as ``_watcher_publish`` at import time)."""
    import sidequest.server.dispatch.dice as dice_module
    import sidequest.server.dispatch.wn_round as wn_round_module

    events: list[dict] = []

    def _make_recorder(original):
        def _recording_publish(event_type, payload, **kwargs):
            events.append(dict(payload))
            return original(event_type, payload, **kwargs)

        return _recording_publish

    monkeypatch.setattr(
        wn_round_module, "_watcher_publish", _make_recorder(wn_round_module._watcher_publish)
    )
    monkeypatch.setattr(
        dice_module, "_watcher_publish", _make_recorder(dice_module._watcher_publish)
    )
    return events


def _assert_kill_preconditions(snap, enc):
    """The parts of the playtest sequence that already work: the reprisal kill
    resolves the fight against the player and the lethal verdict lands."""
    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current <= 0, (
        f"precondition: the forced-hit reprisal must drop the 1-HP PC; "
        f"hp={player_core.hp.current}"
    )
    assert enc.resolved and enc.outcome == "opponent_victory", (
        f"precondition: the kill must resolve against the player; "
        f"resolved={enc.resolved} outcome={enc.outcome}"
    )
    assert any(
        s.text.startswith("Downed") and s.incapacitating for s in player_core.statuses
    ), (
        f"precondition: mutant_wasteland's pc=dead verdict must stamp the "
        f"incapacitating Downed status; statuses={[s.text for s in player_core.statuses]}"
    )
    return player_core


# ---------------------------------------------------------------------------
# AC-1 — the liveness gate must be observable on the GM panel (beat_id on the
# blocked/skipped commit event).
# ---------------------------------------------------------------------------


def test_sealed_walk_blocked_flee_emits_gate_event_with_beat_id(monkeypatch):
    """When the sealed round walk refuses a downed player's committed beat, the
    watcher event must identify WHICH beat was blocked (``beat_id``) — the GM
    panel is the lie detector, and an anonymous skip cannot prove the liveness
    gate engaged against the committed Flee (OTEL Observability Principle).

    Today ``wn_slot_skipped_downed`` fires with only the actor — no beat_id."""
    pack = _load_mutant_wasteland()
    assert (
        pack.lethality_policy is not None
        and pack.lethality_policy.verdicts_on_zero_hp.pc == "dead"
    ), "mutant_wasteland's lethality policy must verdict a 0-HP PC `dead`"

    snap = _snapshot_with_pc(hp_current=1)
    enc = _seat_brawl(snap, pack)
    _pin_initiative_opponent_first(enc)
    events = _install_watcher_recorder(monkeypatch)
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    _dispatch(snap, enc, pack, beat_id=FLEE_BEAT, request_id="mw-166-1-gate-event")

    _assert_kill_preconditions(snap, enc)

    gate_events = [
        ev
        for ev in events
        if ev.get("actor") == PLAYER
        and ev.get("beat_id") == FLEE_BEAT
        and any(k in str(ev.get("op", "")) for k in ("skip", "block", "liveness"))
    ]
    assert gate_events, (
        "the liveness gate that refused Harpo's committed Flee must emit a watcher "
        "event carrying the blocked beat_id='flee' (op naming a skip/block/liveness "
        "decision) so the GM panel can verify the gate engaged; captured player-slot "
        f"events: {[ev for ev in events if ev.get('actor') == PLAYER]}"
    )


# ---------------------------------------------------------------------------
# AC-4 (sealed path) — the narrator must be told the committed flee never
# resolved; silence here is what produced "You are alive" over a corpse.
# ---------------------------------------------------------------------------


def test_sealed_walk_skipped_flee_surfaces_mechanical_truth_to_narrator(monkeypatch):
    """A committed beat whose slot was refused (actor down before their slot)
    must surface that refusal to the NARRATOR — the same contract the walk's
    ``dead_premise`` branch already honors ("The action did NOT resolve
    mechanically"). Without it the dispatch replay text ("Flee → CritSuccess")
    is the only story the narrator hears about the flee, and it narrates a
    successful escape over a 0-HP corpse (the 2026-07-10 playtest prose).

    Today the ``actor_downed`` skip appends NO narrator hint and NO directive."""
    pack = _load_mutant_wasteland()
    snap = _snapshot_with_pc(hp_current=1)
    enc = _seat_brawl(snap, pack)
    _pin_initiative_opponent_first(enc)
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    _dispatch(snap, enc, pack, beat_id=FLEE_BEAT, request_id="mw-166-1-narrator-truth")

    _assert_kill_preconditions(snap, enc)

    # Review rework R5: key on the structural "LIVENESS GATE" prefix (stable
    # vocabulary) OR the negation markers, so a legitimate rewording of the
    # prose tail doesn't false-fail — the load-bearing contract is that the
    # hint names the actor + the blocked beat and marks it a gate refusal.
    narrator_surface = list(enc.narrator_hints) + list(snap.next_turn_directives)
    truth_lines = [
        t
        for t in narrator_surface
        if PLAYER in t
        and FLEE_BEAT in t.lower()
        and (
            "liveness gate" in t.lower()
            or any(marker in t.lower() for marker in _NOT_RESOLVE_MARKERS)
        )
    ]
    assert truth_lines, (
        "the narrator surface (encounter.narrator_hints / next_turn_directives) "
        "must carry the mechanical truth that Harpo's committed Flee did NOT "
        "resolve (he was down before his slot) — otherwise the replay text's "
        "'Flee → CritSuccess' stands unopposed and the prose narrates an escape "
        f"over a corpse; got hints={list(enc.narrator_hints)!r} "
        f"directives={list(snap.next_turn_directives)!r}"
    )


# ---------------------------------------------------------------------------
# AC-3 — player_dead flag wiring (end-to-end and unit). The field exists
# (GameSnapshot.player_dead, "P1-required") and the span helper exists
# (combat_player_dead_span) — nothing in production writes either.
# ---------------------------------------------------------------------------


def test_reprisal_kill_sets_player_dead_flag_end_to_end(otel_capture, monkeypatch):
    """The playtest's settled save had status='Downed — dead (mortally wounded)'
    and ``player_dead=False`` in the same snapshot. When the reprisal kill takes
    the last (solo) PC out under a LETHAL genre verdict, the top-level death
    flag must flip — and ``combat.player_dead`` must fire so the GM panel can
    see the death-detection decision (the span route exists, unwired)."""
    pack = _load_mutant_wasteland()
    snap = _snapshot_with_pc(hp_current=1)
    enc = _seat_brawl(snap, pack)
    _pin_initiative_opponent_first(enc)
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    _dispatch(snap, enc, pack, beat_id=FLEE_BEAT, request_id="mw-166-1-player-dead")

    _assert_kill_preconditions(snap, enc)

    assert snap.player_dead is True, (
        "a lethal reprisal kill of the solo PC (verdict=dead, incapacitating "
        "Downed status stamped) must set GameSnapshot.player_dead — the playtest "
        "save carried a mortally-wounded corpse with player_dead=False"
    )
    dead_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_COMBAT_PLAYER_DEAD
    ]
    assert len(dead_spans) == 1, (
        f"exactly one combat.player_dead span must record the death-detection "
        f"decision; got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert dict(dead_spans[0].attributes or {}).get("player_name") == PLAYER, (
        f"the combat.player_dead span must name the dead PC; "
        f"attrs={dict(dead_spans[0].attributes or {})}"
    )


def test_lethal_verdict_sets_player_dead_and_emits_span(otel_capture):
    """Unit shape of the same contract, on the seam that owns the verdict:
    ``apply_post_resolution_lethality`` with a LETHAL genre verdict downing the
    last player-side PC must set ``snapshot.player_dead`` and emit
    ``combat.player_dead``. (Solo scope — one seated PC. Multi-seat semantics
    are a design question logged in the session Delivery Findings.)"""
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.post_resolution_lethality import (
        apply_post_resolution_lethality,
    )

    pack = _load_mutant_wasteland()
    snap = GameSnapshot(
        genre_slug=GENRE,
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=9),
    )
    snap.characters.append(_make_pc(PLAYER, hp_current=0))
    enc = StructuredEncounter(
        encounter_type=ENCOUNTER_TYPE,
        resolved=True,
        outcome="opponent_victory",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name=PLAYER, role="brawler", side="player"),
            EncounterActor(name=OPPONENT, role="opposition", side="opponent"),
        ],
    )

    incapacitations = apply_post_resolution_lethality(
        snapshot=snap, encounter=enc, pack=pack, turn=9
    )

    assert [e.actor for e in incapacitations] == [PLAYER], (
        f"precondition: the lethal verdict must take the 0-HP PC out; "
        f"got {incapacitations!r}"
    )
    assert snap.player_dead is True, (
        "apply_post_resolution_lethality ruled the solo PC dead "
        "(decision=lethal_down, verdict=dead) but never set snapshot.player_dead "
        "— the flag exists on GameSnapshot ('P1-required: permadeath / death "
        "detection') and no production path writes it"
    )
    dead_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_COMBAT_PLAYER_DEAD
    ]
    assert len(dead_spans) == 1 and dict(dead_spans[0].attributes or {}).get(
        "player_name"
    ) == PLAYER, (
        f"combat.player_dead must fire once for the dead PC (the span helper and "
        f"its GM-panel route exist with zero callers); got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )


# ---------------------------------------------------------------------------
# AC-2 — a dead PC's location must not move. The playtest save had the corpse
# relocated to "Blind Reach — Canyon Rim" by the narrator's escape prose.
# ---------------------------------------------------------------------------


def test_dead_pc_location_move_does_not_commit():
    """``_apply_narration_result_to_snapshot`` commits ``result.location``
    unconditionally. When the acting PC is mechanically dead (0 HP, an
    incapacitating Downed status), a narrator-emitted location change is
    escape-prose confabulation and must be REFUSED (loud skip, not a silent
    fallback) — the corpse stays where it fell."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        StructuredEncounter,
    )
    from sidequest.game.session import GameSnapshot
    from sidequest.game.status import Status, StatusSeverity
    from sidequest.game.turn import TurnManager
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    pack = _load_mutant_wasteland()
    snap = GameSnapshot(
        genre_slug=GENRE,
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=9),
    )
    pc = _make_pc(PLAYER, hp_current=0)
    pc.core.statuses.append(
        Status(
            text="Downed — dead (mortally wounded)",
            severity=StatusSeverity.Scar,
            created_turn=9,
            created_in_encounter=ENCOUNTER_TYPE,
            incapacitating=True,
        )
    )
    snap.characters.append(pc)
    died_at = "Blind Reach — The Crack"
    snap.character_locations[PLAYER] = died_at
    # The playtest's settled shape: the encounter is already resolved against
    # the player when the narration turn applies.
    snap.encounter = StructuredEncounter(
        encounter_type=ENCOUNTER_TYPE,
        resolved=True,
        outcome="opponent_victory",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name=PLAYER, role="brawler", side="player"),
            EncounterActor(name=OPPONENT, role="opposition", side="opponent"),
        ],
    )

    result = NarrationTurnResult(
        narration=(
            "You climb anyway — hand over bleeding hand — until the crack exhales "
            "hot canyon air and spits you out onto sun-blasted rock above the rift."
        ),
        location="Blind Reach — Canyon Rim",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name=PLAYER,
        acting_character_name=PLAYER,
        room=room_for(snapshot=snap, slug="flickering_reach"),
    )

    assert snap.character_locations[PLAYER] == died_at, (
        "a mechanically dead PC (0 HP, incapacitating Downed status) must not be "
        "relocated by narrator prose — the flee's escape move committed on the "
        f"corpse in the 2026-07-10 playtest; expected {died_at!r}, got "
        f"{snap.character_locations[PLAYER]!r}"
    )


# ---------------------------------------------------------------------------
# AC-4 (legacy path) — no "the fight continues" directive may survive a
# same-dispatch reprisal resolution.
# ---------------------------------------------------------------------------


def test_legacy_reprisal_kill_appends_no_fight_continues_directive(monkeypatch):
    """On the legacy immediate-reprisal path (no persisted initiative — AWN
    proceeds with a loud warning), the player's 0-damage flee applies first,
    the reprisal then kills and appends 'the confrontation has RESOLVED'; the
    per-beat resolution close runs LAST and appends the failed-strike anchor:
    '...UNHARMED ... STILL STANDING; the fight continues.' Two contradictory
    MECHANICAL TRUTH directives reach the same narrator prompt — the resolved
    fight must never also be described as continuing."""
    pack = _load_mutant_wasteland()
    snap = _snapshot_with_pc(hp_current=1)
    enc = _seat_brawl(snap, pack)
    # Force the legacy path: no persisted initiative order (the 71-21
    # immediate-reprisal rider; WWN would raise here, AWN warns and proceeds).
    enc.initiative = []
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    _dispatch(snap, enc, pack, beat_id=FLEE_BEAT, request_id="mw-166-1-legacy-close")

    _assert_kill_preconditions(snap, enc)
    opponent_core = snap.find_creature_core(OPPONENT)
    assert opponent_core is not None and opponent_core.hp.current > 0, (
        "precondition: the Other must be alive after the flee (the anchor branch "
        "only fires against a standing opponent)"
    )
    resolved_truth = [
        d for d in snap.next_turn_directives if "has RESOLVED" in d and "opponent_victory" in d
    ]
    assert resolved_truth, (
        f"precondition: the reprisal close must append the RESOLVED truth; "
        f"directives={list(snap.next_turn_directives)!r}"
    )

    contradictions = [
        d
        for d in snap.next_turn_directives
        if any(marker in d for marker in _FIGHT_CONTINUES_MARKERS)
    ]
    assert not contradictions, (
        "no directive may claim the fight continues after the same dispatch's "
        "reprisal resolved it (opponent_victory, PC dead) — the narrator receives "
        "both lines and honors both ('You are alive ... wrongly, but alive'); "
        f"contradictory directives: {contradictions!r}"
    )


# ---------------------------------------------------------------------------
# AC-5 — regression guard: the clean player_victory path is coherent today
# (pingpong [DIAG] 2026-07-10) and must stay that way under the fix.
# ---------------------------------------------------------------------------


def test_player_victory_clean_path_stays_coherent(monkeypatch):
    """GREEN today by design (the pingpong [DIAG] isolated the defect to the
    loss path). Locks the clean-win contract the fix must not disturb: the
    player's killing strike resolves player_victory, no reprisal answers a
    resolved fight, the PC is untouched and alive, ``player_dead`` stays False,
    and no directive contradicts the resolution."""
    pack = _load_mutant_wasteland()
    snap = _snapshot_with_pc(hp_current=8, armed=True)
    enc = _seat_brawl(snap, pack)
    # Same legacy shape as the loss-path twin above, for a symmetric guard.
    enc.initiative = []
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    _dispatch(snap, enc, pack, beat_id=ATTACK_BEAT, request_id="mw-166-1-clean-win")

    assert enc.resolved and enc.outcome == "player_victory", (
        f"the +50-bonus strike must resolve the brawl for the player; "
        f"resolved={enc.resolved} outcome={enc.outcome}"
    )
    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current == 8, (
        f"no reprisal may answer a fight the player's own beat already resolved "
        f"(ADR-139 win-condition liveness); PC hp={player_core.hp.current}/10"
    )
    assert not any(s.text.startswith("Downed") for s in player_core.statuses), (
        f"the winning PC must carry no Downed status; "
        f"statuses={[s.text for s in player_core.statuses]}"
    )
    assert snap.player_dead is False, (
        "player_victory must never flip player_dead — the death flag is for the "
        "loss path only"
    )
    resolved_truth = [
        d for d in snap.next_turn_directives if "has RESOLVED" in d and "player_victory" in d
    ]
    assert resolved_truth, (
        f"the beat-resolution close must hand the narrator the RESOLVED truth; "
        f"directives={list(snap.next_turn_directives)!r}"
    )
    contradictions = [
        d
        for d in snap.next_turn_directives
        if any(marker in d for marker in _FIGHT_CONTINUES_MARKERS)
    ]
    assert not contradictions, (
        f"a resolved clean win must not also be described as continuing; "
        f"contradictory directives: {contradictions!r}"
    )


# ===========================================================================
# Review rework round 1 (Reviewer findings R1–R3, R5 — 2026-07-11).
#
# R2 is the round's true RED: the actor_downed LIVENESS GATE hint claims
# "the fight's close" unconditionally, which is FALSE in an MP partial-down
# (ADR-139: one downed PC ≠ party defeat — the fight stays live for the
# survivor). R1 and R3 are labeled COVERAGE PINS on shipped-but-unasserted
# behavior (the wn_slot_skipped_encounter_resolved branch and the location
# gate's AND-conjunction boundaries) — sanctioned add-tests-for-existing-code,
# passing at birth by design, per the 165-5 pins-vs-RED discipline.
# ===========================================================================

SECOND_PC = "Chico"


def _add_second_pc(snap, enc, *, name: str = SECOND_PC, hp_current: int = 10, armed: bool = False):
    """Seat a second player-side PC beside the fixture's first: a character in
    the snapshot (so the commit barrier waits on them) and an EncounterActor on
    the encounter (so the walk gives them a slot)."""
    from sidequest.game.encounter import EncounterActor

    snap.characters.append(_make_pc(name, hp_current=hp_current, armed=armed))
    enc.actors.append(EncounterActor(name=name, role="brawler", side="player"))


def test_partial_down_hint_must_not_claim_fight_close_while_live(monkeypatch):
    """R2 (RED this round): MP partial-down — the opponent kills 1-HP Harpo at
    its slot, but Chico stands, so per ADR-139 the fight does NOT resolve
    (hp_depletion.partial_down). Harpo's skipped slot still gets the LIVENESS
    GATE hint — and that hint must not instruct the narrator to 'narrate ...
    the fight's close' over an encounter the engine keeps LIVE. Today the tail
    is unconditional → this fails."""
    from sidequest.protocol.models import InitiativeEntry

    pack = _load_mutant_wasteland()
    snap = _snapshot_with_pc(hp_current=1)
    enc = _seat_brawl(snap, pack)
    _add_second_pc(snap, enc, hp_current=10)
    # Opponent first (kills Harpo), then the dead man's slot, then Chico.
    enc.initiative = [
        InitiativeEntry(token_id=OPPONENT, value=12),
        InitiativeEntry(token_id=PLAYER, value=9),
        InitiativeEntry(token_id=SECOND_PC, value=5),
    ]
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    # Harpo seals his flee; the barrier stays open until Chico commits, and
    # Chico's dispatch closes it and walks the round.
    _dispatch(snap, enc, pack, beat_id=FLEE_BEAT, request_id="mw-166-1-r2-seal-harpo")
    _dispatch(snap, enc, pack, beat_id=ATTACK_BEAT, request_id="mw-166-1-r2-seal-chico")

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None and player_core.hp.current <= 0, (
        f"precondition: the reprisal must down 1-HP Harpo; hp={player_core.hp.current}"
    )
    assert enc.resolved is False, (
        "precondition (ADR-139 partial-down): Chico stands, so one downed PC "
        f"must NOT resolve the fight; resolved={enc.resolved} outcome={enc.outcome!r}"
    )
    harpo_gate_hints = [
        h for h in enc.narrator_hints if "LIVENESS GATE" in h and PLAYER in h
    ]
    assert harpo_gate_hints, (
        f"precondition: the downed fleer's skipped slot must still hint the "
        f"narrator; hints={list(enc.narrator_hints)!r}"
    )
    lying_hints = [h for h in harpo_gate_hints if "fight's close" in h]
    assert not lying_hints, (
        "the LIVENESS GATE hint must not claim 'the fight's close' while the "
        "encounter is LIVE (Chico fights on — ADR-139 one-down≠party-defeat); "
        f"misdirecting hints: {lying_hints!r}"
    )


def test_resolved_slot_skip_emits_event_and_hint_for_live_pc(monkeypatch):
    """R1 (coverage pin — passes at birth): the wn_slot_skipped_encounter_resolved
    branch. Armed Harpo kills the Understory Hand at the FIRST slot
    (player_victory); live Chico's sealed flee arrives at a later slot in a
    resolved fight → the liveness gate must block it OBSERVABLY: watcher event
    with Chico's beat_id + a LIVENESS GATE narrator hint. Pins the branch the
    first round shipped untested (Reviewer R1)."""
    from sidequest.protocol.models import InitiativeEntry

    pack = _load_mutant_wasteland()
    snap = _snapshot_with_pc(hp_current=8, armed=True)
    enc = _seat_brawl(snap, pack)
    _add_second_pc(snap, enc, hp_current=10)
    # Harpo first (kills the Hand), Chico's live slot second, opponent last.
    enc.initiative = [
        InitiativeEntry(token_id=PLAYER, value=12),
        InitiativeEntry(token_id=SECOND_PC, value=9),
        InitiativeEntry(token_id=OPPONENT, value=5),
    ]
    events = _install_watcher_recorder(monkeypatch)
    monkeypatch.setattr(
        "sidequest.server.dispatch.dice.random.randint", _reprisal_hits_all_else_min
    )

    _dispatch(snap, enc, pack, beat_id=ATTACK_BEAT, request_id="mw-166-1-r1-seal-harpo")
    _dispatch(snap, enc, pack, beat_id=FLEE_BEAT, request_id="mw-166-1-r1-seal-chico")

    assert enc.resolved and enc.outcome == "player_victory", (
        f"precondition: Harpo's +50 strike must resolve the brawl at his slot; "
        f"resolved={enc.resolved} outcome={enc.outcome!r}"
    )
    chico_core = snap.find_creature_core(SECOND_PC)
    assert chico_core is not None and chico_core.hp.current == 10, (
        f"precondition: Chico must be alive and untouched; hp={chico_core.hp.current}"
    )
    gate_events = [
        ev
        for ev in events
        if ev.get("op") == "wn_slot_skipped_encounter_resolved"
        and ev.get("actor") == SECOND_PC
        and ev.get("beat_id") == FLEE_BEAT
    ]
    assert len(gate_events) == 1, (
        "a LIVE PC's committed beat blocked by prior resolution must emit exactly "
        "one wn_slot_skipped_encounter_resolved event carrying the blocked "
        f"beat_id; captured: {[ev for ev in events if ev.get('actor') == SECOND_PC]}"
    )
    chico_hints = [
        h
        for h in enc.narrator_hints
        if "LIVENESS GATE" in h and SECOND_PC in h and FLEE_BEAT in h.lower()
    ]
    assert chico_hints, (
        f"the narrator must be told Chico's committed flee did not resolve; "
        f"hints={list(enc.narrator_hints)!r}"
    )


def test_location_gate_allows_move_for_incapacitated_but_alive_pc():
    """R3a (coverage pin): the dead-PC location gate requires the DURABLE death
    state — hp<=0 AND an incapacitating status. An incapacitated-but-ALIVE PC
    (unconscious, being carried by the party) is a legitimate narrator move and
    must NOT be refused. Pins the AND against a future loosening to OR."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.session import GameSnapshot
    from sidequest.game.status import Status, StatusSeverity
    from sidequest.game.turn import TurnManager
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    pack = _load_mutant_wasteland()
    snap = GameSnapshot(
        genre_slug=GENRE,
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=9),
    )
    pc = _make_pc(PLAYER, hp_current=8)
    pc.core.statuses.append(
        Status(
            text="Unconscious — knocked out cold",
            severity=StatusSeverity.Wound,
            created_turn=9,
            created_in_encounter=ENCOUNTER_TYPE,
            incapacitating=True,
        )
    )
    snap.characters.append(pc)
    snap.character_locations[PLAYER] = "Blind Reach — The Crack"

    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="The others haul the limp scavenger up over the rim.",
            location="Blind Reach — Canyon Rim",
        ),
        pack=pack,
        player_name=PLAYER,
        acting_character_name=PLAYER,
        room=room_for(snapshot=snap, slug="flickering_reach"),
    )

    assert snap.character_locations[PLAYER] == "Blind Reach — Canyon Rim", (
        "an incapacitated-but-ALIVE PC (hp 8/10) may be moved by narrator prose "
        "(carried by the party) — the death gate must require hp<=0 AND the "
        f"incapacitating status, not either alone; got "
        f"{snap.character_locations[PLAYER]!r}"
    )


def test_location_gate_allows_move_for_zero_hp_without_status():
    """R3b (coverage pin): hp=0 with NO incapacitating status is a transient
    mid-resolution shape, not the durable death state — every lethal down
    stamps the incapacitating Downed status in the same dispatch before
    narration applies, and a non-lethal verdict recovers the PC to the 1-HP
    floor. The gate must not fire on the transient alone (the conservative AND
    contract the Reviewer asked pinned)."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    pack = _load_mutant_wasteland()
    snap = GameSnapshot(
        genre_slug=GENRE,
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=9),
    )
    snap.characters.append(_make_pc(PLAYER, hp_current=0))
    snap.character_locations[PLAYER] = "Blind Reach — The Crack"

    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="You drag yourself over the lip of the crack.",
            location="Blind Reach — Canyon Rim",
        ),
        pack=pack,
        player_name=PLAYER,
        acting_character_name=PLAYER,
        room=room_for(snapshot=snap, slug="flickering_reach"),
    )

    assert snap.character_locations[PLAYER] == "Blind Reach — Canyon Rim", (
        "hp=0 with no incapacitating status is a mid-resolution transient, not "
        "the durable death state — the gate fires only on hp<=0 AND "
        f"incapacitating (pinned AND contract); got "
        f"{snap.character_locations[PLAYER]!r}"
    )
