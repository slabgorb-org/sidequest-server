"""Story 82-4 — narrow ADR-121 to the two-tier shim; remove the dead four-tier walk.

DECISION (recorded with code evidence, 2026-06-05):

The four-tier ``Resolver.resolve_merged`` walk (Global → Genre → World →
Culture) that ADR-121 calls "live" is **dead code** and is being removed,
not wired:

- ``Resolver`` is never instantiated in production (the only grep hit is
  the unrelated ``AsideResolver``); ``resolve``/``resolve_merged`` have no
  callers anywhere, including tests.
- No genre pack ships the file layout the walk expects: there is no
  ``archetype.yaml`` at any tier, and culture dirs hold flat
  ``{culture}.yaml`` corpus files, not ``cultures/{culture}/{axis}.yaml``.
- The production archetype path (chargen_mixin → archetype/shim.py) merges
  *different schemas per tier* (archetypes_base → archetype_constraints →
  archetype_funnels) via pair-constrained lookup — not the same-type
  document merge ``LayeredMerge`` expresses. The walk cannot serve it.

What survives (genuinely live): ``LayeredMerge``, ``MergeStrategy``,
``_apply_strategy``, the provenance wire types, and ``ArchetypeResolved``.
The shim already emits tier-annotated ``Provenance`` that rides the wire
(``archetype_provenance``) and the ``character_creation.archetype_resolved``
span event — the GM/dev-observable surface for this subsystem.

RED contract for Dev:
- Remove ``Resolver``, ``ResolutionContext``, ``Resolved``, and
  ``_load_tier`` from ``sidequest/genre/resolver.py`` and the
  ``sidequest.genre`` package exports ("no unmarked dead code").
- Do NOT remove the survivors pinned below.
- Rewrite ADR-121 (orchestrator repo) to describe the two-tier shim as the
  production reality — verified by Reviewer, not by this suite.
"""

from __future__ import annotations

import json

import yaml

# ---------------------------------------------------------------------------
# Phase A — removal contract (RED: these fail while the dead walk exists)
# ---------------------------------------------------------------------------


def test_resolver_class_removed_from_module() -> None:
    """The four-tier ``Resolver`` class is deleted, not just unexported."""
    import sidequest.genre.resolver as resolver_module

    assert not hasattr(resolver_module, "Resolver"), (
        "Resolver (the four-tier Global→Genre→World→Culture walk) has no "
        "production consumer and no matching content layout — Story 82-4 "
        "narrows ADR-121 to the two-tier shim and deletes this class."
    )


def test_resolution_context_removed_from_module() -> None:
    """``ResolutionContext`` exists only to feed the dead walk — deleted with it."""
    import sidequest.genre.resolver as resolver_module

    assert not hasattr(resolver_module, "ResolutionContext"), (
        "ResolutionContext is consumed only by the removed Resolver walk."
    )


def test_resolved_wrapper_removed_from_module() -> None:
    """``Resolved[T]`` is returned only by the dead walk — deleted with it.

    The production resolution result type is ``ArchetypeResolution``
    (archetype/shim.py), which carries provenance directly.
    """
    import sidequest.genre.resolver as resolver_module

    assert not hasattr(resolver_module, "Resolved"), (
        "Resolved[T] is constructed only by the removed Resolver walk."
    )


def test_tier_file_loader_removed_from_module() -> None:
    """``_load_tier`` is called only by the dead walk — deleted with it."""
    import sidequest.genre.resolver as resolver_module

    assert not hasattr(resolver_module, "_load_tier"), (
        "_load_tier has no caller once Resolver.resolve/resolve_merged are gone."
    )


def test_genre_package_no_longer_exports_dead_walk() -> None:
    """The package facade stops advertising the dead symbols.

    ``__all__`` membership and attribute access must both go — an
    ``__all__`` entry pointing at a deleted symbol would break
    ``from sidequest.genre import *`` loudly, and a surviving attribute
    would be unmarked dead code.
    """
    import sidequest.genre as genre_pkg

    for name in ("Resolver", "ResolutionContext", "Resolved"):
        assert name not in genre_pkg.__all__, (
            f"sidequest.genre.__all__ still exports dead symbol {name!r}"
        )
        assert not hasattr(genre_pkg, name), (
            f"sidequest.genre still exposes dead symbol {name!r}"
        )


# ---------------------------------------------------------------------------
# Phase B — survivor guards (must pass BEFORE and AFTER the removal;
# they scope the weed-whack so Dev cannot over-remove)
# ---------------------------------------------------------------------------


def test_layered_merge_machinery_survives_narrowing() -> None:
    """``LayeredMerge`` + ``MergeStrategy`` stay: they have live consumers.

    ``ArchetypeResolved`` subclasses ``LayeredMerge`` and the merge
    strategies are field metadata on live models. Behavior is asserted
    (not just importability) so a gutted reimplementation also fails.
    """
    from pydantic import Field

    from sidequest.genre import LayeredMerge, MergeStrategy

    assert "LayeredMerge" in __import__("sidequest.genre", fromlist=["__all__"]).__all__
    assert "MergeStrategy" in __import__("sidequest.genre", fromlist=["__all__"]).__all__

    class _Probe(LayeredMerge):
        name: str = Field(default="", json_schema_extra={"merge": "replace"})
        tags: list[str] = Field(default_factory=list, json_schema_extra={"merge": "append"})

    merged = _Probe(name="base", tags=["a"]).merge(_Probe(name="deeper", tags=["b"]))
    assert merged.name == "deeper"
    assert merged.tags == ["a", "b"]
    assert MergeStrategy.CULTURE_FINAL.value == "culture_final"


