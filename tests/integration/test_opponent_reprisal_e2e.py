"""Story 71-21 (RED) — perseus_cloud one-sided combat: server-driven opponent attack.

PLAYTEST BUG (perseus_cloud / space_opera, SWN): personal combat is one-sided.
The player's strike resolves server-side with full mechanical backing (d20-vs-AC,
server-rolled damage, ablative-HP depletion) but the **opponent never takes a
server-driven attack turn**. Any "enemy shoots back" is narrator prose with zero
mechanical backing — the player can never actually lose a firefight.

The mechanical primitive ALREADY EXISTS and is unit-tested:
``ruleset.resolve_opponent_attack(...)`` on ``SwnRulesetModule`` (swn.py),
returning ``OpponentAttackOutcome`` (resolution.py) — but it has **zero production
callers** (verified: ``rg resolve_opponent_attack`` hits only base/swn/resolution/
tests). This story WIRES it into the per-turn combat path. Do NOT reimplement it.

These tests drive the **REAL space_opera pack** through ``dispatch_dice_throw``
(``find_confrontation_def(..., "combat")`` returns the personal Firefight —
``beat_selection`` + ``win_condition: hp_depletion``; the ship block is a separate
``ship_combat`` type). They assert that after the player acts, the seated opponent
reprises mechanically against the player.

DETERMINISM WITHOUT TOUCHING THE ROLL SEAM: the opponent's to-hit modifier is
fixed by content (shoot beat: attack_bonus 1 + combat_skill 1 + Physique-10 mod 0
= +2, so attack_total ∈ [3, 22]). We force a guaranteed HIT by setting the
**player's** AC = 2 (every roll clears it) and a guaranteed MISS by setting it to
30 (no roll clears it). Wide margins keep the tests robust even if Dev sources the
modifier slightly differently — the test pins behavior, not the Dev's internal
seam choice.

OPPONENT STAT SOURCE (Dev guidance): the opponent's ability scores for the attack
come from ``cdef.opponent_ability_scores()`` (content ``opponent_default_stats``
with hp/armor_class/dexterity stripped) — the real personal-combat cdef carries
``Physique: 10``, which is the shoot/overload beats' ``stat_check``. The opponent's
strike beat is the first eligible ``damage_channel: strike`` beat (``shoot``).
Scope is ``beat_selection`` + ``hp_depletion`` ONLY — the ``opposed_check`` path
already applies an opponent beat (do not double-drive it).

Skips gracefully when sidequest-content is not on disk.

``otel_capture`` is re-exported from ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import PackNotFound, find_pack_path

# The OTEL span the opponent-attack to-hit decision MUST emit (lie-detector).
# Pinned here as the contract Dev implements (story 71-21 AC4 / architect rec).
SPAN_OPPONENT_ATTACK = "encounter.opponent_attack_resolved"

PLAYER = "Nova"
OPPONENT = "Corsair"


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
    """The REAL personal-combat (Firefight) ConfrontationDef.

    ``find_confrontation_def`` matches ``confrontation_type == "combat"`` — only
    the personal Firefight is type ``combat`` (the ship block is ``ship_combat``).
    Returns None if the pack shape ever changes so the test fails loud.
    """
    return next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )


def _make_snapshot(
    *, player_ac: int, player_hp: int, opponent_hp: int = 99, opponent_armed: bool = True
):
    """Snapshot with a player Character (AC + HP + blaster) and an opponent NPC.

    - Player is the reprisal TARGET: their ``armor_class`` is the opponent's
      ``target_ac`` and their HP pool is what the reprisal ablates.
    - Opponent HP defaults to 99 so the player's own single strike (1d6 blaster,
      max 6) can NEVER resolve the encounter first — guaranteeing the reprisal
      fires (it must be gated on ``not encounter_resolved``).
    - ``opponent_armed=False`` reproduces PRODUCTION: ``_seed_combat_hp_depletion
      _to_npcs`` creates the opponent NPC with an EMPTY inventory, so its `shoot`
      beat resolves no weapon. The enemy must still damage the player via the
      cdef's ``opponent_damage`` (playtest 67-10).
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    player_core = CreatureCore(
        name=PLAYER,
        description="Station-side gunhand",
        personality="steady",
        inventory=Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}]),
        hp={"current": player_hp, "max": player_hp, "base_max": player_hp},
        armor_class=player_ac,
    )
    player = Character(
        core=player_core,
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
    )

    # Opponent mook. ``opponent_armed`` toggles whether it carries a sidearm —
    # production seeds it WEAPONLESS, so the weaponless case must lean on
    # cdef.opponent_damage for the reprisal to deal HP.
    opponent_items = (
        [{"id": "blaster_sidearm", "name": "Sidearm Blaster"}] if opponent_armed else []
    )
    opponent_core = CreatureCore(
        name=OPPONENT,
        description="Corsair raider",
        personality="brutal",
        inventory=Inventory(items=opponent_items),
        hp={"current": opponent_hp, "max": opponent_hp, "base_max": opponent_hp},
        armor_class=12,
    )

    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(player)
    snap.npcs.append(Npc(core=opponent_core))
    return snap


