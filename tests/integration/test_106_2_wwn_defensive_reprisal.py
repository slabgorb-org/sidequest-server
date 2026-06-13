"""Story 106-2 (RED) — WWN reprisal model: a defensive beat must mitigate the
per-beat opponent reprisal (easy-ramp lever #2).

PLAYTEST BUG (caverns_and_claudes/beneath_sunden, WWN ruleset, ADR-117;
sq-playtest-pingpong 2026-06-13): every player beat — strike, committed_blow,
**and even a defensive brace** — was immediately answered by a full opponent
attack. Zeppo's Brace (a *defensive* beat) ate a `d20=15+2=17 hit, 1d8=5`
reprisal that killed him. The defense mitigated NOTHING — Brace was mechanically
identical to Strike in damage taken. There was no survival play.

ROOT DEFECT (cite-grounded, common to both opponent-attack paths): the reprisal
resolver ``_resolve_opponent_reprisal`` (``dice.py:1568``) is **blind to the
player's committed beat** — it reads the player's AC flat
(``target_ac = int(player_core.armor_class)``, dice.py:1638) and rolls
``resolve_opponent_attack`` with no defensive modifier. The WWN sealed-round walk
(``wn_round.py:283-296``) calls that *same* blind resolver, so even on the
WWN-faithful initiative path, **Brace changes nothing about the incoming attack.**

OPERATOR RULING (Keith, 2026-06-13, .pennyfarthing/sidecars/gm-decisions.md):
**Option A — WWN initiative round (full-defend).** Route WWN ``hp_depletion``
combat through the sealed initiative round (``run_wn_round``) as the sole
opponent-attack path; a committed **full-defend / Break Contact** makes the
opponent's slot **miss or not occur** that round, and a committed **Brace**
measurably blunts the reprisal (the WWN ``brace`` BeatKind already supplies a
damage-mitigation primitive — ``apply_beat_hp_channel(target_mitigation=...)``,
``beat_kinds.py:357-373``; context-story-106-2 "Existing infrastructure to
reuse"). All magnitudes are WWN-SRD-sourced, never invented (standing ruling).

These tests drive the **real** ``dispatch_dice_throw → run_wn_round`` seam on the
REAL heavy_metal pack (``ruleset: wwn``), whose Blade-work ``hp_depletion``
combat authors a ``brace`` and a ``break_contact`` beat. They assert the
defensive choice now changes the enemy's attack. RED today on every count: the
resolver never receives the committed beat.

Shared fixtures: ``tests/integration/_wn_round_102_4`` (the 102-4 WN turn-model
helpers). Skips cleanly when sidequest-content is not on disk.
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

# heavy_metal Blade-work combat (rules.yaml): hp_depletion, opponent_damage 1d8.
_PC = "Vesska"
_OPP = "Hired Blade"

_BRACE = "brace"  # kind: brace — "Reduce incoming damage this round"
_BREAK_CONTACT = "break_contact"  # kind: push — "Combat ends — withdraws / let go"
_STRIKE = "committed_blow"  # kind: strike — the undefended baseline

_SPAN_OPP_ATTACK = "encounter.opponent_attack_resolved"
_SPAN_ROUND_RESOLVED = "wwn.round.resolved"


def _solo_wwn_combat():
    """One PC vs one blade, real heavy_metal (wwn) hp_depletion combat."""
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC], [_OPP])
    return pack, snap, enc


def _pc_hp(snap) -> int:
    core = snap.find_creature_core(_PC)
    assert core is not None, "PC core must resolve"
    return core.hp.current


# ---------------------------------------------------------------------------
# AC1 — Brace measurably reduces the reprisal damage vs an undefended strike.
#       (Also the AC5 wiring proof: drives the real dispatch → run_wn_round seam.)
# ---------------------------------------------------------------------------


def test_brace_takes_strictly_less_reprisal_damage_than_strike(monkeypatch):
    """AC1/AC5: under identical pinned rng + initiative (opponent slot first), a
    PC who commits **Brace** loses strictly LESS HP to the opponent's reprisal
    than a PC who commits a strike. The playtest counter-fact is that today they
    are equal (Brace == Strike in damage taken); this pins that they now differ.

    Brace mitigates via the existing ``target_mitigation`` damage-reduction
    primitive (context-story-106-2 "Existing infrastructure to reuse"). The
    round resolves through the WWN sealed walk — asserted via the
    ``wwn.round.resolved`` span — so this is also the end-to-end wiring test:
    the mitigation is reachable from the production ``dispatch_dice_throw``
    entry, not a unit-level call.
    """
    # MAX rng: opponent d20=20 (guaranteed hit) and opponent damage 1d8=8 — the
    # reprisal lands hard against the undefended baseline so any mitigation is
    # visible as a strict HP-loss reduction.
    monkeypatch.setattr("random.randint", lambda a, b: b)

    # Baseline: PC commits a strike. Opponent (initiative 9) reprises first.
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])
    hp0 = _pc_hp(snap)
    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_STRIKE
    )
    strike_loss = hp0 - _pc_hp(snap)
    assert strike_loss > 0, (
        "fixture precondition: the undefended reprisal must land (the opponent "
        f"acts first and hits at pinned d20=20); strike_loss={strike_loss}"
    )

    # Defended: a fresh identical combat, PC commits Brace instead.
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])
    hp0 = _pc_hp(snap)
    out = dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_BRACE
    )
    brace_loss = hp0 - _pc_hp(snap)

    assert out.commitment_pending is False, "a solo commit closes the barrier and fires the round"
    assert brace_loss < strike_loss, (
        "a committed Brace must take strictly LESS reprisal damage than an "
        f"undefended strike under identical rng; brace_loss={brace_loss} "
        f"strike_loss={strike_loss} (today they are equal — Brace is ignored)"
    )


def test_brace_round_resolves_through_the_wwn_initiative_walk(monkeypatch, otel_capture):
    """AC5 wiring anchor: a braced WWN round goes through the sealed-initiative
    walk (Option A's sole opponent-attack path), proven by the
    ``wwn.round.resolved`` span — so the mitigation tests above genuinely run on
    the production ``dispatch_dice_throw → run_wn_round`` seam, not a unit call.

    GREEN today (102-4 already routes WWN+initiative through the walk); this is
    the regression net that keeps the mitigation reachable from the real entry.
    """
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_BRACE
    )

    assert spans_named(otel_capture, _SPAN_ROUND_RESOLVED), (
        "the braced round must resolve through the WWN sealed-initiative walk "
        f"(missing {_SPAN_ROUND_RESOLVED} span means it fell to the legacy "
        "per-beat reprisal rider, defeating Option A)"
    )


# ---------------------------------------------------------------------------
# AC3 — the defensive mitigation is OTEL-observable (the GM-panel lie-detector).
# ---------------------------------------------------------------------------


def test_brace_mitigation_surfaces_on_opponent_attack_span(monkeypatch, otel_capture):
    """AC3: the ``encounter.opponent_attack_resolved`` span must carry the
    target's committed defensive beat and a non-zero mitigation magnitude, so a
    reviewer can confirm in OTEL that **the brace changed the enemy's roll**
    (prose claiming "you brace and the blow glances off" must be span-backed).

    Contract pinned here for Dev: the span gains
      - ``defender_beat``: the target's committed beat id this round ("brace"),
      - ``defense_mitigation``: int magnitude of the defensive effect applied to
        THIS attack (to-hit penalty or flat damage reduction, WWN-SRD-sourced;
        0 when the committed beat is not defensive).
    RED today — the span carries neither.
    """
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_BRACE
    )

    spans = spans_named(otel_capture, _SPAN_OPP_ATTACK)
    assert len(spans) == 1, (
        f"exactly one opponent-attack span must fire for the braced round; got {len(spans)}"
    )
    attrs = dict(spans[0].attributes)
    assert attrs.get("defender_beat") == _BRACE, (
        "the opponent-attack span must name the target's committed defensive "
        f"beat ('brace') so the GM panel sees the defense; attrs={attrs}"
    )
    assert int(attrs.get("defense_mitigation", 0)) > 0, (
        "the span must carry a non-zero defense_mitigation proving the brace "
        f"reduced the enemy's roll/damage; attrs={attrs}"
    )


def test_undefended_strike_span_reports_zero_mitigation(monkeypatch, otel_capture):
    """AC3 (contrast): an undefended (offensive) commit must report
    ``defense_mitigation == 0`` — the mitigation field is honest, not always-on.
    Pins that the span distinguishes a real defense from an ordinary turn."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_STRIKE
    )

    spans = spans_named(otel_capture, _SPAN_OPP_ATTACK)
    assert len(spans) == 1, f"one opponent-attack span expected; got {len(spans)}"
    attrs = dict(spans[0].attributes)
    assert int(attrs.get("defense_mitigation", 0)) == 0, (
        "an undefended strike must report zero defensive mitigation (the field "
        f"must not be always-on); attrs={attrs}"
    )
    assert attrs.get("defender_beat") != _BRACE, (
        "an undefended strike must not be labeled as a brace on the span"
    )


