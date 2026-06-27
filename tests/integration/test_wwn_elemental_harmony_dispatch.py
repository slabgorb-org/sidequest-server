"""elemental_harmony → WWN — real-chargen caster casts through the PRODUCTION seam.

Originally Plan-3 Task 14's "cast a damage spell into combat through the narrator
apply path" proof. That path was DE-NATIVIZED by #1050 (ADR-143): a live WN
hp_depletion combat is engine-owned, so a ``cast_spell`` beat fed to
``_apply_narration_result_to_snapshot`` is now DROPPED by the
``is_live_wn_combat`` firewall (``encounter.wn_combat_beat_dropped_*``) — the
cast never spends. The regression went unnoticed because this integration test
skips in CI (the ``_has_real_content()`` guard); it ran red only locally. Story
158-47 surfaced the heavy_metal twin and migrated all three WWN cast mirrors.

This test keeps what it uniquely proves — that a ``Channeler`` built through the
REAL ``CharacterBuilder.build()`` walk on the real elemental_harmony pack is
seeded with a usable ``SpellcastingState`` from the class's ``starting_prepared``
— and proves that seeded caster's spell resolves end-to-end through the
production dice seam:

  1. real chargen seeds ``spellcasting`` (prepared == cinder_lance, river_step;
     2 casts);
  2. committing the cast via ``dispatch_dice_throw`` spends exactly one cast (2 -> 1);
  3. the opponent's ``core.hp.current`` is ablated through the WWN cast spine;
  4. the ``wwn.spell.cast`` span fired with refused==False (the GM-panel lie detector).

The exhaustive cast-spine contract (face-independence, refusals, malformed commits,
apply_beat↔dice parity) lives in
``tests/integration/test_dice_path_spell_cast_102_2.py`` on a hand-built caster;
this is the chargen-built counterpart, so they do not duplicate.

rng is pinned via monkeypatch so the save + damage rolls are deterministic.

Skips cleanly when sidequest-content is not present on disk (mirrors the sibling
integration tests' skip pattern).
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# Authored opponent stats on Martial Exchange's opponent_default_stats
# (rules.yaml: hp: 8, armor_class: 12). The wuxia mook.
_OPPONENT_HP = 8
_OPPONENT_AC = 12

# The Channeler's authored starting_prepared (classes.yaml).
_EXPECTED_PREPARED = ["cinder_lance", "river_step"]
_DAMAGE_SPELL = "cinder_lance"  # 1d6, damage_per_level, save: evasion


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_elemental_harmony():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("elemental_harmony"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _build_channeler(pack, name: str):
    """Build a real Channeler from the real pack via CharacterBuilder.build().

    Walks the real char_creation scenes, selecting the first choice in the
    origins scene ("The Ember Isles", class_hint: Channeler) so build() resolves
    the real Channeler ClassDef and seed_wwn_magic seeds core.spellcasting from
    the class's starting_prepared. The remaining scenes are advanced
    generically (choice 0 / auto-advance / followup) until confirmation.
    """
    from sidequest.game.builder import CharacterBuilder
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    scenes = resolve_char_creation_scenes(pack, world_slug=None)
    assert scenes, "elemental_harmony must declare char_creation scenes"

    builder = CharacterBuilder(
        scenes=scenes,
        rules=pack.rules,
        backstory_tables=pack.backstory_tables,
    ).with_lobby_name(name)
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    assert pack.classes, "elemental_harmony must declare classes (Channeler)"
    builder = builder.with_classes(pack.classes)

    # Drive scenes to confirmation. The origins scene's choice 0 is the
    # Channeler-hinted "Ember Isles"; every other scene is advanced generically.
    _guard = 0
    while not builder.is_confirmation():
        _guard += 1
        assert _guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup("Channeler of the Ember Isles")
            continue
        scene = builder.current_scene()
        if not scene.choices:
            # display-only scene OR name-entry scene
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_freeform(name)
            continue
        builder.apply_choice(0)

    character = builder.build(name)
    return character


def _seat_martial_exchange(pack, snap, *, caster_name: str, opponent: str, location: str):
    """Seat a real elemental_harmony Martial Exchange via the PRODUCTION path.

    Uses ``instantiate_encounter_from_trigger`` so the opponent's CreatureCore
    is seeded with the authored hp/AC and reachable via find_creature_core —
    exactly as in live play. Returns the StructuredEncounter.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap.character_locations[caster_name] = location
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=caster_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug="elemental_harmony",
    )
    assert enc is not None, "seating Martial Exchange must produce an encounter"
    snap.encounter = enc
    return enc


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_wwn_cast_spell_routes_through_wwn_module_on_real_elemental_harmony(
    otel_capture, monkeypatch
):
    """The mandated end-to-end proof — real pack, production dice seam."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    pack = _load_elemental_harmony()
    assert pack.rules.ruleset == "wwn", "elemental_harmony must be bound ruleset: wwn"

    # ── Build a real Channeler via the real builder/build() ────────────────
    caster_name = "Mei Lin"
    caster = _build_channeler(pack, caster_name)
    assert caster.char_class == "Channeler", (
        f"chargen must resolve the Channeler class; got {caster.char_class!r}"
    )
    sc = caster.core.spellcasting
    assert sc is not None, "Channeler must be seeded with a SpellcastingState"
    assert sc.prepared == _EXPECTED_PREPARED, (
        f"Channeler starting_prepared must seed prepared; got {sc.prepared!r}"
    )
    assert _DAMAGE_SPELL in sc.prepared, "Channeler must have cinder_lance (damage spell) prepared"
    assert sc.casts_remaining == 2, f"Channeler must start with 2 casts; got {sc.casts_remaining}"

    # ── Snapshot + seat the Martial Exchange via the production seam ───────
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(caster)

    opponent = "Jade Duelist"
    enc = _seat_martial_exchange(
        pack, snap, caster_name=caster_name, opponent=opponent, location="Courtyard"
    )

    # Pin the WN sealed-round initiative (the seam's 1d8+DEX roll is unseeded) so
    # the caster acts first and the opponent answers — the walk order can never
    # flip on a random roll. Mirrors test_dice_path_spell_cast_102_2._seat_combat.
    enc.initiative = [
        InitiativeEntry(token_id=caster_name, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]

    opponent_core = snap.find_creature_core(opponent)
    assert opponent_core is not None, (
        "opponent core must be reachable via find_creature_core — without it the "
        "WWN cast has no defender HP to ablate"
    )
    assert opponent_core.armor_class == _OPPONENT_AC, "opponent AC from opponent_default_stats"
    assert opponent_core.hp.current == _OPPONENT_HP, "opponent HP from opponent_default_stats"

    casts_before = sc.casts_remaining
    hp_before = opponent_core.hp.current

    # ── Cast through the PRODUCTION dice seam, NOT a narrator beat selection ──
    # A live WN hp_depletion combat is engine-owned (ADR-143): the narrator is
    # de-nativized and a stray ``cast_spell`` beat fed to the narration-apply
    # path is dropped by the is_live_wn_combat firewall. Combat casting reaches
    # the WWN cast spine the way a player does it — throwing the die with a
    # ``spell_id`` sidecar (story 102-2). rng pinned low: defender save d20=1
    # (fails → full damage), cinder_lance 1d6 pinned to 1 → exactly 1 HP ablated,
    # below the 8-HP kill; the server reprisal rolls min and misses.
    monkeypatch.setattr("random.randint", lambda a, b: a)

    dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="req-158-47-eh",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[1],
            beat_id="cast_spell",
            spell_id=_DAMAGE_SPELL,
        ),
        rolling_player_id="player-mei-lin",
        character_name=caster_name,
        character_stats=dict(caster.stats),
        encounter=enc,
        pack=pack,
        genre_slug="elemental_harmony",
        session_id="eh-158-47-session",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    # ── Assertion 1: the cast spent one cast (2 -> 1) through the dice spine ─
    caster_after = snap.find_creature_core(caster_name)
    assert caster_after is not None
    sc_after = caster_after.spellcasting
    assert sc_after is not None
    assert sc_after.casts_remaining == casts_before - 1, (
        f"casting cinder_lance must spend exactly one cast through the production "
        f"dice seam; before={casts_before} after={sc_after.casts_remaining}"
    )

    # ── Assertion 2: opponent HP reduced (damage through the HP channel) ───
    assert opponent_core.hp.current < hp_before, (
        f"cinder_lance damage must ablate the opponent's HP through the WWN cast "
        f"spine; before={hp_before} after={opponent_core.hp.current}"
    )

    # ── Assertion 3: wwn.spell.cast span fired with refused=False (lie detector) ─
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.spell.cast"]
    assert len(spans) >= 1, (
        f"the WWN cast spine must emit a wwn.spell.cast span on the dice path "
        f"(GM-panel lie detector); got span names "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert spans[-1].attributes.get("refused") is False, (
        "the cast was a valid cast (caster has the spell prepared + a cast remaining), "
        "so the wwn.spell.cast span must record refused=False"
    )
