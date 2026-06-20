"""Story 152-1 (RED, ADR-143) — WWN defensive actions resolve through WWN math,
NOT the native brace/break_contact reprisal-mitigation model.

REWRITE of the old 106-2 file (Keith ruling, 2026-06-20; design doc
``docs/superpowers/specs/2026-06-20-wn-full-action-set-design.md``). The original
106-2 spec encoded the NATIVE model — a per-beat opponent reprisal that a
``brace`` mitigated with flat HP reduction (``resolve_tier_deltas``) and a
``break_contact`` (push) PREVENTED wholesale. WWN has none of those: combat is
side-initiative (SRD §2.4.1, the opponent attacks ONCE on its own slot, no
per-beat reprisal), and defense is **Armor Class** (SRD §2.4.4). Shaping the
native mechanics into the WWN binding is the exact ADR-143 / SOUL "Bind the
Ruleset, Don't Balance It" trap, so the native scaffolding is REMOVED from the
WWN path and the genuine WWN defensive verbs are added:

  * **Total Defense** (SRD §2.4.4, Instant Action — give up your Main Action):
    +2 Melee & Ranged AC and **immune to Shock** until the start of your next
    turn. Resolved by the EXISTING WWN AC math — the opponent's d20+hit checks
    the boosted AC and *misses*; there is no damage-reduction number.
  * **Fighting Withdrawal** (SRD §2.4.4, Main Action): disengage from an adjacent
    melee attacker so a following **Run** provokes NO free attack. It does NOT
    cancel the opponent's own-turn attack.
  * **Run** (the plain flee): a Move out of melee that DOES provoke one free
    ("opportunity") attack from each adjacent enemy.

All magnitudes are WWN-SRD-verbatim (+2 AC, Shock immunity) — never invented.

GROUND-TRUTH MEASUREMENT (2026-06-20, real heavy_metal pack, ``ruleset: wwn``):
108-3 stripped every native combat beat off the ``hp_depletion`` combat def, so
``cdef.beats == []``. Consequences this suite drives RED:
  1. ``total_defense`` / ``fighting_withdrawal`` / ``run`` are not in the WN action
     allowlist → ``DiceDispatchError: unknown beat_id ... available: []``.
  2. The OPPONENT'S reprisal looks for a strike beat in the empty ``cdef.beats``
     and SKIPS (``dice.opponent_reprisal_skipped reason=no_strike_beat``) — so the
     opponent never attacks and there is nothing to defend against. Dev must
     synthesize the opponent's WN strike (mirror of ``wn_action_beat('attack')``)
     so the opponent attacks once on its slot vs the defender's AC.

CONTRACT this story establishes (for Dev), all WWN-gated
(``isinstance(ruleset, WithoutNumberRulesetModule)``):
  * New synthesized WWN action ids (closed allowlist preserved — a bogus id still
    raises, per 108-8's guard): ``total_defense``, ``fighting_withdrawal``, ``run``.
  * The opponent's strike is WN-synthesized so it attacks on its slot.
  * ``encounter.opponent_attack_resolved`` carries the defender's committed action
    (``defender_beat``) and, under Total Defense, the AC delta (``ac_delta == 2``,
    ``target_ac == base_ac + 2``). A free attack on a flee carries
    ``source="opportunity_attack"``; the own-turn slot attack keeps
    ``source="opponent_reprisal"``.
  * 108-8's invariants stay green (covered by test_108_8): closed allowlist + the
    isinstance gate; native packs resolve authored ids on the native engine.

Shared fixtures: ``tests/integration/_wn_round_102_4``. Skips cleanly when
sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests.integration._wn_round_102_4 import (
    GENRE_PACKS_DIR,
    dispatch_throw,
    force_initiative,
    load_pack,
    seat_wn_combat,
    spans_named,
)

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

# heavy_metal Blade-work combat (rules.yaml): hp_depletion, opponent_damage 1d8,
# opponent_default_stats all-10 (SWN attribute mod +0), armor_class 12.
_PC = "Vesska"
_OPP = "Hired Blade"

# Story 152-1 WWN defensive-action ids the dispatch must synthesize under a WWN
# binding (centralised so a contract change is a one-line edit). ``attack`` is the
# offensive baseline (already synthesized — story 108-8).
_ATTACK = "attack"
_TOTAL_DEFENSE = "total_defense"
_FIGHTING_WITHDRAWAL = "fighting_withdrawal"
_RUN = "run"

_SPAN_OPP_ATTACK = "encounter.opponent_attack_resolved"
_SPAN_ROUND_RESOLVED = "wwn.round.resolved"

# WWN SRD §2.4.4 — Total Defense grants +2 Melee & Ranged AC (verbatim, not invented).
# (Base Melee/Ranged AC is read from the baseline span at runtime, not hard-coded, so the
# +2 assertion survives any change to the unarmored-AC default.)
_TOTAL_DEFENSE_AC_BONUS = 2


def _solo_wwn_combat(*, pc_hp: int = 12):
    """One PC vs one blade, real heavy_metal (wwn) hp_depletion combat."""
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP], pc_hp=pc_hp)
    return pack, snap, enc


def _pc_hp(snap, name: str = _PC) -> int:
    core = snap.find_creature_core(name)
    assert core is not None, f"PC {name!r} core must resolve"
    return core.hp.current


def _opp_attack_spans(otel_capture, *, source: str | None = None):
    """Opponent-attack spans, optionally filtered to a ``source`` attribute
    (``"opponent_reprisal"`` = own-turn slot; ``"opportunity_attack"`` = the free
    attack a flee provokes)."""
    spans = spans_named(otel_capture, _SPAN_OPP_ATTACK)
    if source is None:
        return spans
    return [s for s in spans if dict(s.attributes).get("source") == source]


# ---------------------------------------------------------------------------
# AC1 — the opponent attacks ONCE on its slot vs the defender's AC. The WN engine
#       OWNS the action set, so the opponent's strike is synthesized even on a
#       zero-beat (post-108-3) combat def. Precondition for every defense test.
# ---------------------------------------------------------------------------


def test_wwn_opponent_attacks_on_its_slot_with_a_synthesized_strike(monkeypatch, otel_capture):
    """RED (AC1): on the zero-beat WWN combat def the opponent must still attack on
    its own initiative slot. MEASURED today: ``_resolve_opponent_reprisal`` finds no
    strike beat in the empty ``cdef.beats`` and skips (``no_strike_beat``) — the PC
    takes ZERO damage under MAX rolls. The fix synthesizes the opponent's WN strike
    so the enemy swings once on its slot (no per-beat reprisal model)."""
    monkeypatch.setattr("random.randint", lambda a, b: b)  # MAX: any opponent hit lands hard
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])  # opponent first → it acts while the PC is live
    hp0 = _pc_hp(snap)

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_ATTACK
    )

    loss = hp0 - _pc_hp(snap)
    assert loss > 0, (
        "the opponent took no turn on the zero-beat WWN combat def — its strike was "
        f"skipped (no synthesized opponent beat); PC lost {loss} HP under MAX rolls. "
        "The WN engine must OWN the opponent's action too (AC1: 'the opponent attacks "
        "once on its slot vs the defender's AC')."
    )
    own_turn = _opp_attack_spans(otel_capture, source="opponent_reprisal")
    assert len(own_turn) == 1, (
        "exactly one own-turn opponent attack must fire on the opponent's slot; "
        f"got {len(own_turn)} (source='opponent_reprisal' spans)"
    )


# ---------------------------------------------------------------------------
# AC2 — Total Defense: +2 Melee & Ranged AC (resolved by the existing AC math) and
#       Shock immunity, both until the defender's next turn. WWN SRD §2.4.4.
# ---------------------------------------------------------------------------


def test_total_defense_flips_a_marginal_hit_to_a_miss(monkeypatch, otel_capture):
    """RED (AC2): under identical pinned rng a marginal opponent hit at BASE AC must
    become a MISS under Total Defense (+2 AC). Proves the boost flows through the
    existing ``resolve_opponent_attack`` AC math, not a bespoke damage subtraction.

    d20 pinned to 11; opponent all-10 stats → SWN mod +0 → attack_total 11. Base AC
    10 → HIT (11 >= 10); Total-Defense AC 12 → MISS (11 < 12)."""
    fake = lambda a, b: 11 if (a, b) == (1, 20) else b  # noqa: E731 — d20=11, damage=max
    monkeypatch.setattr("random.randint", fake)

    # Baseline: offensive commit, no defense — the marginal hit lands.
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])
    base_hp0 = _pc_hp(snap)
    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_ATTACK
    )
    base_loss = base_hp0 - _pc_hp(snap)
    base_spans = _opp_attack_spans(otel_capture, source="opponent_reprisal")
    assert len(base_spans) == 1, f"one own-turn attack expected in baseline; got {len(base_spans)}"
    base_attrs = dict(base_spans[0].attributes)
    base_ac = int(base_attrs["target_ac"])
    assert base_attrs["hit"] is True and base_loss > 0, (
        "fixture precondition: the undefended marginal attack must HIT and deal damage "
        f"(attack_total={base_attrs.get('attack_total')} vs AC {base_ac}); base_loss={base_loss}"
    )
    assert int(base_attrs["attack_total"]) in (base_ac, base_ac + 1), (
        "fixture precondition: need a MARGINAL hit (attack_total within 1 of AC) for the "
        f"+2 flip to be observable; attack_total={base_attrs['attack_total']} ac={base_ac}. "
        "Adjust the pinned d20 if the opponent's synthesized to-hit modifier differs."
    )

    # Defended: a fresh identical combat, PC commits Total Defense.
    otel_capture.clear()
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])
    td_hp0 = _pc_hp(snap)
    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_TOTAL_DEFENSE
    )
    td_loss = td_hp0 - _pc_hp(snap)
    td_spans = _opp_attack_spans(otel_capture, source="opponent_reprisal")
    assert len(td_spans) == 1, (
        f"one own-turn attack expected under Total Defense; got {len(td_spans)}"
    )
    td_attrs = dict(td_spans[0].attributes)

    assert td_loss == 0, (
        f"Total Defense must turn the marginal hit into a miss; PC lost {td_loss} HP "
        f"(baseline lost {base_loss}). The +2 AC is not being applied to the opponent's roll."
    )
    assert td_attrs["hit"] is False, "the opponent's attack must MISS the Total-Defense AC"
    assert int(td_attrs["target_ac"]) == base_ac + _TOTAL_DEFENSE_AC_BONUS, (
        f"Total Defense must raise the checked AC by +{_TOTAL_DEFENSE_AC_BONUS} (SRD §2.4.4); "
        f"span target_ac={td_attrs['target_ac']} expected {base_ac + _TOTAL_DEFENSE_AC_BONUS}"
    )


def test_total_defense_span_carries_committed_action_and_ac_delta(monkeypatch, otel_capture):
    """RED (AC5 / OTEL lie-detector): the opponent-attack span must name the
    defender's committed WWN action and the AC delta applied, so the GM panel sees
    the defense fire (prose claiming 'you go on the defensive' must be span-backed).

    Contract: ``defender_beat == 'total_defense'`` and ``ac_delta == 2``."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_TOTAL_DEFENSE
    )

    spans = _opp_attack_spans(otel_capture, source="opponent_reprisal")
    assert len(spans) == 1, f"exactly one own-turn opponent attack expected; got {len(spans)}"
    attrs = dict(spans[0].attributes)
    assert attrs.get("defender_beat") == _TOTAL_DEFENSE, (
        f"the opponent-attack span must name the committed defensive action; attrs={attrs}"
    )
    assert int(attrs.get("ac_delta", 0)) == _TOTAL_DEFENSE_AC_BONUS, (
        f"the span must carry the +{_TOTAL_DEFENSE_AC_BONUS} AC delta Total Defense applied; "
        f"attrs={attrs}"
    )


