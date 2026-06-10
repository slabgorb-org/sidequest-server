"""Story 102-2 — the dice path routes a cast_spell beat into the WN cast spine.

The AC5b spellcast blocker (epic 102, measured gap #2): clicking "Work a
Spell" in the confrontation overlay fires ``dispatch_dice_throw`` → generic
INT throw. No ``wwn.spell.cast`` span, ``casts_remaining`` stays 2/2, no
resource spend — the narration claims magic happened while the GM panel shows
the mechanic silent (the exact Illusionism the OTEL doctrine catches).

Contract pinned here, on the REAL heavy_metal pack (ruleset: wwn, cast_spell
beat + spells_wwn.yaml catalog), through the production dice seam:

  1. DICE_THROW with ``beat_id=cast_spell`` + ``spell_id`` reaches the SAME
     cast spine the narrator apply_beat path uses: ``wwn.spell.cast`` fires,
     exactly one cast is spent, spell damage ablates the defender.
  2. WWN High Magic casting is NOT gated on the d20 face — SRD casting is
     automatic (saves defend). A face of 1 still casts. "Instead of resolving
     as a generic INT dice throw" (story title) is load-bearing.
  3. Parity with the apply_beat path: same spell + same pinned rng produce
     the same mechanical deltas and the same span shape. One cast
     implementation, two entry points (epic 102 "Reuse-first").
  4. Malformed cast commits are LOUD, TYPED rejections (``DiceDispatchError``
     — the UI-renderable dispatch error idiom): cast beat with no spell_id,
     spell_id unknown to the resolved catalog, spell_id on a non-cast beat.
     No silent generic-INT resolution remains possible for cast beats.
  5. Economy refusals (no casts remaining) mirror the spine's
     refused-but-recorded semantics: ``wwn.spell.cast refused=True``, no
     state change, no raise — parity with apply_beat refusals.
  6. Regression: non-cast beats without spell_id behave exactly as today.

Determinism: every rng call on the cast path (defender save d20, damage
dice, downed-seam saves, opponent reprisal) resolves through the stdlib
``random`` module object shared by every importer — pinning
``random.randint`` pins them all, whichever module the implementation ends
up rolling from.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# Authored in heavy_metal rules.yaml (combat ConfrontationDef).
_CAST_BEAT = "cast_spell"
_STRIKE_BEAT = "committed_blow"  # strike, damage_override (deterministic)
# Authored in heavy_metal spells_wwn.yaml: level 1, physical save, 1d6/level.
_SPELL = "wracking_bolt"

_SPAN_CAST = "wwn.spell.cast"

_STATS = {"STR": 12, "DEX": 10, "CON": 10, "INT": 14, "WIS": 10, "CHA": 10}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_content(), reason="sidequest-content not on disk"
)


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _make_caster(name: str, *, casts_remaining: int = 2, level: int = 1):
    """A Necromancer with a hydrated WWN SpellcastingState.

    Built directly (not through chargen) — chargen seeding is proven by
    tests/integration/test_wwn_heavy_metal_dispatch.py; this suite tests the
    dispatch seam, which only reads ``core.spellcasting``.

    ``level`` drives wracking_bolt's damage dice (damage_per_level →
    caster_level × d6) — the killing-cast test needs level 2 so 2d6 pinned
    high (12) overkills the 10-HP seeded opponent through a failed save.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.game.wwn_magic import SpellcastingState

    core = CreatureCore(
        name=name,
        description="A debt-keeper of the grave ledgers.",
        personality="cold",
        inventory=Inventory(),
        hp={"current": 12, "max": 12, "base_max": 12},
        level=level,
        spellcasting=SpellcastingState(
            prepared=[_SPELL],
            casts_remaining=casts_remaining,
            casts_per_day=2,
            max_spell_level=1,
        ),
    )
    return Character(
        core=core,
        char_class="Necromancer",
        race="Human",
        backstory="—",
        stats=dict(_STATS),
    )


