"""Cross-file validators run after world load."""

from __future__ import annotations

import pytest

from sidequest.genre.loader import (
    GenreLoadError,
    _validate_authored_npc_uniqueness,
    _validate_crew_npc_references,
    _validate_opening_bank_coverage,
    _validate_opening_region_bindings,
    _validate_opening_setting_references,
    _validate_present_npcs_resolve,
)
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.genre.models.narrative import (
    Opening,
    OpeningSetting,
    OpeningTrigger,
)
from sidequest.genre.models.rigs_world import (
    ChassisInstanceConfig,
    OceanScores,
)
from sidequest.genre.models.world import CartographyConfig, Region


def _make_chassis(crew_npcs: list[str] | None = None) -> ChassisInstanceConfig:
    return ChassisInstanceConfig(
        id="kestrel",
        name="Kestrel",
        **{"class": "voidborn_freighter"},
        OCEAN=OceanScores(),
        interior_rooms=["galley", "cockpit", "engineering"],
        crew_npcs=crew_npcs or [],
    )


def _make_opening(
    setting: OpeningSetting,
    mode: str = "solo",
    backgrounds: list[str] | None = None,
) -> Opening:
    return Opening(
        id="test_op",
        triggers=OpeningTrigger(mode=mode, backgrounds=backgrounds or []),
        setting=setting,
        establishing_narration="Galley scene; coffee is what passes for coffee.",
        first_turn_invitation="Outside the porthole, void and stars.",
    )


def test_chassis_anchored_resolves() -> None:
    op = _make_opening(OpeningSetting(chassis_instance="kestrel", interior_room="galley"))
    chassis = [_make_chassis()]
    # Should not raise.
    _validate_opening_setting_references([op], chassis, world_slug="testworld")


def test_chassis_instance_unknown_fails() -> None:
    """Validator 2."""
    op = _make_opening(OpeningSetting(chassis_instance="missing_ship", interior_room="galley"))
    chassis = [_make_chassis()]
    with pytest.raises(GenreLoadError, match="chassis_instance"):
        _validate_opening_setting_references([op], chassis, world_slug="testworld")


def test_interior_room_not_in_chassis_fails() -> None:
    """Validator 3."""
    op = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="bridge")  # not in interior_rooms
    )
    chassis = [_make_chassis()]
    with pytest.raises(GenreLoadError, match="interior_room"):
        _validate_opening_setting_references([op], chassis, world_slug="testworld")


def test_location_anchored_skips_chassis_check() -> None:
    """Location-anchored openings don't need chassis_instance to resolve."""
    op = _make_opening(OpeningSetting(location_label="the Promenade"))
    chassis: list[ChassisInstanceConfig] = []
    # Should not raise — no chassis required for location-anchored.
    _validate_opening_setting_references([op], chassis, world_slug="testworld")


def _make_authored_npc(id: str) -> AuthoredNpc:
    return AuthoredNpc(id=id, name=f"Name-{id}")


def test_crew_npcs_all_resolve() -> None:
    chassis = [_make_chassis(crew_npcs=["captain_x", "engineer_y"])]
    npcs = [_make_authored_npc("captain_x"), _make_authored_npc("engineer_y")]
    _validate_crew_npc_references(chassis, npcs, world_slug="testworld")


def test_crew_npc_unknown_fails() -> None:
    """Validator 4."""
    chassis = [_make_chassis(crew_npcs=["captain_x", "missing_npc"])]
    npcs = [_make_authored_npc("captain_x")]
    with pytest.raises(GenreLoadError, match="missing_npc"):
        _validate_crew_npc_references(chassis, npcs, world_slug="testworld")


def test_empty_crew_npcs_ok() -> None:
    """A chassis with no crew_npcs declared is valid."""
    chassis = [_make_chassis(crew_npcs=[])]
    npcs: list[AuthoredNpc] = []
    _validate_crew_npc_references(chassis, npcs, world_slug="testworld")


def test_authored_npc_ids_unique() -> None:
    npcs = [_make_authored_npc("a"), _make_authored_npc("b")]
    _validate_authored_npc_uniqueness(npcs, world_slug="testworld")


def test_authored_npc_duplicate_id_fails() -> None:
    """Validator 5."""
    npcs = [_make_authored_npc("a"), _make_authored_npc("a")]
    with pytest.raises(GenreLoadError, match="duplicate"):
        _validate_authored_npc_uniqueness(npcs, world_slug="testworld")


def test_present_npcs_resolve() -> None:
    op = _make_opening(
        OpeningSetting(
            location_label="the Promenade",
            present_npcs=["arena_master"],
        )
    )
    npcs = [_make_authored_npc("arena_master")]
    _validate_present_npcs_resolve([op], npcs, world_slug="testworld")