def test_total_defense_is_not_flat_mitigation_a_real_hit_takes_full_damage(
    monkeypatch, otel_capture
):
    """RED (AC1 contrast): Total Defense is AC manipulation, NOT the native flat-HP
    ``brace`` mitigation. When the opponent rolls high enough to hit EVEN the +2 AC,
    the defender takes the FULL weapon damage — no partial reduction is subtracted.
    Catches a regression to the native ``defense_mitigation`` model.

    d20=20 (hits any AC), damage 1d8 pinned to 8 → the PC must lose exactly 8."""
    fake = lambda a, b: 20 if (a, b) == (1, 20) else b  # noqa: E731 — d20=20 hit, 1d8=8
    monkeypatch.setattr("random.randint", fake)
    pack, snap, enc = _solo_wwn_combat(pc_hp=20)
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])
    hp0 = _pc_hp(snap)

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_TOTAL_DEFENSE
    )

    loss = hp0 - _pc_hp(snap)
    assert loss == 8, (
        f"a hit that beats the Total-Defense AC must deal FULL weapon damage (8); PC lost "
        f"{loss}. A reduced loss means native flat 'brace' mitigation leaked into the WWN path "
        "(ADR-143: that scaffolding is REMOVED, not retuned)."
    )
    spans = _opp_attack_spans(otel_capture, source="opponent_reprisal")
    attrs = dict(spans[0].attributes)
    assert int(attrs.get("defense_mitigation", 0)) == 0, (
        "Total Defense must not report a flat damage-mitigation magnitude (defense is AC, "
        f"not HP reduction); attrs={attrs}"
    )