def _seat_combat(pack, caster_name: str, opponent: str, *, caster_level: int = 1):
    """Seat the real heavy_metal Blade-work combat via the production seam."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(_make_caster(caster_name, level=caster_level))
    snap.character_locations[caster_name] = "The Reliquary Gate"

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=caster_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="heavy_metal",
    )
    assert enc is not None, "seating Blade-work must produce an encounter"
    snap.encounter = enc
    opp_core = snap.find_creature_core(opponent)
    assert opp_core is not None, "opponent core must resolve (defender HP)"
    return snap, enc, opp_core


def _dispatch(
    *,
    pack,
    snap,
    enc,
    caster_name: str,
    beat_id: str,
    spell_id: str | None,
    face: int = 1,
):
    """Drive the production dice seam with a beat commit."""
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    payload_kwargs: dict[str, object] = {
        "request_id": "req-102-2",
        "throw_params": ThrowParams(
            velocity=(0.0, 5.0, -2.0),
            angular=(1.0, 1.0, 1.0),
            position=(0.5, 0.5),
        ),
        "face": [face],
        "beat_id": beat_id,
    }
    if spell_id is not None:
        payload_kwargs["spell_id"] = spell_id

    broadcasts: list[object] = []
    return dispatch_dice_throw(
        payload=DiceThrowPayload(**payload_kwargs),  # type: ignore[arg-type]
        rolling_player_id="player-vesska",
        character_name=caster_name,
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="hm-102-2-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )


def _cast_spans(otel_capture):
    return [s for s in otel_capture.get_finished_spans() if s.name == _SPAN_CAST]


# ─────────────────────────────────────────────────────────────────────────────
# AC1: the cast beat fires the WN cast spine through the dice path
# ─────────────────────────────────────────────────────────────────────────────


def test_cast_beat_with_spell_id_fires_wwn_cast_spine(otel_capture, monkeypatch):
    """DICE_THROW(cast_spell, spell_id) must spend a cast, ablate the defender,
    and fire wwn.spell.cast — with a d20 face of 1.

    The low face is load-bearing: WWN High Magic casting is automatic (the
    DEFENDER saves); gating the cast on the INT throw would be a generic-INT
    resolution wearing a robe. rng pinned low: defender save d20=1 (fails →
    full damage), damage die 1d6=1 → exactly 1 HP ablated.
    """
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_core = snap.find_creature_core("Vesska")
    hp_before = opp_core.hp.current

    _dispatch(
        pack=pack, snap=snap, enc=enc,
        caster_name="Vesska", beat_id=_CAST_BEAT, spell_id=_SPELL, face=1,
    )

    assert caster_core.spellcasting.casts_remaining == 1, (
        "the dice-path cast must spend exactly one cast (was 2); "
        f"got {caster_core.spellcasting.casts_remaining} — the generic INT "
        "throw spends nothing (the measured AC5b gap)"
    )
    assert opp_core.hp.current == hp_before - 1, (
        f"wracking_bolt (1d6 pinned to 1, save failed) must ablate exactly 1 HP; "
        f"before={hp_before} after={opp_core.hp.current}"
    )
    spans = _cast_spans(otel_capture)
    assert len(spans) == 1, (
        f"the dice path must emit exactly one wwn.spell.cast span (the GM-panel "
        f"lie detector); got {len(spans)}"
    )
    attrs = spans[0].attributes or {}
    assert attrs.get("spell_id") == _SPELL
    assert attrs.get("refused") is False


def test_cast_outcome_is_independent_of_the_d20_face(otel_capture, monkeypatch):
    """face=1 and face=20 must produce IDENTICAL cast results — span attrs,
    HP delta, and cast spend — even though the d20 outcome TIER differs
    (Fail-ish vs Success). If the implementation gated the cast on the throw,
    the low face would refuse/skip and the attribute sets would diverge.
    (Review rework: comparing both runs attr-by-attr distinguishes "cast
    ignores the d20" from "damage happens to be pinned"; a single-run
    arithmetic check could not.)"""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_heavy_metal()

    deltas: list[int] = []
    casts: list[int] = []
    for face in (1, 20):
        snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
        hp_before = opp_core.hp.current
        _dispatch(
            pack=pack, snap=snap, enc=enc,
            caster_name="Vesska", beat_id=_CAST_BEAT, spell_id=_SPELL, face=face,
        )
        deltas.append(hp_before - opp_core.hp.current)
        casts.append(snap.find_creature_core("Vesska").spellcasting.casts_remaining)

    assert deltas[0] == deltas[1] == 1, (
        f"spell damage must be face-independent (save + damage dice drive it); "
        f"face=1 delta {deltas[0]}, face=20 delta {deltas[1]}"
    )
    assert casts == [1, 1], f"exactly one cast spent per run; got {casts}"

    spans = _cast_spans(otel_capture)
    assert len(spans) == 2, f"one cast span per run; got {len(spans)}"
    low, high = (spans[0].attributes or {}), (spans[1].attributes or {})
    for key in ("spell_id", "refused", "save", "save_made", "damage"):
        assert low.get(key) == high.get(key), (
            f"span attr {key!r} diverges between face=1 ({low.get(key)!r}) and "
            f"face=20 ({high.get(key)!r}) — the d20 face is leaking into the cast"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC2: parity with the narrator apply_beat path — one spine, two entry points
# ─────────────────────────────────────────────────────────────────────────────


def test_cast_parity_with_apply_beat_path(otel_capture, monkeypatch):
    """The same spell cast via _resolve_wwn_cast_for_beat (reference) and via
    dispatch_dice_throw must produce equivalent mechanical outcomes and
    equivalent wwn.spell.cast span shapes."""
    from sidequest.agents.orchestrator import BeatSelection
    from sidequest.server.narration_apply import _resolve_wwn_cast_for_beat

    from sidequest.protocol.dice import RollOutcome

    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_heavy_metal()

    # Reference: the narrator apply_beat path on world A. Review rework: the
    # reference BeatSelection carries the SAME outcome tier the dice path
    # derives from face=2 (2 + INT mod vs the server DC → a plain Fail, and
    # not a nat-1 special) so the two invocations are equal on EVERY field —
    # if the spine ever starts reading ``sel.outcome``, this stays a real
    # parity proof instead of silently comparing unequal inputs.
    snap_a, enc_a, opp_a = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_a = snap_a.find_creature_core("Vesska")
    hp_before_a = opp_a.hp.current
    cdef = next(
        c for c in pack.rules.confrontations if c.confrontation_type == "combat"
    )
    actor_a = enc_a.find_actor("Vesska")
    assert actor_a is not None
    _resolve_wwn_cast_for_beat(
        sel=BeatSelection(
            actor="Vesska",
            beat_id=_CAST_BEAT,
            outcome=RollOutcome.Fail,
            spell_id=_SPELL,
        ),
        actor=actor_a,
        snapshot=snap_a,
        pack=pack,
        encounter=enc_a,
        cdef=cdef,
    )

    # Under test: the dice path on world B (face=2 → outcome tier Fail).
    snap_b, enc_b, opp_b = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_b = snap_b.find_creature_core("Vesska")
    hp_before_b = opp_b.hp.current
    _dispatch(
        pack=pack, snap=snap_b, enc=enc_b,
        caster_name="Vesska", beat_id=_CAST_BEAT, spell_id=_SPELL, face=2,
    )

    # Mechanical parity.
    assert (
        caster_b.spellcasting.casts_remaining == caster_a.spellcasting.casts_remaining
    ), "both entry points must spend the same number of casts"
    assert (hp_before_b - opp_b.hp.current) == (hp_before_a - opp_a.hp.current), (
        "both entry points must apply the same spell damage under the same rng"
    )

    # Span-shape parity (the GM panel must not be able to tell the paths apart
    # on the attributes that prove the mechanic engaged).
    spans = _cast_spans(otel_capture)
    assert len(spans) == 2, f"expected one cast span per path; got {len(spans)}"
    ref, dut = (spans[0].attributes or {}), (spans[1].attributes or {})
    for key in ("spell_id", "refused", "save", "save_made", "damage"):
        assert dut.get(key) == ref.get(key), (
            f"span attr {key!r} diverges between apply_beat ({ref.get(key)!r}) "
            f"and dice path ({dut.get(key)!r})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 edges: malformed cast commits are loud, typed rejections
# ─────────────────────────────────────────────────────────────────────────────


def test_cast_beat_without_spell_id_is_loud_typed_rejection(otel_capture):
    """A cast_spell commit with no spell_id is a malformed request (the UI
    picker always sends one) — DiceDispatchError, zero state mutation. The
    silent alternative is today's bug: a generic INT throw."""
    from sidequest.server.dispatch.dice import DiceDispatchError

    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_core = snap.find_creature_core("Vesska")
    hp_before = opp_core.hp.current

    with pytest.raises(DiceDispatchError):
        _dispatch(
            pack=pack, snap=snap, enc=enc,
            caster_name="Vesska", beat_id=_CAST_BEAT, spell_id=None,
        )

    assert caster_core.spellcasting.casts_remaining == 2, "no cast may be spent"
    assert opp_core.hp.current == hp_before, "no damage may land"
    assert not _cast_spans(otel_capture), (
        "a rejected malformed commit must not fabricate a wwn.spell.cast span"
    )


