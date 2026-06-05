"""barsoom chargen × archetype-resolution guard — review finding R3 (89-5).

The original 89-5 red suite proved the boon/caster wiring through
``CharacterBuilder.build()`` — which never runs archetype resolution (that
lives in ``chargen_mixin``). Two live-play-breaking content defects escaped
through that gap (review R1/R2): the barsoom scenes set no
``rpg_role_hint`` (→ 45-6 gate ``missing_axes_with_pack_axes``, every
chargen confirm rejected) and used an invalid ``jungian_hint: warrior``
(→ ``resolver_raised``).

This suite closes the gap by driving the REAL resolver
(``resolve_archetype``) with every hint pair the barsoom scenes can
accumulate:

  1. every class-selecting choice carries BOTH ``jungian_hint`` and
     ``rpg_role_hint`` (the builder forms ``resolved_archetype`` only when
     both accumulated — builder.py);
  2. every (origin choice × calling choice) accumulation, computed with the
     same last-one-wins semantics as ``CharacterBuilder.accumulated()``,
     yields a pair that ``resolve_archetype`` resolves WITHOUT raising —
     against the live ``archetypes_base.yaml`` + heavy_metal constraints
     (no string-matching: the real resolver is the oracle).

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_heavy_metal():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("heavy_metal"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _barsoom_scenes(pack):
    from sidequest.server.dispatch.char_creation_resolve import (
        resolve_char_creation_scenes,
    )

    return resolve_char_creation_scenes(pack, world_slug="barsoom")


def _choices_with(scenes, attr: str):
    out = []
    for scene in scenes:
        for choice in scene.choices or []:
            eff = choice.mechanical_effects
            if eff is not None and getattr(eff, attr) is not None:
                out.append((scene, choice))
    return out


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_every_barsoom_class_choice_carries_both_archetype_hints() -> None:
    """R1 guard: the builder pairs jungian/rpg_role only when BOTH hints
    accumulate; heavy_metal declares archetype axes, so a class choice
    missing either hint ships a chargen the 45-6 gate rejects."""
    pack = _load_heavy_metal()
    assert pack.base_archetypes is not None and pack.archetype_constraints is not None, (
        "precondition: heavy_metal declares archetype axes (the gate is live)"
    )

    class_choices = _choices_with(_barsoom_scenes(pack), "class_hint")
    assert class_choices, "barsoom must offer class choices"
    for scene, choice in class_choices:
        eff = choice.mechanical_effects
        assert eff.jungian_hint is not None, (
            f"class choice {choice.label!r} (scene {scene.id!r}) missing jungian_hint — "
            f"the archetype gate will reject the resulting chargen"
        )
        assert eff.rpg_role_hint is not None, (
            f"class choice {choice.label!r} (scene {scene.id!r}) missing rpg_role_hint — "
            f"builder forms no (j/r) pair and the 45-6 gate blocks with "
            f"missing_axes_with_pack_axes (review R1)"
        )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_every_barsoom_hint_pair_resolves_through_the_real_resolver() -> None:
    """R2/R3 guard: every (origin × calling) accumulation — last-one-wins,
    mirroring AccumulatedChoices — must resolve via resolve_archetype
    without GenreValidationError (unknown axis id OR forbidden pairing)."""
    from sidequest.genre.archetype.shim import resolve_archetype

    pack = _load_heavy_metal()
    scenes = _barsoom_scenes(pack)
    base = pack.base_archetypes
    constraints = pack.archetype_constraints
    assert base is not None and constraints is not None

    origin_choices = _choices_with(scenes, "race_hint")
    class_choices = _choices_with(scenes, "class_hint")
    assert origin_choices and class_choices

    barsoom_world = pack.worlds.get("barsoom")
    assert barsoom_world is not None
    funnels = barsoom_world.archetype_funnels

    seen_pairs: set[tuple[str, str]] = set()
    for _os, origin in origin_choices:
        for _cs, calling in class_choices:
            # Last-one-wins accumulation in scene order (origins precedes
            # the calling scene in the barsoom list).
            jungian = (
                calling.mechanical_effects.jungian_hint or origin.mechanical_effects.jungian_hint
            )
            rpg_role = (
                calling.mechanical_effects.rpg_role_hint or origin.mechanical_effects.rpg_role_hint
            )
            assert jungian is not None and rpg_role is not None, (
                f"({origin.label!r} × {calling.label!r}) accumulates an incomplete "
                f"pair ({jungian!r}, {rpg_role!r})"
            )
            seen_pairs.add((jungian, rpg_role))

    assert seen_pairs, "no (jungian, rpg_role) pairs accumulated"
    for jungian, rpg_role in sorted(seen_pairs):
        # The real resolver is the oracle: raises GenreValidationError on an
        # unknown axis id or a forbidden pairing. Any raise fails the test.
        resolution = resolve_archetype(
            jungian=jungian,
            rpg_role=rpg_role,
            base=base,
            constraints=constraints,
            funnels=funnels,
            genre="heavy_metal",
            world="barsoom",
        )
        assert resolution.resolved.name, (
            f"pair ({jungian}, {rpg_role}) resolved to an empty archetype name"
        )