def test_total_defense_grants_shock_immunity(monkeypatch, otel_capture):
    """RED (AC2 — Shock immunity, SRD §2.4.4): a committed Total Defense suppresses the
    Shock chip a missed attack would otherwise deal. The immunity is EXPLICIT, not a
    side effect of the +2 AC: the shock weapon's ``shock_ac`` ceiling (20) is far above
    even the boosted AC (12), so without the immunity flag the chip would still land.

    Setup: give the opponent a Shock 1 / AC 20 weapon; pin a MISS (d20=2). At base AC
    the miss chips 1; under Total Defense it must chip 0."""
    from sidequest.genre.models.inventory import DamageSpec

    fake = lambda a, b: 2 if (a, b) == (1, 20) else b  # noqa: E731 — d20=2 → a clean miss
    monkeypatch.setattr("random.randint", fake)

    def _shock_combat():
        pack, snap, enc = _solo_wwn_combat()
        cdef = next(c for c in pack.rules.confrontations if c.win_condition == "hp_depletion")
        cdef.opponent_damage = DamageSpec(dice="1d8", bonus=0, shock=1, shock_ac=20)
        force_initiative(enc, [(_OPP, 9), (_PC, 2)])
        return pack, snap, enc

    # Baseline: undefended miss still chips Shock (the rule is live).
    pack, snap, enc = _shock_combat()
    base_hp0 = _pc_hp(snap)
    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_ATTACK
    )
    base_chip = base_hp0 - _pc_hp(snap)
    assert base_chip == 1, (
        f"fixture precondition: an undefended miss must chip Shock 1; PC lost {base_chip}"
    )

    # Total Defense: immune to Shock → no chip despite the same miss.
    pack, snap, enc = _shock_combat()
    td_hp0 = _pc_hp(snap)
    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_TOTAL_DEFENSE
    )
    td_chip = td_hp0 - _pc_hp(snap)
    assert td_chip == 0, (
        f"Total Defense must grant Shock immunity (SRD §2.4.4); PC still took {td_chip} Shock "
        "damage. The +2 AC alone does not suppress Shock (shock_ac=20 > boosted AC 12) — the "
        "immunity must be explicit."
    )


