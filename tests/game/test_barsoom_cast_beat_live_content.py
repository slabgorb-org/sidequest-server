"""Barsoom caster Callings × the LIVE heavy_metal cast gate — TDD RED for 89-5.

The generic beat-filter machinery is proven in test_wwn_beat_filter.py with
synthetic fixtures. This suite re-proves it against the REAL loaded pack
(wiring test, CLAUDE.md "Every Test Suite Needs a Wiring Test"): once 89-5
appends the Barsoom Callings to the cast_spell class_filter staged by the
89-4 BARSOOM HOOK, a Mentalist / Super-scientist with live spellcasting
state must see cast_spell in the live combat confrontation — and a
heavy_metal Warrior must not.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# (caster display_name, a 89-4 suggested starting seed it should cast)
_BARSOOM_CASTERS = [
    ("Mentalist", "phantom_bowmen"),
    ("Super-scientist", "disintegration_ray"),
]


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _combat_confrontation(pack):
    combat = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert combat is not None, "heavy_metal must expose a combat confrontation"
    return combat


def _class_by_display(pack, display_name: str):
    cls = next((c for c in pack.classes if c.display_name == display_name), None)
    assert cls is not None, (
        f"heavy_metal classes.yaml must declare {display_name!r} (89-5 Calling); "
        f"got {sorted(c.display_name for c in pack.classes)}"
    )
    return cls


def _spellcasting(prepared: list[str]):
    from sidequest.game.wwn_magic import SpellcastingState

    return SpellcastingState(
        prepared=prepared,
        casts_remaining=1,
        casts_per_day=1,
        max_spell_level=1,
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
@pytest.mark.parametrize("display_name,seed_spell", _BARSOOM_CASTERS)
def test_barsoom_caster_sees_cast_spell_in_live_combat(
    display_name: str, seed_spell: str
) -> None:
    """A live-content Barsoom caster with casts + prepared sees cast_spell."""
    from sidequest.game.beat_filter import beats_available_for

    pack = _load_heavy_metal()
    combat = _combat_confrontation(pack)
    caster = _class_by_display(pack, display_name)

    # The 89-4 suggested seed must be a real catalog spell (guards the pairing).
    assert pack.wwn_spell_catalog is not None
    assert seed_spell in {s.id for s in pack.wwn_spell_catalog.spells}, (
        f"{seed_spell!r} must exist in spells_wwn.yaml (authored in 89-4)"
    )

    out = beats_available_for(
        combat,
        caster,
        spell_slots_remaining=0.0,  # B/X economy must be ignored on the WWN arm
        spellcasting=_spellcasting([seed_spell]),
    )
    beat_ids = [b.id for b in out]
    assert "cast_spell" in beat_ids, (
        f"{display_name} with casts + prepared must see cast_spell in the live "
        f"combat confrontation (class_filter append, 89-4 hook); got {beat_ids}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_live_warrior_still_excluded_from_cast_spell() -> None:
    """The filter append must not loosen the gate: a heavy_metal Warrior with
    a (synthetic) full spellcasting state still cannot select cast_spell."""
    from sidequest.game.beat_filter import beats_available_for

    pack = _load_heavy_metal()
    combat = _combat_confrontation(pack)
    warrior = _class_by_display(pack, "Warrior")

    out = beats_available_for(
        combat,
        warrior,
        spell_slots_remaining=99.0,
        spellcasting=_spellcasting(["phantom_bowmen"]),
    )
    beat_ids = [b.id for b in out]
    assert "cast_spell" not in beat_ids, (
        f"Warrior must never see cast_spell; got {beat_ids}"
    )
