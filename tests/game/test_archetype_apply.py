"""Tests for ``sidequest.game.archetype_apply.apply_archetype_resolved``.

Covers:
- Sets ``resolved_archetype`` to the archetype's display name.
- Sets ``archetype_provenance`` to a JSON-serializable provenance dict.
- Overwrites both fields in lockstep on re-application.
- Preserves all other Character fields.
"""

from __future__ import annotations

from sidequest.game.archetype_apply import apply_archetype_resolved
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.genre.archetype.resolved import ArchetypeResolved
from sidequest.genre.archetype.shim import ArchetypeResolution, ResolutionSource
from sidequest.genre.models.archetype_constraints import PairingWeight
from sidequest.protocol.provenance import (
    ContributionKind,
    MergeStep,
    Provenance,
    Tier,
)


def _make_character(char_class: str = "Delver", race: str = "Human") -> Character:
    core = CreatureCore(
        name="Rux",
        description="A seasoned delver",
        personality="Curious",
        level=1,
        xp=0,
        hp=HpPool(current=20, max=20, base_max=20),
    )
    return Character(
        core=core, backstory="An orphan of the Reach.", char_class=char_class, race=race
    )


def _make_resolution(
    name: str = "Wandering Oracle",
    tier: Tier = Tier.world,
    source: ResolutionSource = ResolutionSource.world_funnel,
    faction: str | None = "The Observers",
) -> ArchetypeResolution:
    resolved = ArchetypeResolved(
        name=name,
        jungian="sage",
        rpg_role="healer",
        npc_role=None,
        speech_pattern="",
        lore="Watches the edges of the map.",
        faction=faction,
        cultural_status=None,
    )
    provenance = Provenance(
        source_tier=tier,
        source_file="caverns_and_claudes/worlds/flickering_reach/archetype_funnels.yaml",
        source_span=None,
        merge_trail=[
            MergeStep(
                tier=tier,
                file="caverns_and_claudes/worlds/flickering_reach/archetype_funnels.yaml",
                span=None,
                contribution=ContributionKind.initial,
            )
        ],
    )
    return ArchetypeResolution(
        resolved=resolved,
        source=source,
        weight=PairingWeight.common,
        provenance=provenance,
    )


def test_apply_sets_display_name() -> None:
    char = _make_character()
    assert char.resolved_archetype is None
    apply_archetype_resolved(char, _make_resolution(name="Wandering Oracle"))
    assert char.resolved_archetype == "Wandering Oracle"


def test_apply_sets_provenance_as_json_dict() -> None:
    char = _make_character()
    apply_archetype_resolved(char, _make_resolution(tier=Tier.world))
    prov = char.archetype_provenance
    assert isinstance(prov, dict)
    assert prov["source_tier"] == "world"
    assert prov["source_file"].endswith("archetype_funnels.yaml")
    assert len(prov["merge_trail"]) == 1
    assert prov["merge_trail"][0]["tier"] == "world"


def test_apply_overwrites_in_lockstep() -> None:
    char = _make_character()
    apply_archetype_resolved(char, _make_resolution(name="First", tier=Tier.genre))
    assert char.resolved_archetype == "First"
    assert char.archetype_provenance is not None
    assert char.archetype_provenance["source_tier"] == "genre"

    apply_archetype_resolved(
        char,
        _make_resolution(
            name="Second",
            tier=Tier.world,
            source=ResolutionSource.world_funnel,
        ),
    )
    # Both fields updated together — no stale provenance on the new name.
    assert char.resolved_archetype == "Second"
    assert char.archetype_provenance is not None
    assert char.archetype_provenance["source_tier"] == "world"


def test_apply_preserves_other_fields() -> None:
    char = _make_character(char_class="Delver", race="Gnome")
    char.backstory = "Sealed in amber for a thousand years."
    char.hooks = ["debt to the Reach", "forgotten home"]

    apply_archetype_resolved(char, _make_resolution(name="Resonant Witness"))

    assert char.char_class == "Delver"
    assert char.race == "Gnome"
    assert char.backstory == "Sealed in amber for a thousand years."
    assert char.hooks == ["debt to the Reach", "forgotten home"]
    assert char.core.name == "Rux"
    assert char.core.hp.current == 20