def test_cast_beat_with_unknown_spell_id_is_loud_typed_rejection(otel_capture):
    """A spell_id absent from the resolved catalog is a client/content bug —
    reject loudly with the typed dispatch error, never improvise."""
    from sidequest.server.dispatch.dice import DiceDispatchError

    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_core = snap.find_creature_core("Vesska")
    hp_before = opp_core.hp.current

    with pytest.raises(DiceDispatchError):
        _dispatch(
            pack=pack, snap=snap, enc=enc,
            caster_name="Vesska", beat_id=_CAST_BEAT,
            spell_id="riff_of_unmaking_xyz",
        )

    assert caster_core.spellcasting.casts_remaining == 2
    assert opp_core.hp.current == hp_before


def test_spell_id_on_non_cast_beat_is_loud_typed_rejection(otel_capture):
    """spell_id on a strike beat is malformed — silently ignoring a mechanical
    request field is exactly the No Silent Fallbacks failure mode."""
    from sidequest.server.dispatch.dice import DiceDispatchError

    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
    hp_before = opp_core.hp.current

    with pytest.raises(DiceDispatchError):
        _dispatch(
            pack=pack, snap=snap, enc=enc,
            caster_name="Vesska", beat_id=_STRIKE_BEAT, spell_id=_SPELL, face=20,
        )

    assert opp_core.hp.current == hp_before, (
        "the rejected commit must not have half-applied the strike"
    )
    assert not _cast_spans(otel_capture)