# ---------------------------------------------------------------------------
# AC2 — a committed full-defend / Break Contact prevents the reprisal this round
#       (HP loss zero — the player is NOT taking a full enemy attack regardless
#       of choice; evidence point #2).
# ---------------------------------------------------------------------------


def test_break_contact_prevents_the_reprisal_this_round(monkeypatch):
    """AC2: a PC who commits **Break Contact** (full-defend / disengage) takes
    ZERO reprisal damage this round — the opponent's slot misses or does not
    occur. Today the opponent attacks the PC at full force regardless (the
    defense is ignored), so the PC loses HP. RED.

    rng MAX so the undefended baseline WOULD land for 8 (see AC1: strike_loss>0
    under these exact conditions) — the zero here is the defense working, not a
    lucky miss.
    """
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack, snap, enc = _solo_wwn_combat()
    force_initiative(enc, [(_OPP, 9), (_PC, 2)])
    hp0 = _pc_hp(snap)

    dispatch_throw(
        pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_BREAK_CONTACT
    )

    assert _pc_hp(snap) == hp0, (
        "a committed Break Contact / full-defend must prevent the opponent's "
        f"reprisal this round (zero HP loss); before={hp0} after={_pc_hp(snap)}"
    )


# ---------------------------------------------------------------------------
# AC4 — WWN-gated, fail-loud: a WWN fight that cannot route through the sealed
#       initiative walk must FAIL LOUD, never silently degrade to the legacy
#       unconditional reprisal. The SWN sibling's legacy path stays untouched.
# ---------------------------------------------------------------------------