def _make_encounter():
    """Firefight StructuredEncounter: player vs opponent, hp_depletion combat."""
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
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
    )


def _drive_player_shoot(snap, enc, pack, *, broadcasts):
    """Run one player ``shoot`` turn through dispatch_dice_throw.

    face=18, Physique 10 (+0 mod), shoot base=2 → DC 14 → Success (damage fires).
    The opponent reprisal must occur as part of resolving this same dispatch.
    """
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    return dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="reprisal-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[18],
            beat_id="shoot",
        ),
        rolling_player_id="player-nova",
        character_name=PLAYER,
        character_stats={"Physique": 10},
        encounter=enc,
        pack=pack,
        genre_slug="space_opera",
        session_id="reprisal-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )


# ---------------------------------------------------------------------------
# AC1 — the wiring exists: dispatch calls resolve_opponent_attack
# ---------------------------------------------------------------------------


def test_player_shoot_invokes_resolve_opponent_attack(monkeypatch):
    """AC1: after the player's beat resolves, dispatch must call
    ``ruleset.resolve_opponent_attack`` for the seated opponent. RED today —
    the primitive has zero production callers."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")
    assert _personal_combat_cdef(pack) is not None, (
        "space_opera must expose a personal 'combat' (Firefight) confrontation"
    )

    from sidequest.game.ruleset.swn import SwnRulesetModule

    calls: list[dict] = []
    original = SwnRulesetModule.resolve_opponent_attack

    def _spy(self, **kwargs):
        calls.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(SwnRulesetModule, "resolve_opponent_attack", _spy)

    snap = _make_snapshot(player_ac=2, player_hp=12)
    _drive_player_shoot(snap, _make_encounter(), pack, broadcasts=[])

    assert len(calls) == 1, (
        f"opponent must take exactly one server-driven attack turn per player "
        f"beat; resolve_opponent_attack called {len(calls)} times"
    )
    # The reprisal targets the PLAYER's AC (the player is the Other from the
    # opponent's seat).
    assert calls[0]["target_ac"] == 2, (
        f"opponent's target_ac must be the player's armor_class (2); "
        f"got {calls[0].get('target_ac')!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — a hit ablates the player's HP; a miss leaves it intact
# ---------------------------------------------------------------------------


def test_opponent_hit_ablates_player_hp(otel_capture):
    """AC2 (hit): with player AC=2 the opponent's reprisal always lands; the
    player's HP must drop and a state_patch.hp span must fire on the player."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    snap = _make_snapshot(player_ac=2, player_hp=12)
    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    hp_before = player_core.hp.current

    _drive_player_shoot(snap, _make_encounter(), pack, broadcasts=[])

    assert player_core.hp.current < hp_before, (
        f"opponent reprisal must ablate the player's HP on a guaranteed hit; "
        f"before={hp_before} after={player_core.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in span_names, (
        f"a state_patch.hp span must fire when the opponent damages the player; spans={span_names}"
    )