# ─────────────────────────────────────────────────────────────────────────────
# Economy refusal: mirrors the spine's refused-but-recorded semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_cast_with_no_casts_remaining_is_refused_not_generic(otel_capture, monkeypatch):
    """casts_remaining=0 is a VALID request refused by the economy (a stale
    overlay can race the gate) — the spine refuses loudly on the span
    (refused=True), spends nothing, damages nothing, and does NOT raise.
    Parity with apply_beat refusals; and emphatically NOT a generic INT
    resolution."""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_core = snap.find_creature_core("Vesska")
    caster_core.spellcasting.casts_remaining = 0
    hp_before = opp_core.hp.current

    _dispatch(
        pack=pack, snap=snap, enc=enc,
        caster_name="Vesska", beat_id=_CAST_BEAT, spell_id=_SPELL,
    )

    assert caster_core.spellcasting.casts_remaining == 0
    assert opp_core.hp.current == hp_before, "a refused cast must not damage"
    spans = _cast_spans(otel_capture)
    assert len(spans) == 1, (
        "a refused cast must still be RECORDED on wwn.spell.cast "
        "(refused-but-recorded, never a silent no-op)"
    )
    assert (spans[0].attributes or {}).get("refused") is True


# ─────────────────────────────────────────────────────────────────────────────
# Regression: non-cast beats are byte-for-byte today's behavior
# ─────────────────────────────────────────────────────────────────────────────