def test_present_npcs_unknown_fails() -> None:
    """Validator 12 part-b."""
    op = _make_opening(
        OpeningSetting(
            location_label="the Promenade",
            present_npcs=["missing_envoy"],
        )
    )
    npcs: list[AuthoredNpc] = []
    with pytest.raises(GenreLoadError, match="present_npcs"):
        _validate_present_npcs_resolve([op], npcs, world_slug="testworld")


def test_bank_coverage_solo_and_mp_present() -> None:
    """Validator 7: ≥1 solo, ≥1 MP."""
    solo = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="solo",
    )
    mp = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="multiplayer",
    )
    chargen_backgrounds: list[str] = []  # validator 8 with empty list = no constraint
    _validate_opening_bank_coverage([solo, mp], chargen_backgrounds, world_slug="testworld")


def test_bank_coverage_missing_mp_fails() -> None:
    solo = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="solo",
    )
    with pytest.raises(GenreLoadError, match="multiplayer"):
        _validate_opening_bank_coverage([solo], [], world_slug="testworld")


def test_bank_coverage_missing_solo_fails() -> None:
    mp = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="multiplayer",
    )
    with pytest.raises(GenreLoadError, match="solo"):
        _validate_opening_bank_coverage([mp], [], world_slug="testworld")


def test_either_mode_satisfies_both() -> None:
    """An opening with mode=either counts toward both solo and MP coverage."""
    op = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="either",
    )
    _validate_opening_bank_coverage([op], [], world_slug="testworld")


def test_chargen_background_uncovered_fails() -> None:
    """Validator 8: every chargen background must be reachable by some opening."""
    solo_a = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="solo",
        backgrounds=["Far Landing Raised Me"],
    )
    mp = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="multiplayer",
    )
    with pytest.raises(GenreLoadError, match="Wirework Made Me"):
        _validate_opening_bank_coverage(
            [solo_a, mp],
            chargen_backgrounds=["Far Landing Raised Me", "Wirework Made Me"],
            world_slug="testworld",
        )


def test_fallback_opening_covers_all() -> None:
    """An opening with backgrounds=[] is a fallback, satisfies validator 8 for everything."""
    solo_fallback = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="solo",
        backgrounds=[],  # fallback
    )
    mp = _make_opening(
        OpeningSetting(chassis_instance="kestrel", interior_room="galley"),
        mode="multiplayer",
    )
    _validate_opening_bank_coverage(
        [solo_fallback, mp],
        chargen_backgrounds=["Far Landing Raised Me", "Wirework Made Me"],
        world_slug="testworld",
    )


# ---- opening setting.region_id binds to a cartography node ------------
#
# Playtest 2026-05-25 [BUG] flickering_reach: current_region is a load-bearing
# cartography node id, but openings declared only free-text location_labels and
# the world's starting_region matched none of them, so the party spawned
# pointing at a region the opening never visited. Openings now declare an
# explicit setting.region_id; this validator enforces at load time that every
# declared region_id resolves to a real cartography node (no fuzzy matching).


def _cartography(region_ids: list[str]) -> CartographyConfig:
    return CartographyConfig(
        starting_region=region_ids[0] if region_ids else "",
        regions={
            rid: Region(name=rid, summary="s", description="d") for rid in region_ids
        },
    )


def test_opening_region_id_resolves_to_node() -> None:
    """region_id naming a declared cartography region passes."""
    op = _make_opening(
        OpeningSetting(location_label="the western wall at dusk", region_id="blind_reach")
    )
    _validate_opening_region_bindings(
        [op], _cartography(["toods_dome", "blind_reach"]), world_slug="testworld"
    )


def test_opening_region_id_dangling_fails() -> None:
    """region_id that is not a cartography node fails the world load loudly —
    no silent fallback, no fuzzy free-text match."""
    op = _make_opening(
        OpeningSetting(location_label="somewhere", region_id="not_a_region")
    )
    with pytest.raises(GenreLoadError, match="not a declared cartography region"):
        _validate_opening_region_bindings(
            [op], _cartography(["toods_dome", "blind_reach"]), world_slug="testworld"
        )


def test_opening_without_region_id_is_skipped() -> None:
    """An opening that declares no region_id is unconstrained by this validator
    (region binding is opt-in for openings that anchor away from spawn)."""
    op = _make_opening(OpeningSetting(location_label="the Promenade"))
    _validate_opening_region_bindings(
        [op], _cartography(["toods_dome", "blind_reach"]), world_slug="testworld"
    )