# ---------------------------------------------------------------------------
# AC3 — Fighting Withdrawal + Run. A plain Run provokes the free attack; Fighting
#       Withdrawal avoids it; neither cancels the opponent's own-turn attack.
#       WWN SRD §2.4.4.
# ---------------------------------------------------------------------------


def test_plain_run_provokes_a_free_opportunity_attack(monkeypatch, otel_capture):
    """RED (AC3): a plain Run out of melee provokes ONE free attack from the adjacent
    enemy. PC-first so the flee resolves on the PC's slot; under MAX rolls the free
    attack lands. The free attack is distinguished by ``source='opportunity_attack'``."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(
        enc, [(_PC, 9), (_OPP, 2)]
    )  # PC flees first; opponent's later slot finds no target
    hp0 = _pc_hp(snap)

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_RUN)

    opp_atk = _opp_attack_spans(otel_capture, source="opportunity_attack")
    assert len(opp_atk) == 1, (
        "a plain Run from melee must provoke exactly one free attack "
        f"(source='opportunity_attack'); got {len(opp_atk)}"
    )
    assert hp0 - _pc_hp(snap) > 0, (
        "the provoked free attack must land under MAX rolls (no damage taken)"
    )


def test_fighting_withdrawal_avoids_the_free_attack(monkeypatch, otel_capture):
    """RED (AC3): Fighting Withdrawal disengages safely — the free attack a Run would
    provoke does NOT occur. Same setup/rng as the Run test; the only change is the
    committed action, so a difference is the disengage working, not luck."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_PC, 9), (_OPP, 2)])
    hp0 = _pc_hp(snap)

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="p1",
        beat_id=_FIGHTING_WITHDRAWAL,
    )

    assert not _opp_attack_spans(otel_capture, source="opportunity_attack"), (
        "Fighting Withdrawal must avoid the free attack a flee would provoke (SRD §2.4.4); "
        "an opportunity_attack span fired"
    )
    assert _pc_hp(snap) == hp0, (
        f"Fighting Withdrawal must take no opportunity damage; PC lost {hp0 - _pc_hp(snap)} HP"
    )


