"""Guard against the neon_dystopia/pulp_noir chargen-block class of bug.

A pack that declares the axis-driven archetype system (``base_archetypes``
*and* ``archetype_constraints`` both present) makes ``pack_has_axes`` True in
the chargen archetype gate (``_gate_archetype_resolution``). For such a pack
the gate REQUIRES the builder to produce a resolvable ``jungian/rpg_role``
pair — otherwise it blocks the commit with ``missing_axes_with_pack_axes`` and
the world is unplayable (playtest finding #C6, 2026-05-28).

The builder only forms ``resolved_archetype`` when a chargen scene set BOTH
``jungian_hint`` and ``rpg_role_hint``. So every axis-pack must have at least
one canned-choice path that sets both, and the resulting pair must resolve
(not be forbidden). neon_dystopia and pulp_noir shipped with axes declared but
their crucible choices set only ``class_hint`` — every character creation
hard-blocked at the gate.

This parametrizes over the live packs that declare axes and asserts, for each,
that there is a choice-combination whose hints resolve through the real
``resolve_archetype`` shim with provenance stamped (the exact gate-pass
signal). It catches the bug for ANY current or future axis-pack, not just the
two found in the playtest.
"""

from __future__ import annotations

import pytest

from sidequest.genre.archetype.shim import resolve_archetype
from sidequest.genre.error import GenreValidationError
from sidequest.genre.loader import GenreLoader

# Live packs that declare the axis-driven archetype system. Kept explicit
# (rather than globbed) so adding a pack is a deliberate act that surfaces
# this requirement.
AXIS_PACKS = [
    "caverns_and_claudes",
    "elemental_harmony",
    "heavy_metal",
    "neon_dystopia",
    "pulp_noir",
    "space_opera",
]


@pytest.mark.parametrize("pack_name", AXIS_PACKS)
def test_axis_pack_chargen_produces_resolvable_archetype_pair(pack_name: str) -> None:
    """Every axis-pack must let chargen form a resolvable jungian/rpg_role pair.

    Without this, the archetype gate blocks the commit and the world is
    unplayable (finding #C6).
    """
    pack = GenreLoader().load(pack_name)

    # Precondition: this pack really is an axis-pack (gate's pack_has_axes).
    assert pack.base_archetypes is not None and pack.archetype_constraints is not None, (
        f"{pack_name} is in AXIS_PACKS but does not declare base_archetypes + "
        "archetype_constraints — fix the list or the pack."
    )

    # Collect every (jungian, rpg_role) hint pair the canned choices can set.
    # The builder pairs the LAST jungian_hint with the LAST rpg_role_hint
    # across scenes, but any choice that carries BOTH is a self-contained
    # pair the player can pick — that's the minimum bar for a playable path.
    paired_choices: list[tuple[str, str, str]] = []
    for scene in pack.char_creation:
        for choice in scene.choices:
            me = choice.mechanical_effects
            if me is None:
                continue
            if me.jungian_hint is not None and me.rpg_role_hint is not None:
                paired_choices.append((scene.id, me.jungian_hint, me.rpg_role_hint))

    assert paired_choices, (
        f"{pack_name} declares axes but no chargen choice sets BOTH jungian_hint "
        "and rpg_role_hint — the builder can never form resolved_archetype, so "
        "the archetype gate blocks every character (finding #C6)."
    )

    # Every such pair must resolve (genre fallback at minimum) with provenance
    # stamped — that is exactly what apply_archetype_resolved keys the gate on.
    for scene_id, jungian, rpg_role in paired_choices:
        try:
            resolution = resolve_archetype(
                jungian=jungian,
                rpg_role=rpg_role,
                base=pack.base_archetypes,
                constraints=pack.archetype_constraints,
                funnels=None,
                genre=pack_name,
                world=None,
            )
        except GenreValidationError as exc:  # pragma: no cover - failure path
            pytest.fail(
                f"{pack_name} scene '{scene_id}' offers a forbidden/unknown pair "
                f"{jungian}/{rpg_role}: {exc}"
            )
        assert resolution.provenance is not None
        assert resolution.resolved.name, (
            f"{pack_name} {jungian}/{rpg_role} resolved to an empty name"
        )


@pytest.mark.parametrize("pack_name", ["neon_dystopia", "pulp_noir"])
def test_promoted_pack_crucible_choices_all_carry_both_hints(pack_name: str) -> None:
    """Regression for #C6: the crucible (class-selection) scene of the two
    2026-05-23-promoted packs must carry both archetype hints on EVERY choice,
    so any class the player picks reaches a resolvable archetype.
    """
    pack = GenreLoader().load(pack_name)
    crucible = next(s for s in pack.char_creation if s.id == "crucible")
    for choice in crucible.choices:
        me = choice.mechanical_effects
        assert me is not None and me.jungian_hint is not None, (
            f"{pack_name} crucible choice '{choice.label}' missing jungian_hint"
        )
        assert me.rpg_role_hint is not None, (
            f"{pack_name} crucible choice '{choice.label}' missing rpg_role_hint"
        )
