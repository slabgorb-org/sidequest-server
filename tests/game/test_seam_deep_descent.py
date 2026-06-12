"""deep_descent resolver — bind-to-entrance or fail loud (Story 105-2)."""

import pytest

from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.game.seams.base import SeamCrossingError
from sidequest.game.seams.deep_descent import resolve_deep_descent
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import Route

SEAM_ROUTE = Route(
    name="Down the Rope",
    description="The one-way descent.",
    from_id="the_dropmouth",
    to_id="deep_descent",
)


@pytest.fixture
def snapshot_on_surface() -> GameSnapshot:
    """A solo save with PC ``Groucho`` seated on the surface at ``the_dropmouth``."""
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": "the_dropmouth"},
        player_seats={"p1": "Groucho"},
    )


class _StoreWithEntrance:
    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar"))
        return g


class _EmptyStore:
    def load_map(self, *, entrance_id):
        return RegionGraph(entrance_id=entrance_id)  # no nodes — corrupt seed


def test_resolves_to_entrance_and_binds_pc(snapshot_on_surface):  # fixture: PC at the_dropmouth
    result = resolve_deep_descent(
        snapshot=snapshot_on_surface,
        player_name="Groucho",
        route=SEAM_ROUTE,
        resolved_via="surface_descent",
        dungeon_store=_StoreWithEntrance(),
    )
    assert result.to_region == "entrance"
    assert snapshot_on_surface.region_for(perspective="Groucho") == "entrance"


def test_no_store_fails_loud(snapshot_on_surface):
    with pytest.raises(SeamCrossingError) as exc:
        resolve_deep_descent(
            snapshot=snapshot_on_surface,
            player_name="Groucho",
            route=SEAM_ROUTE,
            resolved_via="surface_descent",
            dungeon_store=None,
        )
    assert exc.value.reason == "no_dungeon_store"
    assert snapshot_on_surface.region_for(perspective="Groucho") == "the_dropmouth"


def test_corrupt_seed_fails_loud(snapshot_on_surface):
    with pytest.raises(SeamCrossingError) as exc:
        resolve_deep_descent(
            snapshot=snapshot_on_surface,
            player_name="Groucho",
            route=SEAM_ROUTE,
            resolved_via="surface_descent",
            dungeon_store=_EmptyStore(),
        )
    assert exc.value.reason == "no_dungeon_entrance"
    assert snapshot_on_surface.region_for(perspective="Groucho") == "the_dropmouth"
