"""sq-playtest 2026-06-02 (wry_whimsy/oz): `current_region` never advances.

Root cause: in a cartography region-mode world the narrator advances the scene
via ``result.location`` (a region heading) reliably, but ``current_region`` is a
SEPARATE field that otherwise only moves when the narrator emits an explicit
``apply_world_patch(current_region=...)``. The 2026-05-21 fix gave the narrator a
cartography RegionProjection ("YOU ARE HERE" + MOVEMENT RULE) so it *would* emit
that patch — but the narrator does not reliably comply (oz playtest: stuck at
``munchkin_country`` three regions deep, which also starves the
``_region_changed``-gated LOCATION_DESCRIPTION re-emit → the Location panel froze
on "Gathering your bearings…").

The robust complement (engine-deterministic, no LLM compliance needed): when the
narrator's heading RESOLVES to a known cartography region via
``_resolve_heading_to_cartography``, advance ``current_region`` (and the acting
PC's ``pc_regions``) to it. This fires only on a real region match — a sub-area
heading like "The Emerald City — The Throne Room" correctly resolves to
``the_emerald_city`` — and is gated to region-mode worlds so room-graph
(dungeon) worlds keep their frontier-hook current_region management.

Fixtures, not live packs (project memory: no content-coupled unit tests).
"""

from __future__ import annotations

from types import SimpleNamespace

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for


def _oz_regions() -> dict[str, Region]:
    return {
        "munchkin_country": Region(
            name="The Munchkin Country",
            summary="The blue East.",
            description="The blue East.",
            adjacent=["the_yellow_brick_road", "the_emerald_city"],
        ),
        "the_yellow_brick_road": Region(
            name="The Yellow Brick Road",
            summary="The gold road.",
            description="The gold road.",
            adjacent=["munchkin_country", "the_emerald_city"],
        ),
        "the_emerald_city": Region(
            name="The Emerald City",
            summary="The green hub.",
            description="The green hub.",
            adjacent=["the_yellow_brick_road"],
        ),
    }


def _region_mode_pack(pack, *, mode: NavigationMode = NavigationMode.region):
    """Attach a synthetic cartography world keyed "oz" onto the (mock) pack."""
    world_obj = SimpleNamespace(
        cartography=CartographyConfig(navigation_mode=mode, regions=_oz_regions())
    )
    pack.worlds = {"oz": world_obj}
    return pack


def test_region_mode_location_change_advances_current_region(
    snapshot_with_pack,
    character_named_sam,
):
    """The narrator's heading resolves to a known cartography region different
    from current_region → current_region (and the acting PC's pc_regions)
    advance to it. Pre-fix current_region stayed frozen at the init region."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.current_region = "munchkin_country"
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.characters.append(character_named_sam)

    # Heading whose leading "Place — Epithet" segment resolves to the_emerald_city.
    result = NarrationTurnResult(
        narration="Susan follows the gold road to the green city's gates.",
        location="The Emerald City — The Green Street",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )

    assert snap.current_region == "the_emerald_city", (
        "current_region must advance to the cartography region the narrator's "
        f"heading resolves to; got {snap.current_region!r}"
    )
    assert snap.pc_regions.get("Susan") == "the_emerald_city", (
        "the acting PC's pc_regions entry must track the region advance; "
        f"got {snap.pc_regions.get('Susan')!r}"
    )


def test_unchanged_region_heading_is_a_noop(
    snapshot_with_pack,
    character_named_sam,
):
    """A heading resolving to the SAME region the PC is already in must not
    churn current_region (idempotent; no spurious region-change re-emits)."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.current_region = "the_emerald_city"
    snap.characters.append(character_named_sam)

    result = NarrationTurnResult(
        narration="Susan crosses the plaza inside the city.",
        location="The Emerald City — The Palace Steps",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )

    assert snap.current_region == "the_emerald_city"


def test_region_mode_unresolved_sub_location_not_added_to_discovered_regions(
    snapshot_with_pack,
    character_named_sam,
):
    """Location-tab bug (DRIVER 2026-06-04, the_circuit). In a region-mode world
    the cartography region set is AUTHORED/closed. A narrator scene title that
    does NOT resolve to a known region ("Dunkelkurve — Inside the Tunnel" is a
    POI *within* sturmichi, not a region) must NOT be forked into
    discovered_regions — otherwise chapter titles pollute the Map node-graph
    (discovered_regions = ['sturmichi', 'Kanjō Loop — …', 'Dunkelkurve — …']).
    The Story 45-17 surface-form forking is for room-graph worlds only.
    """
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack)
    snap.current_region = "munchkin_country"
    snap.discovered_regions = ["munchkin_country"]
    snap.character_locations["Susan"] = "The Munchkin Country"
    snap.characters.append(character_named_sam)

    # A sub-location heading that resolves to NO cartography region.
    result = NarrationTurnResult(
        narration="Susan ducks into a hollow beneath the blue hills.",
        location="A Hollow Beneath the Hills",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )

    assert "A Hollow Beneath the Hills" not in snap.discovered_regions, (
        "a region-mode world must NOT fork an unresolved scene title into "
        "discovered_regions — it's a POI within the region, not a new region; "
        f"got discovered_regions={snap.discovered_regions!r}"
    )
    assert snap.discovered_regions == ["munchkin_country"], (
        "discovered_regions must hold only authored cartography region ids in a "
        f"region-mode world; got {snap.discovered_regions!r}"
    )


def test_room_graph_world_still_forks_unresolved_heading_into_discovered_regions(
    snapshot_with_pack,
    character_named_sam,
):
    """Guard (preserve Story 45-17): a room-graph / non-region-mode world still
    forks a narrator-invented sub-area heading into discovered_regions — those
    worlds legitimately grow their graph from narrator inventions. The region-
    mode skip must NOT regress this path.
    """
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack, mode=NavigationMode.room_graph)
    snap.current_region = "munchkin_country"
    snap.discovered_regions = ["munchkin_country"]
    snap.characters.append(character_named_sam)

    result = NarrationTurnResult(
        narration="The party pries open a sealed maintenance hatch.",
        location="The Sealed Maintenance Hatch",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )

    assert "The Sealed Maintenance Hatch" in snap.discovered_regions, (
        "a room-graph world must still fork an unresolved heading into "
        "discovered_regions (Story 45-17 surface-form forking); "
        f"got {snap.discovered_regions!r}"
    )


def test_room_graph_world_does_not_advance_current_region_from_heading(
    snapshot_with_pack,
    character_named_sam,
):
    """Guard: a room-graph (non-region) world must NOT have current_region
    inferred from result.location here — those worlds manage current_region via
    the room graph / frontier hook. The region-mode gate protects them."""
    snap, pack = snapshot_with_pack
    _region_mode_pack(pack, mode=NavigationMode.room_graph)
    snap.current_region = "munchkin_country"
    snap.characters.append(character_named_sam)

    result = NarrationTurnResult(
        narration="The party descends toward the green city.",
        location="The Emerald City — The Green Street",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world="oz",
        player_name="Susan",
        room=room_for(snapshot=snap),
    )

    assert snap.current_region == "munchkin_country", (
        "a room-graph world must not advance current_region from the narrator "
        f"heading via this region-mode path; got {snap.current_region!r}"
    )