def test_weaponless_opponent_still_ablates_player_hp_via_cdef_opponent_damage(otel_capture):
    """Playtest 67-10: production seeds the opponent NPC WEAPONLESS, so the `shoot`
    beat (which resolves the actor's inventory weapon) finds nothing — the enemy
    hit twice and dealt 0 HP, the player never took damage. The cdef's authored
    ``opponent_damage`` is the fix: the reprisal must ablate the player's HP even
    with an empty opponent inventory, and must NOT emit opponent_damage_spec_missing."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    # Guard: the real personal-combat cdef must actually author opponent_damage,
    # else this test would vacuously pass against a fixed inventory weapon.
    cdef = _personal_combat_cdef(pack)
    assert cdef is not None and cdef.opponent_damage is not None, (
        "space_opera `combat` cdef must author opponent_damage (the reprisal "
        "damage source for the weaponless seeded mook)"
    )

    snap = _make_snapshot(player_ac=2, player_hp=12, opponent_armed=False)
    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    hp_before = player_core.hp.current

    _drive_player_shoot(snap, _make_encounter(), pack, broadcasts=[])

    assert player_core.hp.current < hp_before, (
        f"a weaponless opponent must still ablate the player's HP via "
        f"cdef.opponent_damage; before={hp_before} after={player_core.hp.current}"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in span_names


def test_opponent_miss_leaves_player_hp_intact(otel_capture):
    """AC2 (miss edge): with player AC=30 the opponent's reprisal can never land
    (max total 22). The opponent-attack span MUST still fire (the reprisal ran
    and missed), but the player's HP must be unchanged. RED today — no reprisal
    fires at all, so the opponent-attack span is absent."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    snap = _make_snapshot(player_ac=30, player_hp=12)
    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    hp_before = player_core.hp.current

    _drive_player_shoot(snap, _make_encounter(), pack, broadcasts=[])

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_OPPONENT_ATTACK in span_names, (
        f"the opponent-attack span must fire even on a MISS (the reprisal ran); spans={span_names}"
    )
    assert player_core.hp.current == hp_before, (
        f"a missed reprisal must not change the player's HP; "
        f"before={hp_before} after={player_core.hp.current}"
    )


# ---------------------------------------------------------------------------
# AC3 — the player can actually lose (hp_depletion against the player)
# ---------------------------------------------------------------------------


def test_opponent_kill_resolves_hp_depletion_against_player(otel_capture):
    """AC3: a 1-HP player with AC=2 is dropped by the guaranteed-hit reprisal;
    the encounter must resolve via hp_depletion (the player loses). RED today —
    the player never takes damage, so the encounter never resolves against them."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    snap = _make_snapshot(player_ac=2, player_hp=1)
    enc = _make_encounter()

    _drive_player_shoot(snap, enc, pack, broadcasts=[])

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current <= 0, (
        f"the 1-HP player must be dropped to 0 by the reprisal; got {player_core.hp.current}"
    )
    assert enc.resolved, (
        "the encounter must resolve once the player's HP is depleted "
        "(the player can finally lose a firefight)"
    )
    # The hp_depletion resolution span must carry the hp_depletion source.
    resolved_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "encounter.resolved"
    ]
    assert resolved_spans, "an encounter.resolved span must fire on the player's defeat"
    sources = [s.attributes.get("source") for s in resolved_spans]
    assert any("hp_depletion" in str(src) for src in sources), (
        f"resolution must be sourced to hp_depletion; got sources={sources}"
    )


# ---------------------------------------------------------------------------
# AC4 — OTEL lie-detector: the opponent to-hit decision emits a span
# ---------------------------------------------------------------------------


def test_opponent_attack_emits_otel_to_hit_span(otel_capture):
    """AC4: every opponent attack emits ``encounter.opponent_attack_resolved``
    carrying the full to-hit math so the GM panel can tell a real reprisal from
    narrator improv. RED today — the span does not exist."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    snap = _make_snapshot(player_ac=2, player_hp=12)
    _drive_player_shoot(snap, _make_encounter(), pack, broadcasts=[])

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_OPPONENT_ATTACK]
    assert len(spans) == 1, (
        f"exactly one {SPAN_OPPONENT_ATTACK} span must fire per opponent turn; got {len(spans)}"
    )
    attrs = dict(spans[0].attributes)
    for key in ("attacker", "target", "d20", "modifier", "attack_total", "target_ac", "hit"):
        assert key in attrs, f"opponent-attack span must carry {key!r}; attrs={attrs}"

    # Internal consistency the GM panel relies on.
    assert attrs["target"] == PLAYER, "span 'target' must be the player"
    assert attrs["target_ac"] == 2, "span 'target_ac' must equal the player's AC"
    assert attrs["attack_total"] == attrs["d20"] + attrs["modifier"], (
        "attack_total must equal d20 + modifier"
    )
    assert attrs["hit"] is True, "AC=2 guarantees a hit; span 'hit' must be True"