def test_fighting_withdrawal_does_not_cancel_the_opponent_own_turn_attack(
    monkeypatch, otel_capture
):
    """RED (AC3 / AC1): Fighting Withdrawal must NOT cancel the opponent's own-turn
    attack — the key contrast with the REMOVED native break_contact (which set
    ``defense_prevented`` and negated the whole attack).

    The opponent acts FIRST (slot 9 > PC slot 2), so when its turn comes the PC has
    not yet withdrawn (the withdrawal is the PC's own Main Action, resolved at the
    PC's later slot) — faithful WWN side-initiative. The opponent therefore attacks
    the still-seated PC normally; under MAX rolls it lands. A break_contact-style
    whole-attack prevent would zero this."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])
    hp0 = _pc_hp(snap)

    dispatch_throw(
        pack=pack,
        snap=snap,
        enc=enc,
        character_name=_PC,
        player_id="p1",
        beat_id=_FIGHTING_WITHDRAWAL,
    )

    own_turn = _opp_attack_spans(otel_capture, source="opponent_reprisal")
    assert own_turn, (
        "the opponent's own-turn attack must still fire after a Fighting Withdrawal — it does "
        "NOT cancel the enemy turn (contrast the removed native break_contact whole-attack "
        "prevent). No source='opponent_reprisal' span fired."
    )
    assert all(dict(s.attributes).get("defense_prevented") is not True for s in own_turn), (
        "no own-turn opponent attack may be marked defense_prevented — the native whole-attack "
        "prevention is REMOVED from the WWN path (ADR-143)"
    )
    assert hp0 - _pc_hp(snap) > 0, (
        "the opponent's un-cancelled own-turn attack must land on the still-seated PC (MAX rolls)"
    )
    assert not _opp_attack_spans(otel_capture, source="opportunity_attack"), (
        "Fighting Withdrawal is a safe disengage — it must not provoke a free opportunity attack"
    )


def test_run_does_not_re_attack_a_fleer_downed_mid_flight(monkeypatch, otel_capture):
    """RED (Reviewer MEDIUM, rework R1): in a MULTI-opponent flee, once the FIRST
    opportunity attack downs the fleer the loop must STOP — the remaining opponents
    must not keep swinging at a downed PC (which also double-fires the resolution
    close, confusing the GM-panel lie detector). The own-turn slot already guards
    this with ``if encounter.resolved: continue`` (wn_round.py); the opportunity
    loop must mirror it (break/skip once the fight resolves or the fleer drops).

    Two blades; the PC at 6 HP flees; MAX rolls so the first opportunity attack
    (1d8=8) downs the PC. EXACTLY ONE opportunity_attack span must fire. Today the
    loop has no guard, so BOTH blades swing → 2 spans → RED."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP, "Cutthroat"], pc_hp=6)
    force_initiative(enc, [(_PC, 9), (_OPP, 5), ("Cutthroat", 2)])  # PC flees on its slot first

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_RUN)

    assert _pc_hp(snap) <= 0, (
        f"fixture precondition: the first opportunity attack must DOWN the 6-HP fleer; "
        f"PC at {_pc_hp(snap)} HP"
    )
    opp_atk = _opp_attack_spans(otel_capture, source="opportunity_attack")
    assert len(opp_atk) == 1, (
        "once the fleer is downed by the first opportunity attack the loop must STOP — the "
        f"remaining opponent must not swing at the downed PC; got {len(opp_atk)} opportunity "
        "attacks (the loop lacks the encounter.resolved/fleer-downed guard the own-turn slot has)"
    )


