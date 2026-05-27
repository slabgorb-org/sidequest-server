"""Unit tests for InteractionTable model invariants.

Guards the new invariant established in the dogfight SWN-resolution refactor:
InteractionTable carries geometry (cells, maneuvers, starting_state) only.
The deterministic damage fields (damage_increments / starting_hull) were
validated-but-never-consumed; they are removed so the model no longer admits
pack authors accidentally anchoring to the old damage model.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import InteractionCell, InteractionTable


def _cell(red: str, blue: str) -> InteractionCell:
    return InteractionCell(
        pair=[red, blue],
        name=f"{red}_vs_{blue}",
        red_view={"gun_solution": False},
        blue_view={"gun_solution": True},
    )


def test_interaction_table_rejects_damage_increments_field():
    """damage_increments is no longer a valid field; extra="forbid" must reject it."""
    with pytest.raises(ValidationError):
        InteractionTable(
            version="1",
            starting_state="merge",
            maneuvers_consumed=["straight", "loop"],
            cells=[_cell("straight", "loop")],
            damage_increments={"graze": 5, "clean": 15, "devastating": 30},
        )


def test_interaction_table_rejects_starting_hull_field():
    """starting_hull is no longer a valid field; extra="forbid" must reject it."""
    with pytest.raises(ValidationError):
        InteractionTable(
            version="1",
            starting_state="merge",
            maneuvers_consumed=["straight", "loop"],
            cells=[_cell("straight", "loop")],
            starting_hull=20,
        )


def test_interaction_table_loads_geometry_only():
    """A geometry-only table (no legacy damage fields) loads cleanly."""
    table = InteractionTable(
        version="1",
        starting_state="merge",
        maneuvers_consumed=["straight", "loop"],
        cells=[_cell("straight", "loop")],
    )
    assert table.cells[0].blue_view["gun_solution"] is True