# ---------------------------------------------------------------------------
# AC5 — the opponent's roll is visible in the player-facing dice overlay
# ---------------------------------------------------------------------------


def test_opponent_roll_broadcasts_dice_pair(otel_capture):
    """AC5: the opponent's attack roll must be broadcast as a DICE_REQUEST +
    DICE_RESULT pair so Sebastien/Jade see the enemy roll animate (player-facing
    math).

    Assertion shape (2026-06-08): assert directly on the OPPONENT's pair —
    a request + result attributed to the opponent NPC. The prior version
    counted total DICE_RESULTs (>=3, "player check + player damage + opponent
    to-hit"), but that proxy is coupled to the player's damage roll, which is
    skipped when the fixture player carries no weapon (``damage_spec_missing``
    on the ``shoot`` beat — the seeded PC has no inventory weapon and the beat
    no damage_override). The player's weapon is irrelevant to AC5; what AC5
    asserts is that the ENEMY's roll reaches the overlay. Even on a miss the
    opponent rolls to-hit and must broadcast that pair.
    """
    from sidequest.protocol.messages import DiceRequestMessage, DiceResultMessage

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    broadcasts: list[object] = []
    snap = _make_snapshot(player_ac=30, player_hp=12)  # opponent misses → still rolls to-hit
    _drive_player_shoot(snap, _make_encounter(), pack, broadcasts=broadcasts)

    opp_requests = [
        m
        for m in broadcasts
        if isinstance(m, DiceRequestMessage) and m.payload.character_name == OPPONENT
    ]
    opp_results = [
        m
        for m in broadcasts
        if isinstance(m, DiceResultMessage) and m.payload.character_name == OPPONENT
    ]
    assert opp_requests and opp_results, (
        "the opponent's attack roll must be broadcast as a DICE_REQUEST+DICE_RESULT "
        "pair attributed to the opponent so the table sees the enemy roll; got "
        f"requests={[type(m).__name__ for m in broadcasts]}"
    )


# ---------------------------------------------------------------------------
# AC6 — capability gate: SWN reprises, native fails loud (never silently skips)
# ---------------------------------------------------------------------------


def test_capability_gate_swn_provides_native_fails_loud():
    """AC6 (contract): the gate must key on CAPABILITY, not the string 'swn'.
    The SWN module implements ``resolve_opponent_attack``; native inherits the
    base ``NotImplementedError`` (fail-loud, never a silent skip). This pins the
    contract the dispatch gate must honor so wwn/cwn inherit and native is never
    silently dropped."""
    from sidequest.game.ruleset.registry import get_ruleset_module

    swn = get_ruleset_module("swn")
    out = swn.resolve_opponent_attack(
        attacker_stats={"Physique": 10},
        stat_check="Physique",
        attack_bonus=1,
        combat_skill=1,
        target_ac=2,
        d20=10,
    )
    assert out.hit is True and out.target_ac == 2, "SWN must resolve a real outcome"

    dial = get_ruleset_module("dial")
    with pytest.raises(NotImplementedError):
        dial.resolve_opponent_attack(
            attacker_stats={"Physique": 10},
            stat_check="Physique",
            attack_bonus=1,
            combat_skill=1,
            target_ac=2,
            d20=10,
        )


