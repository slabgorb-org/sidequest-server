"""heavy_metal → WWN — real-chargen caster casts through the PRODUCTION seam.

Originally story 87-2's "cast a damage spell into combat through the narrator
apply path" proof. That path was DE-NATIVIZED by #1050 (ADR-143): a live WN
hp_depletion combat is engine-owned, so a ``cast_spell`` beat fed to
``_apply_narration_result_to_snapshot`` is now DROPPED by the
``is_live_wn_combat`` firewall (``encounter.wn_combat_beat_dropped_*``) — the
cast never spends. The test went unnoticed because the heavy_metal pack-load
error masked it until barsoom was fixed (2026-06-27); story 158-47 then surfaced
the regression, misread as a chargen-seeding bug. It is not: chargen seeds the
spell correctly. The cast had to MIGRATE to the door a player actually uses in a
WN combat — the **DICE_THROW** seam (story 102-2) — which the firewall does not
gate.

This test keeps what it uniquely proves — that a caster built through the REAL
chargen FSM walk on the real heavy_metal pack is seeded with a usable
``SpellcastingState`` — and proves that seeded caster's spell actually resolves
end-to-end through the production dice seam:

  1. real chargen seeds ``spellcasting`` with the discovered damage spell prepared
     and at least one cast remaining;
  2. committing the cast via ``dispatch_dice_throw`` spends exactly one cast;
  3. the opponent's HP is ablated through the WWN cast spine;
  4. the ``wwn.spell.cast`` span fired with refused==False (the GM-panel lie detector).

The exhaustive cast-spine contract (face-independence, economy refusals, malformed
commits, apply_beat↔dice parity) lives in
``tests/integration/test_dice_path_spell_cast_102_2.py`` on a hand-built caster;
this test is the chargen-built counterpart, so they do not duplicate.

The caster class + damage spell are DISCOVERED from the loaded pack (not hardcoded),
so the proof does not couple to the specific spell ids the content authors.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# heavy_metal Blade-work opponent_default_stats (rules.yaml, Story 1).
_OPPONENT_HP = 10
_OPPONENT_AC = 12


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _discover_caster_and_damage_spell(pack):
    """Return (class_display, spell_id) for a caster whose starting_prepared holds
    a damage spell. RED-clear if none exists (Dev must give a caster a damage spell)."""
    cat = pack.wwn_spell_catalog
    assert cat is not None, (
        "spells_wwn.yaml must be authored (real magic) — wwn_spell_catalog is None"
    )
    damage_ids = {s.id for s in cat.spells if s.damage_die}
    assert damage_ids, "the spell catalog must include at least one damage spell"
    assert pack.classes is not None
    for cls in pack.classes:
        if cls.wwn_magic is None:
            continue
        for sid in cls.wwn_magic.starting_prepared:
            if sid in damage_ids:
                return cls.display_name, sid
    raise AssertionError(
        "no caster starts with a damage spell prepared — combat casting cannot ablate HP"
    )


def _build_caster(pack, name: str, *, class_display: str):
    from sidequest.game.builder import CharacterBuilder, FreeformInput, SceneResult
    from sidequest.genre.models import MechanicalEffects
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    scenes = resolve_char_creation_scenes(pack, world_slug=None)
    assert scenes, "heavy_metal must declare char_creation scenes"

    builder = CharacterBuilder(
        scenes=scenes,
        rules=pack.rules,
        backstory_tables=pack.backstory_tables,
    ).with_lobby_name(name)
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    builder = builder.with_classes(pack.classes)

    matched = False
    _guard = 0
    while not builder.is_confirmation():
        _guard += 1
        assert _guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup(name)
            continue
        scene = builder.current_scene()
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_freeform(name)
            continue
        idx = next(
            (
                i
                for i, c in enumerate(scene.choices)
                if c.mechanical_effects and c.mechanical_effects.class_hint == class_display
            ),
            None,
        )
        if idx is not None:
            matched = True
            builder.apply_choice(idx)
        else:
            builder.apply_choice(0)

    if not matched:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=class_display),
            )
        )
    return builder.build(name)


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_wwn_cast_spell_routes_through_wwn_module_on_real_heavy_metal(otel_capture, monkeypatch):
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.dice import dispatch_dice_throw
    from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger

    pack = _load_heavy_metal()
    assert pack.rules.ruleset == "wwn", "heavy_metal must be bound ruleset: wwn"

    class_display, spell_id = _discover_caster_and_damage_spell(pack)

    # ── Build the real caster; it must seed the spell we will cast ─────────
    caster_name = "Sael"
    caster = _build_caster(pack, caster_name, class_display=class_display)
    assert caster.char_class == class_display, (
        f"chargen must resolve {class_display}; got {caster.char_class!r}"
    )
    sc = caster.core.spellcasting
    assert sc is not None, f"{class_display} must be seeded with a SpellcastingState"
    assert spell_id in sc.prepared, (
        f"{class_display} must have {spell_id!r} prepared; got {sc.prepared!r}"
    )
    casts_before = sc.casts_remaining
    assert casts_before >= 1, "caster must start with at least one cast"

    # ── Seat the real Blade-work combat via the production seam ────────────
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(caster)
    opponent = "The Collector's Blade"
    snap.character_locations[caster_name] = "The Antechamber"
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

    # Pin the WN sealed-round initiative (the seam's 1d8+DEX roll is unseeded) so
    # the caster acts first and the opponent answers — the walk order can never
    # flip on a random roll. Mirrors test_dice_path_spell_cast_102_2._seat_combat.
    enc.initiative = [
        InitiativeEntry(token_id=caster_name, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]

    opponent_core = snap.find_creature_core(opponent)
    assert opponent_core is not None, "opponent core must be reachable to ablate"
    assert opponent_core.armor_class == _OPPONENT_AC, "opponent AC from opponent_default_stats"
    assert opponent_core.hp.current == _OPPONENT_HP, "opponent HP from opponent_default_stats"
    hp_before = opponent_core.hp.current

    # ── Cast through the PRODUCTION dice seam, NOT a narrator beat selection ──
    # A live WN hp_depletion combat is engine-owned (ADR-143): the narrator is
    # de-nativized and a stray ``cast_spell`` beat fed to the narration-apply
    # path is dropped by the is_live_wn_combat firewall. Combat casting reaches
    # the WWN cast spine the way a player does it — throwing the die with a
    # ``spell_id`` sidecar (story 102-2). rng pinned low: defender save d20=1
    # (fails → full damage), wracking_bolt 1d6 pinned to 1 → exactly 1 HP
    # ablated, well below the 10-HP kill; the server reprisal rolls min and misses.
    monkeypatch.setattr("random.randint", lambda a, b: a)

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="req-158-47",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[1],
            beat_id="cast_spell",
            spell_id=spell_id,
        ),
        rolling_player_id="player-sael",
        character_name=caster_name,
        character_stats=dict(caster.stats),
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id="hm-158-47-session",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    # 1. one cast spent — the chargen-seeded caster's cast routed through the
    #    production dice spine and spent exactly one cast.
    caster_after = snap.find_creature_core(caster_name)
    assert caster_after is not None and caster_after.spellcasting is not None
    assert caster_after.spellcasting.casts_remaining == casts_before - 1, (
        f"casting {spell_id!r} must spend exactly one cast through the production dice "
        f"seam; before={casts_before} after={caster_after.spellcasting.casts_remaining}"
    )

    # 2. opponent HP ablated through the WWN cast spine
    assert opponent_core.hp.current < hp_before, (
        f"{spell_id!r} damage must ablate the opponent's HP through the WWN cast spine; "
        f"before={hp_before} after={opponent_core.hp.current}"
    )

    # 3. wwn.spell.cast span fired with refused=False (the GM-panel lie detector)
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.spell.cast"]
    assert len(spans) >= 1, (
        f"the WWN cast spine must emit a wwn.spell.cast span on the dice path; got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert spans[-1].attributes.get("refused") is False, (
        "a valid cast (prepared + a cast remaining) must record refused=False"
    )
