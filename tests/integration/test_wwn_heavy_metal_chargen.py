"""heavy_metal → WWN Story 2 — chargen seeding proof (real magic).

RED for story 87-2. Builds each heavy_metal class through the REAL
``CharacterBuilder.build()`` and asserts WWN magic state seeds correctly on
``core``:

  * warrior (Warrior) and expert (Expert) → effort == {}, spellcasting is None;
  * necromancer / elementalist / pact_born (Mage traditions) → an Effort pool
    AND a POPULATED SpellcastingState (prepared == starting_prepared[:capacity],
    casts_remaining == casts_per_day). This is the real-magic contract that
    supersedes the original Effort-only design (2026-06-05 AMENDMENT).

class_hint lives on heavy_metal's ``crucible`` scene (not ``origins``), so the
build helper scans every choice-bearing scene for the matching class_hint.

Effort-max formula (mirrored from seed_wwn_magic in builder.py):
  pool_max = effort_base + starting_skill_level + swn_attribute_modifier(governing_score)
Computed from the BUILT character's actual stat so a stat-generation change is caught.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# (display_name, is_caster) — the build target + expected seeding shape.
# 89-5 adds the two Barsoom caster Callings (Mentalist + Super-scientist);
# they ride the same WWN seeding contract as the doom casters. The build
# helper's class_hint injection covers them even though the genre-tier
# crucible scene doesn't offer them (barsoom's world-tier surface does —
# see tests/integration/test_barsoom_chargen.py).
_NON_CASTERS = [("Warrior", False), ("Expert", False)]
_CASTER_SKIP = pytest.mark.skip(
    reason="content-coupled: Mentalist/Super-scientist Callings migrated to barsoom "
    "world-tier content (epic 94), so the genre-tier build falls back to Warrior; "
    "rewrite against fixtures — story 94-4"
)
_CASTERS = [
    ("Necromancer", True),
    ("Elementalist", True),
    ("Pact-born", True),
    pytest.param("Mentalist", True, marks=_CASTER_SKIP),
    pytest.param("Super-scientist", True, marks=_CASTER_SKIP),
]


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _build_class(pack, name: str, *, class_display: str):
    """Build a character whose class is ``class_display`` via the REAL builder.

    Walks the real heavy_metal char_creation scenes; at each choice-bearing scene
    selects the first choice whose mechanical_effects.class_hint matches the target
    display name (heavy_metal carries class_hint on the ``crucible`` scene). Other
    scenes advance generically (choice 0 / auto-advance / freeform / followup). If
    no scene offered the target class_hint, inject it as a late SceneResult so
    build() accumulates it last-one-wins (mirrors the EH helper).
    """
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
    assert pack.classes, "heavy_metal must declare classes (Story 2)"
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
@pytest.mark.parametrize("class_display,_is_caster", _NON_CASTERS)
def test_non_caster_seeds_no_magic(class_display: str, _is_caster: bool) -> None:
    """Warrior + Expert: no wwn_magic → effort == {}, spellcasting is None."""
    pack = _load_heavy_metal()
    char = _build_class(pack, f"{class_display} One", class_display=class_display)
    assert char.char_class == class_display, f"expected {class_display}, got {char.char_class!r}"

    assert char.core.effort == {}, (
        f"{class_display} (no magic) must have empty effort; got {char.core.effort!r}"
    )
    assert char.core.spellcasting is None, (
        f"{class_display} must have spellcasting=None; got {char.core.spellcasting!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("class_display,_is_caster", _CASTERS)
def test_caster_seeds_effort_and_populated_spellcasting(
    class_display: str, _is_caster: bool
) -> None:
    """Each Mage tradition seeds an Effort pool AND a populated SpellcastingState.

    This is the real-magic contract: casters are NOT Effort-only. spellcasting
    must be non-None with prepared spells, casts_remaining == casts_per_day.
    """
    from sidequest.game.ruleset.swn import swn_attribute_modifier

    pack = _load_heavy_metal()
    assert pack.rules.ruleset == "wwn"
    char = _build_class(pack, f"{class_display} Caster", class_display=class_display)
    assert char.char_class == class_display, f"expected {class_display}, got {char.char_class!r}"

    cls = next(c for c in pack.classes if c.display_name == class_display)
    wm = cls.wwn_magic
    assert wm is not None, f"{class_display} must carry wwn_magic"

    # 1. Effort pool keyed by the class's effort source, with the formula's max.
    src = wm.effort_sources[0].source
    assert src in char.core.effort, (
        f"{class_display} must seed effort[{src!r}]; got keys {list(char.core.effort)}"
    )
    pool = char.core.effort[src]
    # governing attr → the pack stat it maps to (attribute_map: canonical → abbrev).
    governing_key = wm.effort_sources[0].governing_attr
    stat_abbrev = pack.rules.wwn.attribute_map[governing_key]
    gov_score = char.stats.get(stat_abbrev, 10)
    effort_base = pack.rules.wwn.magic.effort_base
    starting_skill_level = wm.effort_sources[0].starting_skill_level
    expected_max = max(1, effort_base + starting_skill_level + swn_attribute_modifier(gov_score))
    if wm.partial:
        expected_max = max(1, expected_max - 1)
    assert pool.max == expected_max, (
        f"{class_display} effort max mismatch: expected {expected_max} "
        f"(base={effort_base}+skill={starting_skill_level}+mod for {stat_abbrev}={gov_score}); got {pool.max}"
    )

    # 2. Populated SpellcastingState (NOT None — the real-magic change).
    sc = char.core.spellcasting
    assert sc is not None, (
        f"{class_display} must seed a SpellcastingState (real magic) — None means the "
        f"caster has no spells, which is the superseded Effort-only behavior"
    )
    casts_per_day = wm.casts_per_day_by_level["1"]
    assert sc.casts_per_day == casts_per_day, (
        f"{class_display} casts_per_day must be {casts_per_day}; got {sc.casts_per_day}"
    )
    assert sc.casts_remaining == casts_per_day, (
        f"{class_display} casts_remaining at chargen must equal casts_per_day={casts_per_day}; "
        f"got {sc.casts_remaining}"
    )
    capacity = wm.prepared_by_level.get("1", len(wm.starting_prepared))
    expected_prepared = wm.starting_prepared[:capacity]
    assert sc.prepared == expected_prepared, (
        f"{class_display} prepared must be {expected_prepared!r}; got {sc.prepared!r}"
    )
    assert sc.prepared, f"{class_display} must have at least one prepared spell"