# ---------------------------------------------------------------------------
# AC5 — wiring: the defensive round resolves through the production
#       dispatch_dice_throw → run_wn_round seam, proven by the WWN round span.
# ---------------------------------------------------------------------------


def test_total_defense_round_resolves_through_the_wwn_sealed_walk(monkeypatch, otel_capture):
    """RED (AC5, wiring): a Total-Defense round must resolve through the WWN
    sealed-initiative walk (the sole opponent-attack path), proven by the
    ``wwn.round.resolved`` span — so the AC-math tests above genuinely run on the
    production seam, not a unit call."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_TOTAL_DEFENSE
    )

    assert spans_named(otel_capture, _SPAN_ROUND_RESOLVED), (
        "the Total-Defense round must resolve through the WWN sealed-initiative walk "
        f"(missing {_SPAN_ROUND_RESOLVED!r} means it fell off the production seam)"
    )


# ---------------------------------------------------------------------------
# AC1 (no-regression, scope-pinning): the WWN defensive synthesis must NOT bleed
# into the SWN family. A space_opera SWN fight with no persisted initiative still
# reprises on the legacy path and does not raise — proving the new behavior is
# bound to the WWN module, not all of SwnRulesetModule (ADR-117 isinstance).
# (Carried over from the old 106-2 AC4 guard; GREEN by design today.)
# ---------------------------------------------------------------------------


def test_swn_sibling_without_initiative_keeps_legacy_reprisal_no_raise():
    """The WWN-only defensive changes must NOT regress the SWN family's legacy
    reprisal path (story 71-21, space_opera/perseus_cloud), which deliberately
    resolves with no persisted initiative. A space_opera SWN fight with empty
    initiative must STILL reprise on the legacy path and NOT raise."""
    from tests.integration.test_opponent_reprisal_e2e import (
        _load_space_opera_pack,
        _make_encounter,
        _make_snapshot,
    )

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    snap = _make_snapshot(player_ac=2, player_hp=12)
    enc = _make_encounter()
    assert not enc.initiative, "fixture precondition: SWN legacy path has no persisted initiative"
    player_core = snap.find_creature_core("Nova")
    assert player_core is not None
    hp_before = player_core.hp.current

    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="swn-noinit-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0), angular=(1.0, 1.0, 1.0), position=(0.5, 0.5)
            ),
            face=[18],
            beat_id="shoot",
        ),
        rolling_player_id="player-nova",
        character_name="Nova",
        character_stats={"Physique": 10},
        encounter=enc,
        pack=pack,
        genre_slug="space_opera",
        session_id="swn-noinit-session",
        round_number=1,
        room_broadcast=None,
        snapshot=snap,
    )
    assert player_core.hp.current < hp_before, (
        "the SWN legacy reprisal must still fire with no initiative (the WWN defensive "
        "synthesis must not regress 71-21's space_opera path)"
    )