# ---------------------------------------------------------------------------
# sq-playtest 2026-06-07 SILENT death-spiral — the reprisal must SURFACE what it
# does: narrator directives, resolution signal, persisted-event ops, INFO logs.
# (Mechanics were correct; every channel that tells the story/table was silent.)
# ---------------------------------------------------------------------------


def _capture_dice_watcher(monkeypatch):
    """Capture ``dispatch.dice._watcher_publish`` calls (canonical pattern)."""
    import sidequest.server.dispatch.dice as dice_mod

    captured: list[dict] = []

    def _capture(event_type, fields, **kwargs):
        captured.append({"event_type": event_type, **fields})

    monkeypatch.setattr(dice_mod, "_watcher_publish", _capture)
    return captured


def test_reprisal_hit_appends_damage_directive_and_logs(caplog):
    """A reprisal HIT that does not down the player must (1) append a
    next-turn directive naming attacker/target/damage so the narrator can
    narrate the hit, and (2) log an INFO line for text-log forensics."""
    import logging

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    snap = _make_snapshot(player_ac=2, player_hp=12)
    enc = _make_encounter()
    with caplog.at_level(logging.INFO, logger="sidequest.server.dispatch.dice"):
        _drive_player_shoot(snap, enc, pack, broadcasts=[])

    hit_directives = [
        d for d in snap.next_turn_directives if "struck" in d and PLAYER in d and OPPONENT in d
    ]
    assert hit_directives, (
        f"reprisal hit must append a narrator directive naming the hit; "
        f"directives={snap.next_turn_directives!r}"
    )
    assert any("dice.opponent_reprisal_hit" in r.message for r in caplog.records), (
        "reprisal hit must INFO-log dice.opponent_reprisal_hit (text-log forensics)"
    )
    # Not downed → no resolution signal, no resolution directive.
    assert snap.pending_resolution_signal is None
    assert not any("RESOLVED" in d for d in snap.next_turn_directives)


def test_reprisal_miss_appends_no_damage_directive(caplog):
    """barsoom playtest 2026-06-10: a reprisal MISS told the narrator NOTHING
    (``if not outcome.hit: return``), so the prose fabricated a hit, damage that
    never landed, and a precise false HP value ("One hit point left" while the
    engine had the PC at 4/10). The miss path must append a MECHANICAL TRUTH
    directive anchoring the miss + the UNCHANGED HP so the narrator cannot
    invent either, and INFO-log the miss for text-log forensics (parity with
    dice.opponent_reprisal_hit)."""
    import logging

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    # AC 30: the reprisal d20 (max 20 + mook mods) can never reach it — a
    # guaranteed miss, no rng pinning needed.
    snap = _make_snapshot(player_ac=30, player_hp=4)
    enc = _make_encounter()
    with caplog.at_level(logging.INFO, logger="sidequest.server.dispatch.dice"):
        _drive_player_shoot(snap, enc, pack, broadcasts=[])

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None and player_core.hp.current == 4, (
        "precondition: the missed reprisal must not ablate the player"
    )
    miss_directives = [
        d for d in snap.next_turn_directives if "MISSED" in d and PLAYER in d and OPPONENT in d
    ]
    assert miss_directives, (
        f"a reprisal MISS must append a narrator directive anchoring the miss "
        f"(the silent miss path is how the narrator fabricated 'One hit point "
        f"left' against an engine 4/10); directives={snap.next_turn_directives!r}"
    )
    # The directive must anchor the player's REAL, unchanged HP so the prose
    # cannot quote an invented number.
    assert any("4/4" in d for d in miss_directives), (
        f"the miss directive must state the unchanged HP (4/4); got {miss_directives!r}"
    )
    assert any("dice.opponent_reprisal_miss" in r.message for r in caplog.records), (
        "reprisal miss must INFO-log dice.opponent_reprisal_miss (text-log "
        "forensics parity with the hit line)"
    )
    # A miss resolves nothing.
    assert snap.pending_resolution_signal is None
    assert not any("RESOLVED" in d for d in snap.next_turn_directives)