def test_strike_beat_without_spell_id_regression_unchanged(otel_capture, monkeypatch):
    """The existing strike path must be untouched: HP ablates through the
    strike channel, no wwn.spell.cast span, no cast spent."""
    monkeypatch.setattr(
        "sidequest.server.dispatch.damage_roll.random.randint", lambda a, b: a
    )
    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_core = snap.find_creature_core("Vesska")
    hp_before = opp_core.hp.current

    _dispatch(
        pack=pack, snap=snap, enc=enc,
        caster_name="Vesska", beat_id=_STRIKE_BEAT, spell_id=None, face=20,
    )

    assert opp_core.hp.current < hp_before, (
        "committed_blow must still ablate HP exactly as before 102-2"
    )
    assert caster_core.spellcasting.casts_remaining == 2, (
        "a strike must never touch the cast economy"
    )
    assert not _cast_spans(otel_capture), (
        "a strike must never emit wwn.spell.cast"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Review rework (102-2 round 2) — ADR-139 integrity on the kill path
# ─────────────────────────────────────────────────────────────────────────────


def test_killing_cast_resolves_encounter_and_suppresses_reprisal(
    otel_capture, monkeypatch
):
    """[HIGH, review round 2] A dice-path cast that DROPS the opponent must
    end the fight — the dead opponent must NOT take its reprisal swing.

    The reprisal gate reads ``apply_result.resolved`` (set by apply_beat),
    but a killing cast resolves the encounter in the SPINE via
    ``check_hp_depletion``, after apply_beat — so the gate sees a stale False
    and the corpse attacks the winner (ADR-139 win-condition liveness).

    rng pin is split by die size: every d20 (defender save, downed-seam
    saves, any reprisal to-hit) rolls 1, every damage die rolls max — so a
    level-2 wracking_bolt is 2d6=12 vs the failed save, overkilling the
    10-HP seeded opponent in one cast. The PRIMARY assertion is the span:
    ``encounter.opponent_attack_resolved`` fires on every reprisal attempt
    (hit or miss), so its absence proves the reprisal never ran — a
    player-HP check alone would pass vacuously when the pinned reprisal
    d20=1 happens to miss.
    """
    monkeypatch.setattr("random.randint", lambda a, b: a if b == 20 else b)
    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(
        pack, "Vesska", "Furnace Thrall", caster_level=2
    )
    caster_core = snap.find_creature_core("Vesska")
    player_hp_before = caster_core.hp.current

    _dispatch(
        pack=pack, snap=snap, enc=enc,
        caster_name="Vesska", beat_id=_CAST_BEAT, spell_id=_SPELL, face=2,
    )

    assert opp_core.hp.current == 0, (
        f"precondition: 2d6 pinned high (12) through a failed save must drop "
        f"the 10-HP opponent; hp={opp_core.hp.current}"
    )
    assert enc.resolved is True, (
        "a killing cast must resolve the encounter via check_hp_depletion "
        "(the win condition the spine fires)"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "encounter.opponent_attack_resolved" not in span_names, (
        "the DEAD opponent took its reprisal swing — the reprisal gate read "
        "the stale apply_result.resolved instead of the authoritative "
        "encounter.resolved the cast spine set (ADR-139 win-condition "
        f"liveness); got spans: {span_names}"
    )
    assert caster_core.hp.current == player_hp_before, (
        "no reprisal damage may land in a fight that is already won"
    )


def test_cast_on_opposed_check_confrontation_rejects_loudly(otel_capture, monkeypatch):
    """[MEDIUM, review round 2] A wwn cast_spell commit on an opposed_check
    ConfrontationDef must be a loud typed rejection — TODAY it passes
    validation and then the opposed branch silently skips the cast spine
    entirely (no span, no spend, no damage: the exact silent-fallback shape
    this epic exists to kill). No current content ships the combination;
    this pins the seam shut before someone authors it."""
    from sidequest.genre.models.rules import ResolutionMode
    from sidequest.server.dispatch.dice import DiceDispatchError

    pack = _load_heavy_metal()
    snap, enc, opp_core = _seat_combat(pack, "Vesska", "Furnace Thrall")
    caster_core = snap.find_creature_core("Vesska")
    cdef = next(
        c for c in pack.rules.confrontations if c.confrontation_type == "combat"
    )
    # monkeypatch (not direct assignment) so the shared loaded-pack object is
    # restored after the test — load_genre_pack may cache instances.
    monkeypatch.setattr(cdef, "resolution_mode", ResolutionMode.opposed_check)

    with pytest.raises(DiceDispatchError):
        _dispatch(
            pack=pack, snap=snap, enc=enc,
            caster_name="Vesska", beat_id=_CAST_BEAT, spell_id=_SPELL, face=5,
        )

    assert caster_core.spellcasting.casts_remaining == 2, (
        "no cast may be spent on a rejected opposed_check commit"
    )
    assert not _cast_spans(otel_capture), (
        "a rejected commit must not fabricate a wwn.spell.cast span"
    )
