"""WWN Content Plan 3 Task 15 — chargen wiring proof for the real elemental_harmony pack.

Proves that building each elemental_harmony archetype through the REAL
``CharacterBuilder.build()`` seeds the correct WWN magic state (Effort pools +
spellcasting + initial prepared) on the resulting character's ``core``, and that
the state survives a ``GameSnapshot`` serialize/deserialize round-trip.

Five archetype groups tested:

  1. Channeler (caster) — Effort pool + SpellcastingState with cinder_lance/river_step
  2. Spirit Medium (caster) — Effort pool + SpellcastingState with still_the_breath/ember_veil
  3. Martial Artist (Vowed, Effort-only) — Effort pool, spellcasting=None
  4. Guardian (Warrior) — effort={}, spellcasting=None
  5. Scholar / Wanderer (Experts) — effort={}, spellcasting=None

Effort-max formula (mirrored from seed_wwn_magic in builder.py):
  pool_max = effort_base + starting_skill_level + swn_attribute_modifier(wis_score)
  (Partial class: pool_max -= 1, min 1)

The governing stat is WISDOM → WIS (pack's attribute_map; the WN family uses the
canonical STR/DEX/CON/INT/WIS/CHA block). The test computes pool_max from the
BUILT character's actual WIS score (not hardcoded), so a stat-generation or
arrangement change will catch any drift.

Round-trip: serialize the Channeler character's snapshot via
``GameSnapshot.model_dump_json()`` + ``GameSnapshot.model_validate_json()``
and assert that effort + spellcasting + prepared survive identically.

Skips cleanly when sidequest-content is not present on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_elemental_harmony():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("elemental_harmony"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _build_archetype(pack, name: str, *, class_hint: str):
    """Build a character with the given class_hint via the REAL builder.

    Walks the real char_creation scenes for elemental_harmony:
      origins   (choices)
      pronouns  (choices + freeform)
      path      (choices)
      confirmation (no choices)

    (The pre-WWN "element"/affinity scene was removed 2026-05-31 — channeling
    is determined by the WWN class seeded from the origin's class_hint, not a
    standalone affinity pick.)

    For origins we select the first choice that carries the requested
    class_hint. For archetypes that have NO class_hint in origins
    (Guardian, Wanderer) we select origins choice 0 generically, then
    inject a SceneResult override so the REAL build() sees the right
    class_hint — this is the same as the user picking an origin and
    receiving a custom path, but expressed at the builder level. All
    remaining scenes are advanced generically (choice 0 / auto-advance).
    """
    from sidequest.game.builder import CharacterBuilder, FreeformInput, SceneResult
    from sidequest.genre.models import MechanicalEffects
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    scenes = resolve_char_creation_scenes(pack, world_slug=None)
    assert scenes, "elemental_harmony must declare char_creation scenes"

    builder = (
        CharacterBuilder(
            scenes=scenes,
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
        )
        .with_lobby_name(name)
        .with_classes(pack.classes)
    )
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)

    # Walk all scenes to the confirmation point.
    # At the origins scene, select the choice matching the requested class_hint
    # when one exists (Channeler, Martial Artist, Spirit Medium, Scholar).
    # For archetypes with no origins class_hint (Guardian, Wanderer), pick
    # choice 0 generically then inject the desired class_hint as a late
    # SceneResult — last-one-wins in accumulated(), so build() sees it.
    _origins_done = False
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
        # Origins scene: pick the matching class_hint choice when present.
        if not _origins_done and scene.id == "origins":
            idx = next(
                (
                    i
                    for i, c in enumerate(scene.choices)
                    if c.mechanical_effects and c.mechanical_effects.class_hint == class_hint
                ),
                None,
            )
            builder.apply_choice(idx if idx is not None else 0)
            _origins_done = True
        else:
            builder.apply_choice(0)

    # For Guardian and Wanderer (no origins class_hint), inject the class_hint
    # after the walk completes so build() accumulates it last-one-wins.
    if class_hint not in {"Channeler", "Spirit Medium", "Martial Artist", "Scholar"}:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=class_hint),
            )
        )

    return builder.build(name)


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_channeler_seeds_effort_and_spellcasting():
    """Channeler gets effort['channeler'] with correct max + SpellcastingState."""
    from sidequest.game.ruleset.swn import swn_attribute_modifier

    pack = _load_elemental_harmony()
    assert pack.rules.ruleset == "wwn"

    char = _build_archetype(pack, "Mei Lin", class_hint="Channeler")
    assert char.char_class == "Channeler", f"expected Channeler, got {char.char_class!r}"

    # 1. Effort pool
    assert "channeler" in char.core.effort, (
        f"Channeler must have effort['channeler']; got keys {list(char.core.effort)}"
    )
    pool = char.core.effort["channeler"]

    # Compute expected max from the BUILT char's actual WIS score (the governing
    # stat for WWN-magic classes; attribute_map WISDOM → WIS in rules.yaml).
    wis_score = char.stats.get("WIS", 10)
    effort_base = pack.rules.wwn.magic.effort_base  # == 1 per rules.yaml
    starting_skill_level = 1  # classes.yaml: effort_sources[0].starting_skill_level
    expected_max = effort_base + starting_skill_level + swn_attribute_modifier(wis_score)
    # partial=false → no -1 adjustment; floor 1 anyway
    expected_max = max(1, expected_max)

    assert pool.max == expected_max, (
        f"Channeler effort max mismatch: expected {expected_max} "
        f"(effort_base={effort_base} + skill={starting_skill_level} + "
        f"modifier={swn_attribute_modifier(wis_score)} for WIS={wis_score}), "
        f"got {pool.max}"
    )

    # 2. Spellcasting state
    sc = char.core.spellcasting
    assert sc is not None, "Channeler must be seeded with a SpellcastingState"

    # casts_per_day_by_level["1"] == 2 per classes.yaml
    channeler_def = next(c for c in pack.classes if c.display_name == "Channeler")
    casts_per_day = channeler_def.wwn_magic.casts_per_day_by_level["1"]
    assert sc.casts_per_day == casts_per_day, (
        f"Channeler casts_per_day must be {casts_per_day}, got {sc.casts_per_day}"
    )
    assert sc.casts_remaining == casts_per_day, (
        f"Channeler casts_remaining at chargen must equal casts_per_day={casts_per_day}; "
        f"got {sc.casts_remaining}"
    )

    # max_spell_level_by_level["1"] == 1
    expected_msl = channeler_def.wwn_magic.max_spell_level_by_level["1"]
    assert sc.max_spell_level == expected_msl, (
        f"Channeler max_spell_level must be {expected_msl}, got {sc.max_spell_level}"
    )

    # prepared == starting_prepared[:capacity]
    # prepared_by_level["1"] == 2; starting_prepared == ["cinder_lance", "river_step"]
    expected_prepared = channeler_def.wwn_magic.starting_prepared[:casts_per_day]
    assert sc.prepared == expected_prepared, (
        f"Channeler prepared must be {expected_prepared!r}, got {sc.prepared!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_spirit_medium_seeds_effort_and_spellcasting():
    """Spirit Medium gets effort['spirit_medium'] + SpellcastingState."""
    from sidequest.game.ruleset.swn import swn_attribute_modifier

    pack = _load_elemental_harmony()
    char = _build_archetype(pack, "River Song", class_hint="Spirit Medium")
    assert char.char_class == "Spirit Medium", f"expected Spirit Medium, got {char.char_class!r}"

    assert "spirit_medium" in char.core.effort, (
        f"Spirit Medium must have effort['spirit_medium']; got {list(char.core.effort)}"
    )
    pool = char.core.effort["spirit_medium"]

    wis_score = char.stats.get("WIS", 10)
    effort_base = pack.rules.wwn.magic.effort_base
    starting_skill_level = 1
    expected_max = max(1, effort_base + starting_skill_level + swn_attribute_modifier(wis_score))
    assert pool.max == expected_max, (
        f"Spirit Medium effort max {pool.max} != expected {expected_max} "
        f"(WIS={wis_score}, modifier={swn_attribute_modifier(wis_score)})"
    )

    sc = char.core.spellcasting
    assert sc is not None, "Spirit Medium must be seeded with a SpellcastingState"

    medium_def = next(c for c in pack.classes if c.display_name == "Spirit Medium")
    casts_per_day = medium_def.wwn_magic.casts_per_day_by_level["1"]
    assert sc.casts_per_day == casts_per_day
    assert sc.casts_remaining == casts_per_day

    capacity = medium_def.wwn_magic.prepared_by_level.get(
        "1", len(medium_def.wwn_magic.starting_prepared)
    )
    expected_prepared = medium_def.wwn_magic.starting_prepared[:capacity]
    assert sc.prepared == expected_prepared, (
        f"Spirit Medium prepared must be {expected_prepared!r}, got {sc.prepared!r}"
    )
    # Specifically: ["still_the_breath", "ember_veil"]
    assert sc.prepared == ["still_the_breath", "ember_veil"], (
        f"Spirit Medium starting_prepared mismatch: got {sc.prepared!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_martial_artist_seeds_effort_only_no_spellcasting():
    """Martial Artist (Vowed) gets effort['vowed'] but spellcasting=None."""
    from sidequest.game.ruleset.swn import swn_attribute_modifier

    pack = _load_elemental_harmony()
    char = _build_archetype(pack, "Iron Fist", class_hint="Martial Artist")
    assert char.char_class == "Martial Artist", f"expected Martial Artist, got {char.char_class!r}"

    assert "vowed" in char.core.effort, (
        f"Martial Artist must have effort['vowed']; got {list(char.core.effort)}"
    )
    pool = char.core.effort["vowed"]

    wis_score = char.stats.get("WIS", 10)
    effort_base = pack.rules.wwn.magic.effort_base
    starting_skill_level = 1
    expected_max = max(1, effort_base + starting_skill_level + swn_attribute_modifier(wis_score))
    assert pool.max == expected_max, (
        f"Martial Artist vowed effort max {pool.max} != expected {expected_max}"
    )

    # Vowed: no cast tables → spellcasting MUST be None
    assert char.core.spellcasting is None, (
        f"Martial Artist (Vowed, Effort-only) must have spellcasting=None; "
        f"got {char.core.spellcasting!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_guardian_warrior_no_effort_no_spellcasting():
    """Guardian (Warrior) has no wwn_magic → effort={}, spellcasting=None."""
    pack = _load_elemental_harmony()
    char = _build_archetype(pack, "Stone Shield", class_hint="Guardian")
    assert char.char_class == "Guardian", f"expected Guardian, got {char.char_class!r}"

    assert char.core.effort == {}, (
        f"Guardian (Warrior, no magic) must have empty effort; got {char.core.effort!r}"
    )
    assert char.core.spellcasting is None, (
        f"Guardian must have spellcasting=None; got {char.core.spellcasting!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_scholar_expert_no_effort_no_spellcasting():
    """Scholar (Expert) has no wwn_magic → effort={}, spellcasting=None."""
    pack = _load_elemental_harmony()
    char = _build_archetype(pack, "Lotus Scholar", class_hint="Scholar")
    assert char.char_class == "Scholar", f"expected Scholar, got {char.char_class!r}"

    assert char.core.effort == {}, (
        f"Scholar (Expert, no magic) must have empty effort; got {char.core.effort!r}"
    )
    assert char.core.spellcasting is None, (
        f"Scholar must have spellcasting=None; got {char.core.spellcasting!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_wanderer_expert_no_effort_no_spellcasting():
    """Wanderer (Expert) has no wwn_magic → effort={}, spellcasting=None."""
    pack = _load_elemental_harmony()
    char = _build_archetype(pack, "Open Road", class_hint="Wanderer")
    assert char.char_class == "Wanderer", f"expected Wanderer, got {char.char_class!r}"

    assert char.core.effort == {}, (
        f"Wanderer (Expert, no magic) must have empty effort; got {char.core.effort!r}"
    )
    assert char.core.spellcasting is None, (
        f"Wanderer must have spellcasting=None; got {char.core.spellcasting!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_channeler_snapshot_round_trip():
    """Effort pools + spellcasting + prepared survive a GameSnapshot serialize/deserialize round-trip.

    Uses the production snapshot path: model_dump_json() / model_validate_json() —
    the same encoding the persistence layer writes to the database (ADR-115).
    """
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    pack = _load_elemental_harmony()
    char = _build_archetype(pack, "Round Trip", class_hint="Channeler")
    assert char.core.spellcasting is not None, "Channeler must have spellcasting for round-trip"
    assert char.core.effort, "Channeler must have effort for round-trip"

    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=1),
    )
    snap.characters.append(char)

    # Serialize using the production snapshot dump path
    snap_json = snap.model_dump_json()
    snap_reloaded = GameSnapshot.model_validate_json(snap_json)

    assert len(snap_reloaded.characters) == 1
    char_rt = snap_reloaded.characters[0]

    # Effort pools survive
    assert "channeler" in char_rt.core.effort, (
        f"effort['channeler'] must survive round-trip; got keys {list(char_rt.core.effort)}"
    )
    pool_original = char.core.effort["channeler"]
    pool_rt = char_rt.core.effort["channeler"]
    assert pool_rt.max == pool_original.max, (
        f"effort max must survive round-trip: {pool_original.max} → {pool_rt.max}"
    )
    assert pool_rt.source == pool_original.source, (
        f"effort source must survive round-trip: {pool_original.source!r} → {pool_rt.source!r}"
    )

    # Spellcasting state survives
    sc_original = char.core.spellcasting
    sc_rt = char_rt.core.spellcasting
    assert sc_rt is not None, "SpellcastingState must survive round-trip (not become None)"
    assert sc_rt.prepared == sc_original.prepared, (
        f"prepared must survive round-trip: {sc_original.prepared!r} → {sc_rt.prepared!r}"
    )
    assert sc_rt.casts_remaining == sc_original.casts_remaining, (
        f"casts_remaining must survive round-trip: {sc_original.casts_remaining} → {sc_rt.casts_remaining}"
    )
    assert sc_rt.casts_per_day == sc_original.casts_per_day, (
        f"casts_per_day must survive round-trip: {sc_original.casts_per_day} → {sc_rt.casts_per_day}"
    )
    assert sc_rt.max_spell_level == sc_original.max_spell_level, (
        f"max_spell_level must survive round-trip: {sc_original.max_spell_level} → {sc_rt.max_spell_level}"
    )