def test_reprisal_down_stamps_resolution_signal_and_directive(caplog):
    """A reprisal that DOWNS the player must stamp pending_resolution_signal
    (narrator renders the [ENCOUNTER RESOLVED] zone this turn), append a
    resolution directive, and INFO-log the resolution (hp_depletion +
    reprisal close lines)."""
    import logging

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    snap = _make_snapshot(player_ac=2, player_hp=1)  # any hit downs the player
    enc = _make_encounter()
    with caplog.at_level(logging.INFO):
        _drive_player_shoot(snap, enc, pack, broadcasts=[])

    assert enc.resolved and enc.outcome == "opponent_victory", (
        f"precondition: the reprisal must down the 1-HP player; "
        f"resolved={enc.resolved} outcome={enc.outcome}"
    )
    sig = snap.pending_resolution_signal
    assert sig is not None and sig.outcome == "opponent_victory", (
        f"reprisal resolution must stamp pending_resolution_signal "
        f"(sq-playtest 2026-06-07: narrator kept the fight alive); got {sig!r}"
    )
    assert any("RESOLVED" in d and "opponent_victory" in d for d in snap.next_turn_directives), (
        f"resolution directive missing; directives={snap.next_turn_directives!r}"
    )
    messages = [r.message for r in caplog.records]
    assert any("hp_depletion.resolved" in m for m in messages), (
        "check_hp_depletion must INFO-log the resolution (text-log forensics)"
    )
    assert any("dice.opponent_reprisal_resolved_encounter" in m for m in messages), (
        "the reprisal close must INFO-log dice.opponent_reprisal_resolved_encounter"
    )


def test_reprisal_down_publishes_resolved_watcher_event(monkeypatch):
    """The reprisal close must publish the op="resolved" watcher event
    (source=hp_depletion) — _maybe_persist_encounter_row maps it to a persisted
    ENCOUNTER_RESOLVED row, which the 2026-06-07 forensic timeline lacked."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    captured = _capture_dice_watcher(monkeypatch)
    snap = _make_snapshot(player_ac=2, player_hp=1)
    _drive_player_shoot(snap, _make_encounter(), pack, broadcasts=[])

    resolved = [
        e for e in captured if e.get("op") == "resolved" and e.get("source") == "hp_depletion"
    ]
    assert len(resolved) == 1, (
        f"exactly one op=resolved source=hp_depletion watcher event must publish "
        f"on the reprisal close; got {resolved!r}"
    )
    assert resolved[0]["outcome"] == "opponent_victory"
    assert resolved[0]["down_side"] == "player"


def test_reprisal_ops_persist_to_encounter_rows():
    """The reprisal's watcher ops must be registered for events-table
    persistence (ADR-124 census needs the HP-authoring events) and the new kind
    must be replay-skipped (reconnect must not crash on the internal row)."""
    from sidequest.server.session_handler import _REPLAY_SKIP_KINDS
    from sidequest.telemetry.watcher_hub import _KIND_BY_OP

    assert _KIND_BY_OP.get("opponent_attack_resolved") == "ENCOUNTER_OPPONENT_ATTACK"
    # The damage roll is a DISTINCT kind from the to-hit roll (sq-playtest
    # 2026-06-13 telemetry-gap): sharing one display name surfaced the damage
    # row as a "null" duplicate ENCOUNTER_OPPONENT_ATTACK (d20=None/hit=None).
    assert _KIND_BY_OP.get("opponent_damage_roll_resolved") == "ENCOUNTER_OPPONENT_DAMAGE"
    assert "ENCOUNTER_OPPONENT_ATTACK" in _REPLAY_SKIP_KINDS
    assert "ENCOUNTER_OPPONENT_DAMAGE" in _REPLAY_SKIP_KINDS