def test_archetype_resolved_remains_layered_merge_subclass() -> None:
    """The live wire model keeps its LayeredMerge base after the removal."""
    from sidequest.genre.archetype.resolved import ArchetypeResolved
    from sidequest.genre.resolver import LayeredMerge

    assert issubclass(ArchetypeResolved, LayeredMerge)


def test_provenance_wire_types_survive_and_roundtrip() -> None:
    """Provenance types live in protocol (not resolver.py) and ride the wire.

    These are consumed by the production shim and by
    ``archetype_provenance`` on the wire — they must be untouched by the
    resolver narrowing.
    """
    from sidequest.protocol.provenance import (
        ContributionKind,
        MergeStep,
        Provenance,
        Tier,
    )

    prov = Provenance(
        source_tier=Tier.world,
        source_file="g/worlds/w/archetype_funnels.yaml",
        source_span=None,
        merge_trail=[
            MergeStep(
                tier=Tier.world,
                file="g/worlds/w/archetype_funnels.yaml",
                span=None,
                contribution=ContributionKind.initial,
            )
        ],
    )
    raw = json.loads(prov.model_dump_json())
    assert raw["source_tier"] == "world"
    assert raw["merge_trail"][0]["contribution"] == "initial"
    rehydrated = Provenance.model_validate(raw)
    assert rehydrated == prov


# ---------------------------------------------------------------------------
# Wiring pin — the chosen mechanism (two-tier shim) IS the production path
# ---------------------------------------------------------------------------


def test_chargen_mixin_calls_the_shim_resolver() -> None:
    """Runtime-identity wiring tripwire: the symbol chargen calls is the shim.

    The production caller (``_resolve_character_archetype`` in
    chargen_mixin.py) resolves archetypes via
    ``sidequest.genre.archetype.shim.resolve_archetype``. This asserts
    runtime object identity (not source text), so it fails if the import
    is rewired to a different mechanism — e.g. someone resurrecting a
    four-tier walk without revisiting the 82-4 decision.
    """
    from sidequest.genre.archetype import shim
    from sidequest.server.websocket_handlers import chargen_mixin

    assert chargen_mixin.resolve_archetype is shim.resolve_archetype


def test_shim_resolution_carries_tier_annotated_provenance() -> None:
    """End-to-end through the chosen mechanism: provenance is GM-observable.

    Drives the real ``resolve_archetype`` with a synthetic pack and pins
    the tier-annotated provenance the GM panel surfaces (and that
    ``apply_archetype_resolved`` copies onto the character as
    ``archetype_provenance``). World funnel hit → world tier; fallback →
    genre tier. This is the observable contract the narrowed ADR-121
    describes.
    """
    from sidequest.genre.archetype.shim import ResolutionSource, resolve_archetype
    from sidequest.genre.models.archetype_axes import BaseArchetypes
    from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
    from sidequest.genre.models.archetype_funnels import ArchetypeFunnels
    from sidequest.protocol.provenance import Tier

    base = BaseArchetypes.model_validate(
        yaml.safe_load("""
jungian:
  - id: sage
    drive: "Seeks truth"
    ocean_tendencies:
      openness: [7.0, 9.5]
      conscientiousness: [6.0, 8.0]
      extraversion: [2.0, 5.0]
      agreeableness: [4.0, 7.0]
      neuroticism: [3.0, 6.0]
    stat_affinity: [wisdom, intellect]
rpg_roles:
  - id: healer
    combat_function: "Restores allies"
    stat_affinity: [wisdom]
npc_roles: []
""")
    )
    constraints = ArchetypeConstraints.model_validate(
        yaml.safe_load("""
valid_pairings:
  common:
    - [sage, healer]
  uncommon: []
  rare: []
  forbidden: []
genre_flavor:
  jungian: {}
  rpg_roles:
    healer:
      fallback_name: "Hedge Healer"
npc_roles_available: []
""")
    )
    funnels = ArchetypeFunnels.model_validate(
        yaml.safe_load("""
funnels:
  - name: Thornwall Mender
    absorbs:
      - [sage, healer]
    faction: Thornwall Convocation
    lore: "Itinerant healers"
    cultural_status: respected
additional_constraints:
  forbidden: []
""")
    )

    # World-funnel hit → world-tier provenance with a single initial step.
    hit = resolve_archetype(
        jungian="sage",
        rpg_role="healer",
        base=base,
        constraints=constraints,
        funnels=funnels,
        genre="testgenre",
        world="testworld",
    )
    assert hit.source == ResolutionSource.world_funnel
    assert hit.provenance.source_tier == Tier.world
    assert len(hit.provenance.merge_trail) == 1
    assert "testworld" in hit.provenance.source_file

    # No funnels → genre-fallback provenance.
    fallback = resolve_archetype(
        jungian="sage",
        rpg_role="healer",
        base=base,
        constraints=constraints,
        funnels=None,
        genre="testgenre",
        world=None,
    )
    assert fallback.source == ResolutionSource.genre_fallback
    assert fallback.resolved.name == "Hedge Healer"
    assert fallback.provenance.source_tier == Tier.genre
    assert fallback.provenance.merge_trail[0].file == "testgenre/archetype_constraints.yaml"
