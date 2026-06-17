"""barsoom world-tier chargen surface — TDD RED for story 89-5.

Pins the content contract for Barsoom's character-creation surface and the
two crunch calls Keith resolved on 2026-06-05:

  * the barsoom world declares a WORLD-TIER ``char_creation`` scene list
    (``resolve_char_creation_scenes(pack, "barsoom")`` returns the world
    list, which REPLACES the genre crucible — the documented wired path
    for a world-tier origin surface, see char_creation_resolve.py);
  * D1: selectable origins = native Barsoomian peoples + the transported
    Earthman;
  * D5 (gravity boon, Keith: "STR edge + leap ability"): the Earthman
    choice carries ``race_hint: Earthman`` AND ``stat_bonuses: {STR: 2}``
    — ``stat_bonuses`` is the pre-wired engine consumer
    (``CharacterBuilder.generate_stats`` applies accumulated bonuses
    additively under every stat-generation strategy), so this is real
    crunch, not narrator-only flavor. The leap ABILITY half of the boon is
    asserted at build time in tests/integration/test_barsoom_chargen.py;
  * four-arm physiology (Keith: "fiction-only"): NO non-Earthman origin
    choice carries mechanical crunch — green-Martian four arms stay
    narrator-framed flavor;
  * the chargen class surface offers the heroic-pulp Callings (Warrior,
    Expert, Mentalist, Super-scientist) and does NOT offer the doom
    Callings (Necromancer / Elementalist / Pact-born) — faithful Barsoom
    has no necromancers (epic: "heroic Frazetta pulp, NOT grimdark doom").

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# Keith's mechanics call (2026-06-05, story 89-5): the gravity boon's stat
# edge — key must be heavy_metal's STR abbreviation (attribute_map STRENGTH→STR).
_EARTHMAN_STAT_BONUS = {"STR": 2}

_BARSOOM_CASTER_CALLINGS = {"Mentalist", "Super-scientist"}
_DOOM_CALLINGS = {"Necromancer", "Elementalist", "Pact-born"}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _barsoom_scenes(pack):
    """The chargen scenes a barsoom connection actually gets (production path)."""
    from sidequest.server.dispatch.char_creation_resolve import (
        resolve_char_creation_scenes,
    )

    return resolve_char_creation_scenes(pack, world_slug="barsoom")


def _all_choices(scenes):
    for scene in scenes:
        for choice in scene.choices or []:
            yield scene, choice


def _race_choices(scenes):
    """(scene, choice) pairs whose effects carry a race_hint — the origin surface."""
    return [
        (s, c)
        for s, c in _all_choices(scenes)
        if c.mechanical_effects and c.mechanical_effects.race_hint
    ]


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_barsoom_declares_world_tier_chargen_scenes() -> None:
    """barsoom must author its own world-tier char_creation (replaces genre)."""
    pack = _load_heavy_metal()
    world = pack.worlds.get("barsoom")
    assert world is not None, "heavy_metal must ship the barsoom world"

    assert world.char_creation, (
        "barsoom must declare a WORLD-TIER char_creation scene list — this is the "
        "wired origin surface for the Earthman boon (89-5). An empty list falls "
        "back to the genre crucible, which has no Barsoom origins."
    )

    scenes = _barsoom_scenes(pack)
    genre_ids = {s.id for s in pack.char_creation}
    world_ids = {s.id for s in scenes}
    assert world_ids == {s.id for s in world.char_creation}, (
        "resolve_char_creation_scenes('barsoom') must return the world-tier list "
        f"(replacement, not merge); got {sorted(world_ids)} vs genre {sorted(genre_ids)}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_barsoom_offers_earthman_origin_with_gravity_stat_edge() -> None:
    """D5: exactly one Earthman origin choice, carrying race_hint + the STR edge."""
    pack = _load_heavy_metal()
    scenes = _barsoom_scenes(pack)

    earthman = [
        (s, c) for s, c in _race_choices(scenes) if c.mechanical_effects.race_hint == "Earthman"
    ]
    assert len(earthman) == 1, (
        f"barsoom chargen must offer exactly ONE Earthman origin choice (D1/D5); "
        f"found {len(earthman)}"
    )

    _scene, choice = earthman[0]
    eff = choice.mechanical_effects
    assert eff.stat_bonuses == _EARTHMAN_STAT_BONUS, (
        "the Earthman choice must carry the gravity boon's stat edge as "
        f"stat_bonuses == {_EARTHMAN_STAT_BONUS} (the pre-wired generate_stats "
        f"consumer — Keith's 'STR edge' call); got {eff.stat_bonuses!r}"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_barsoom_offers_native_origins() -> None:
    """D1: native Barsoomian peoples are selectable alongside the Earthman."""
    pack = _load_heavy_metal()
    scenes = _barsoom_scenes(pack)

    native = [
        (s, c) for s, c in _race_choices(scenes) if c.mechanical_effects.race_hint != "Earthman"
    ]
    assert len(native) >= 2, (
        "barsoom chargen must offer at least two native origins (red Martian + "
        f"green Martian at minimum, D1); got {len(native)} non-Earthman race choices"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_native_origins_carry_no_crunch_four_arms_fiction_only() -> None:
    """Keith's call: green-Martian four arms are fiction-only — no native origin
    choice may carry mechanical crunch (stat_bonuses / mutation_hint). The
    Earthman is the SOLE documented world-tier mechanical origin (tier
    exception, D5/§9)."""
    pack = _load_heavy_metal()
    scenes = _barsoom_scenes(pack)

    native = [
        (s, c) for s, c in _race_choices(scenes) if c.mechanical_effects.race_hint != "Earthman"
    ]
    assert native, "precondition: native origins must exist (see companion test)"

    for scene, choice in native:
        eff = choice.mechanical_effects
        assert not eff.stat_bonuses, (
            f"native origin {choice.label!r} (scene {scene.id!r}) must NOT carry "
            f"stat_bonuses — four arms / native physiology is fiction-only per "
            f"Keith's 89-5 crunch call; got {eff.stat_bonuses!r}"
        )
        assert eff.mutation_hint is None, (
            f"native origin {choice.label!r} must NOT carry a mutation_hint "
            f"(would seed a Race-source mechanical ability); fiction-only"
        )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_barsoom_class_surface_offers_pulp_callings_not_doom() -> None:
    """barsoom chargen offers Warrior/Expert/Mentalist/Super-scientist and
    none of the doom Callings (heroic Frazetta pulp, not grimdark doom)."""
    pack = _load_heavy_metal()
    scenes = _barsoom_scenes(pack)

    offered = {
        c.mechanical_effects.class_hint
        for _s, c in _all_choices(scenes)
        if c.mechanical_effects and c.mechanical_effects.class_hint
    }

    expected = {"Warrior", "Expert"} | _BARSOOM_CASTER_CALLINGS
    missing = expected - offered
    assert not missing, (
        f"barsoom chargen must offer the pulp Callings {sorted(expected)}; "
        f"missing {sorted(missing)} (offered: {sorted(offered)})"
    )

    leaked = offered & _DOOM_CALLINGS
    assert not leaked, f"barsoom chargen must NOT offer the doom Callings; leaked {sorted(leaked)}"