def test_wwn_combat_without_initiative_fails_loud(monkeypatch):
    """AC4: a WWN ``hp_depletion`` combat with NO persisted initiative must raise
    loudly when a beat is dispatched — Option A makes the sealed initiative walk
    the *only* WWN opponent-attack path, so a missing order is a real failure,
    not a cue to silently resolve on the legacy unconditional reprisal
    (dice.py:641-652 today logs a warning and falls through — that silent
    degrade is exactly what fails loud now). RED today: no raise.
    """
    monkeypatch.setattr("random.randint", lambda a, b: b)
    from sidequest.server.dispatch.dice import DiceDispatchError

    pack, snap, enc = _solo_wwn_combat()
    enc.initiative = []  # pre-P4 / direct-construction shape: no persisted order

    with pytest.raises(DiceDispatchError):
        dispatch_throw(
            pack=pack, snap=snap, enc=enc, character_name=_PC, player_id="p1", beat_id=_STRIKE
        )


def test_swn_sibling_without_initiative_keeps_legacy_reprisal_no_raise():
    """AC4 (no-regression guard, scope-pinning): the WWN-only fail-loud above
    must NOT regress the SWN family's legacy reprisal path (story 71-21,
    space_opera/perseus_cloud), which deliberately resolves with no persisted
    initiative. A space_opera SWN fight with empty initiative must STILL reprise
    on the legacy path and NOT raise — proving the fail-loud is bound to the WWN
    module class (ADR-117 isinstance), not applied to all SwnRulesetModule.

    GREEN by design today; the guard fails only if Dev over-broadens the
    fail-loud to the whole SWN family and breaks 71-21.
    """
    from tests.integration.test_opponent_reprisal_e2e import (
        _load_space_opera_pack,
        _make_encounter,
        _make_snapshot,
    )

    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    # The space_opera firefight fixture carries NO initiative (legacy SWN path).
    snap = _make_snapshot(player_ac=2, player_hp=12)
    enc = _make_encounter()
    assert not enc.initiative, "fixture precondition: SWN legacy path has no persisted initiative"
    player_core = snap.find_creature_core("Nova")
    assert player_core is not None
    hp_before = player_core.hp.current

    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    # Must NOT raise — and the legacy reprisal must still ablate the AC-2 player.
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
        "the SWN legacy reprisal must still fire with no initiative (WWN "
        "fail-loud must not regress 71-21's space_opera path)"
    )
