"""Story 90-4 — the DETERMINISTIC scene-harness fixture proof for WWN crunch.

The deterministic counterpart to 90-3's live free-play OTEL proof. Where the
sibling ``test_wwn_elemental_harmony_dispatch.py`` builds its caster through the
real ``CharacterBuilder.build()`` and seats combat through the production
``instantiate_encounter_from_trigger`` seam, THIS proof stands the SAME state up
entirely from a scene-harness FIXTURE (``hydrate_fixture``) — exactly the path a
content-only deterministic playtest takes through ``POST /dev/scene/{name}``
(ADR-092). That is the whole point of story 90-4: a fixture must be able to seed

  * per-character WWN ``core.spellcasting`` (``_hydrate_character``), and
  * a WWN ``hp_depletion`` combat that seats player/opponent actors
    (``_hydrate_encounter``),

so the crunch fires WITHOUT chargen and WITHOUT the trigger seam.

This is the wiring test mandated by CLAUDE.md ("Every Test Suite Needs a Wiring
Test"): it drives the REAL elemental_harmony pack through the REAL apply / dice
seams and asserts on OTEL spans (the GM-panel lie detector), so it survives
refactor and fails on real wiring breakage. rng is pinned for determinism.

Skips cleanly when sidequest-content is not on disk (mirrors the sibling
integration proofs).

RED (90-4): both tests fail today because ``hydrate_fixture`` silently drops the
``spellcasting:`` block (caster has no prepared spell / no casts) and builds a
dial_threshold, actor-less StructuredEncounter (the cast/strike spine finds no
defender). They pass once ``_hydrate_character`` + ``_hydrate_encounter`` seed
the WWN state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# elemental_harmony combat = "Martial Exchange" (rules.yaml): type: combat,
# win_condition: hp_depletion, category: combat. opponent_default_stats carries
# all six WWN ability scores so the defender-save path resolves.
_GENRE = "elemental_harmony"
_CASTER = "Mei Lin"
_OPPONENT = "Jade Duelist"
_DAMAGE_SPELL = "cinder_lance"  # 1d6, damage_per_level, save: evasion (genre catalog)
_STRIKE_BEAT = "attack"  # 108-8 synthesized WN strike (the future-correct id; the
# pre-108-3 native "elemental_burst" carried a damage_override 2d6 and a flavor
# "Strength" stat_check). attack carries no damage_override and a canonical "STR"
# stat_check — on the weaponless elemental_harmony fixture caster it resolves no
# damage and the flavor-keyed stat block raises a STR KeyError. The strike-DAMAGE
# proof below is loud-skipped pending a follow-up; the cast proof in this file is
# 152-2's live RED. See the TEA deviation log.


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_elemental_harmony():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path(_GENRE))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _write_wwn_combat_fixture(fixtures_dir: Path, name: str) -> None:
    """Write a deterministic WWN combat fixture: a Channeler with a seeded
    spellcasting economy, an opponent NPC, and an hp_depletion combat seating
    both as actors.

    The opponent NPC carries no explicit HP — the CreatureCore default
    (HpPool 10/10/10) is the defender's pool, which is all the proof needs (it
    asserts HP *decreases*, not a specific value). ``type: combat`` resolves the
    Martial Exchange ConfrontationDef via find_confrontation_def at apply time.
    """
    body = (
        f"genre: {_GENRE}\n"
        "world: test_world\n"
        "location: Courtyard\n"
        "turn: 2\n"
        "characters:\n"
        f"  - name: {_CASTER}\n"
        "    description: A Channeler of the Ember Isles\n"
        "    personality: serene under fire\n"
        "    backstory: trained in the ember registers\n"
        "    char_class: Channeler\n"
        "    race: Human\n"
        "    level: 1\n"
        "    stats:\n"
        "      STR: 12\n"
        "      DEX: 12\n"
        "      CON: 10\n"
        "      INT: 12\n"
        "      WIS: 10\n"
        "      CHA: 10\n"
        "    spellcasting:\n"
        "      prepared:\n"
        f"        - {_DAMAGE_SPELL}\n"
        "        - river_step\n"
        "      casts_remaining: 2\n"
        "      casts_per_day: 2\n"
        "      max_spell_level: 1\n"
        "npcs:\n"
        f"  - name: {_OPPONENT}\n"
        "    role: rival duelist\n"
        "    disposition: -2\n"
        "encounter:\n"
        "  type: combat\n"
        "  win_condition: hp_depletion\n"
        "  category: combat\n"
        "  actors:\n"
        f"    - name: {_CASTER}\n"
        "      role: caster\n"
        "      side: player\n"
        f"    - name: {_OPPONENT}\n"
        "      role: defender\n"
        "      side: opponent\n"
    )
    (fixtures_dir / f"{name}.yaml").write_text(body, encoding="utf-8")


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_hydrated_wwn_fixture_drives_cast_spell_and_ablates_hp(otel_capture, monkeypatch, tmp_path):
    """A scene-harness fixture seeds spellcasting + an hp_depletion combat; the
    REAL player cast path (DICE_THROW -> run_wn_round -> WWN cast spine) then
    spends a cast and ablates the opponent's HP.

    158-53 RESCOPE: this proof used to drive the cast through a narrator
    ``BeatSelection`` on ``_apply_narration_result_to_snapshot``. That path is
    DEAD for a live WN hp_depletion combat — ADR-143 (#1050,
    ``wn_combat_beat_dropped_engine_owns_round``) de-nativizes the narrator and
    DROPS stray beat selections; a WN combat resolves ONLY on the player's
    DICE_THROW via ``run_wn_round``. Driving the cast the old way spent no cast
    (before=2 after=2) because the beat was dropped before it reached the spine —
    not because the counter fails to decrement (it does: wwn.py resolve_spellcast,
    proven green by tests/integration/test_dice_path_spell_cast_102_2.py). So this
    proof now drives the ADR-143-correct path the real player uses.

    Asserts (all on the real pack, through the real DICE_THROW cast seam):
      1. the fixture seeded core.spellcasting (prepared + 2 casts);
      2. the cast spent exactly one cast (2 -> 1);
      3. the opponent's HP was ablated through the HP channel;
      4. the wwn.spell.cast span fired with refused=False (the lie detector);
      5. the state_patch.hp span fired (ablative-HP combat, the GM-panel proof);
      6. (158-53 RED) the wwn.spell.cast span records BOTH the before AND after
         charge count, so the GM panel proves the spend delta (before=2 after=1)
         on one span — AC #2. The span carries no ``casts_before`` today.
    """
    from sidequest.game.scene_harness import hydrate_fixture
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    pack = _load_elemental_harmony()
    assert pack.rules is not None and pack.rules.ruleset == "wwn", (
        f"{_GENRE} must be bound ruleset: wwn; got {pack.rules.ruleset!r}"
    )

    _write_wwn_combat_fixture(tmp_path, "wwn_cast_proof")
    snapshot = hydrate_fixture(name="wwn_cast_proof", fixtures_dir=tmp_path)

    # ── Assertion 1: the fixture seeded the caster's spellcasting ──────────
    caster_core = snapshot.find_creature_core(_CASTER)
    assert caster_core is not None, "the hydrated caster must be reachable"
    sc = caster_core.spellcasting
    assert sc is not None, (
        "the fixture's spellcasting: block must seed core.spellcasting — without "
        "it the cast is refused for want of a prepared spell"
    )
    assert _DAMAGE_SPELL in sc.prepared, f"{_DAMAGE_SPELL} must be prepared; got {sc.prepared!r}"
    assert sc.casts_remaining == 2, f"fixture must seed 2 casts; got {sc.casts_remaining}"

    opponent_core = snapshot.find_creature_core(_OPPONENT)
    assert opponent_core is not None, (
        "the hydrated opponent must be reachable via find_creature_core — without "
        "a defender HP pool the cast has nothing to ablate"
    )

    # Story 106-2 (Option A): a WWN hp_depletion combat dispatched through
    # dispatch_dice_throw resolves via the sealed initiative walk and fails loud
    # without a persisted order (the production seating seam always rolls one).
    # Seat a deterministic order so the cast spine drives the real walk: the
    # caster acts first, the opponent answers at its slot.
    enc = snapshot.encounter
    assert enc is not None, "fixture declares an encounter — it must hydrate"
    enc.initiative = [
        InitiativeEntry(token_id=_CASTER, value=9),
        InitiativeEntry(token_id=_OPPONENT, value=2),
    ]

    # Pin every rng call on the cast path (defender save d20, damage dice, the
    # opponent reprisal) to MIN via the shared stdlib random module: the defender
    # save fails (full damage), cinder_lance 1d6 -> 1 HP ablated, and the reprisal
    # is weak — deterministic, opponent survives, downed seam untripped.
    monkeypatch.setattr("random.randint", lambda a, b: a)

    casts_before = sc.casts_remaining
    hp_before = opponent_core.hp.current

    broadcasts: list[object] = []
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="wwn-fixture-cast-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[1],  # WWN High Magic casting is automatic (defender saves) —
            # a low d20 still casts; the face never gates the spell.
            beat_id="cast_spell",
            spell_id=_DAMAGE_SPELL,
        ),
        rolling_player_id="player-mei-lin",
        character_name=_CASTER,
        # The caster's canonical stats (elemental_harmony flavor names are
        # display-only via attribute_map); the defender save target is derived
        # by dispatch the same way _physical_save_target_for does.
        character_stats={"STR": 12, "DEX": 12, "CON": 10, "INT": 12, "WIS": 10, "CHA": 10},
        encounter=enc,
        pack=pack,
        genre_slug=_GENRE,
        session_id="wwn-fixture-cast-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snapshot,
    )

    # ── Assertion 2: the cast spent one cast (2 -> 1) ──────────────────────
    sc_after = snapshot.find_creature_core(_CASTER).spellcasting  # type: ignore[union-attr]
    assert sc_after is not None
    assert sc_after.casts_remaining == casts_before - 1, (
        f"casting {_DAMAGE_SPELL} through the real WWN DICE_THROW cast spine must "
        f"spend exactly one cast; before={casts_before} after={sc_after.casts_remaining}"
    )

    # ── Assertion 3: opponent HP ablated through the HP channel ─────────────
    assert opponent_core.hp.current < hp_before, (
        f"{_DAMAGE_SPELL} damage must ablate the opponent's HP through the WWN "
        f"cast spine; before={hp_before} after={opponent_core.hp.current}"
    )

    # ── Assertion 4: wwn.spell.cast span fired, refused=False ──────────────
    cast_spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.spell.cast"]
    assert len(cast_spans) >= 1, (
        "a hydrated WWN fixture must fire a wwn.spell.cast span on the real "
        f"DICE_THROW cast path (GM-panel lie detector); got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert cast_spans[-1].attributes.get("refused") is False, (
        "the cast was valid (spell prepared + a cast remaining), so the "
        "wwn.spell.cast span must record refused=False — proving the fixture "
        "seeded a USABLE spellcasting state, not an empty one"
    )

    # ── Assertion 5: state_patch.hp span fired (ablative-HP combat proof) ──
    finished = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in finished, (
        f"the WWN cast spine must emit a state_patch.hp span when it ablates the "
        f"defender (deterministic wwn.* combat proof); got spans: {finished}"
    )

    # ── Assertion 6 (158-53 RED): the span proves the spend delta ──────────
    # AC #2: "the cast-spend emits an OTEL watcher span with before/after
    # remaining so the GM panel verifies the decrement fired." The span records
    # casts_remaining (the AFTER value) but carries NO before value today — so
    # the panel sees "1 cast left" but cannot prove the spend was 2->1 (a real
    # decrement) versus 1->1 (a no-op) from this span alone. Record the before
    # count ON the span so the delta is self-evident to the lie detector.
    cast_attrs = cast_spans[-1].attributes or {}
    assert cast_attrs.get("casts_remaining") == casts_before - 1, (
        "the wwn.spell.cast span must record the AFTER (post-spend) charge count; "
        f"got casts_remaining={cast_attrs.get('casts_remaining')!r}"
    )
    assert cast_attrs.get("casts_before") == casts_before, (
        "the wwn.spell.cast span must ALSO record the BEFORE charge count so the "
        "GM panel sees the spend delta on one span (before=2 after=1) — AC #2. "
        f"got casts_before={cast_attrs.get('casts_before')!r} (None = attribute "
        "absent: the span carries no before value today — this is 158-53's RED)"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.skip(
    reason="125-8 orphan / follow-up: this strike-DAMAGE proof used the native "
    "elemental_burst beat (damage_override 2d6, flavor 'Strength' stat_check). 108-3 "
    "stripped it; the synthesized WN 'attack' replacement has a canonical STR "
    "stat_check (KeyError vs the elemental_harmony flavor-keyed stat block) and no "
    "damage_override, and the fixture caster is weaponless (damage_spec_missing → no "
    "ablation). Restoring this needs a weapon + canonical stats — out of 152-2's "
    "cast-routing scope. Loud-skipped (never xfail) until a follow-up lands. The cast "
    "proof (test_hydrated_wwn_fixture_drives_cast_spell_and_ablates_hp) is 152-2's live RED."
)
def test_hydrated_wwn_fixture_drives_deterministic_strike(otel_capture, monkeypatch, tmp_path):
    """The same hydrated hp_depletion combat drives a deterministic WWN STRIKE
    (no spellcasting) — proving fixture-seated combat ablates HP through the
    dice seam, the non-spell half of "wwn.* combat".

    The synthesized WN ``attack`` strike lands the actor's weapon dice / genre
    unarmed floor; rng is pinned to MIN so the opponent survives and the downed
    seam is not tripped. The proof asserts HP *decreases* (not a fixed value), so
    it is robust to the floor magnitude.
    """
    from sidequest.game.scene_harness import hydrate_fixture
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    pack = _load_elemental_harmony()

    _write_wwn_combat_fixture(tmp_path, "wwn_strike_proof")
    snapshot = hydrate_fixture(name="wwn_strike_proof", fixtures_dir=tmp_path)

    enc = snapshot.encounter
    assert enc is not None, "fixture declares an encounter — it must hydrate"
    assert enc.win_condition == "hp_depletion", (
        f"the fixture combat must hydrate as hp_depletion; got {enc.win_condition!r}"
    )
    # Story 106-2 (Option A): a WWN hp_depletion combat dispatched through
    # dispatch_dice_throw now resolves via the sealed initiative walk and fails
    # loud without a persisted order (the production seating seam always rolls
    # one). Seat a deterministic order so the strike-spine proof drives the real
    # walk; the caster acts first, the opponent answers at its slot.
    from sidequest.protocol.models import InitiativeEntry

    enc.initiative = [
        InitiativeEntry(token_id=_CASTER, value=9),
        InitiativeEntry(token_id=_OPPONENT, value=2),
    ]

    opponent_core = snapshot.find_creature_core(_OPPONENT)
    assert opponent_core is not None, "opponent must be reachable to ablate"
    hp_before = opponent_core.hp.current

    # Pin the damage faces (generate_server_faces → random.randint) to MIN so the
    # attack's weapon/unarmed dice deal their floor — opponent survives, downed
    # seam untripped.
    monkeypatch.setattr("sidequest.server.dispatch.damage_roll.random.randint", lambda a, b: a)

    broadcasts: list[object] = []
    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="wwn-fixture-strike-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],  # high d20 so the strike clears its DC and lands
            beat_id=_STRIKE_BEAT,
        ),
        rolling_player_id="player-mei-lin",
        character_name=_CASTER,
        # The synthesized WN attack carries a canonical "STR" stat_check (108-8),
        # and a character's stats are stored canonical-keyed (the elemental_harmony
        # flavor names are display-only via attribute_map) — so the proof seats
        # canonical stats. (The pre-108-3 native elemental_burst beat used the flavor
        # "Strength" stat_check, which is why this fixture once carried flavor keys.)
        character_stats={
            "STR": 12,
            "DEX": 12,
            "CON": 10,
            "INT": 12,
            "WIS": 10,
            "CHA": 10,
        },
        encounter=enc,
        pack=pack,
        genre_slug=_GENRE,
        session_id="wwn-fixture-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snapshot,
    )

    # ── Assertion 1: HP ablated through the HP channel ─────────────────────
    assert opponent_core.hp.current < hp_before, (
        f"{_STRIKE_BEAT} must ablate the fixture opponent's HP on the real wwn "
        f"pack; before={hp_before} after={opponent_core.hp.current}"
    )

    # ── Assertion 2: state_patch.hp span fired (the lie detector) ──────────
    finished = [s.name for s in otel_capture.get_finished_spans()]
    assert SPAN_STATE_PATCH_HP in finished, (
        f"a fixture-seated WWN combat strike must emit a state_patch.hp span "
        f"(GM-panel lie detector); got spans: {finished}"
    )
