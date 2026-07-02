"""ADR-153 §3 state graph — model surface (story 158-40, AC-1).

RED: ``InteractionCell`` has no ``next_state`` field (``extra="forbid"``
rejects it) and ``ConfrontationDef`` has no ``interaction_tables`` registry.

``test_cell_still_rejects_damage_field`` is a GREEN non-regression pin, not
part of the RED set: the ADR-153 §2 firewall says cells carry geometry +
``gun_solution`` (+ ``next_state``) only — a ``damage:`` field on the cell
model must STAY rejected after Dev adds the new fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import (
    ConfrontationDef,
    InteractionCell,
    InteractionTable,
)


def _cell(pair: list[str], **kwargs: object) -> InteractionCell:
    return InteractionCell(pair=pair, red_view={}, blue_view={}, **kwargs)


def test_cell_accepts_next_state() -> None:
    """AC-1: a cell may name the relative-position state the duel transitions
    into next turn."""
    cell = _cell(["straight", "loop"], next_state="tail_chase")
    assert cell.next_state == "tail_chase"


def test_cell_next_state_defaults_none() -> None:
    """AC-1: ``next_state`` is optional — None means the duel stays in the
    current state (the overwhelmingly common cell)."""
    cell = _cell(["straight", "straight"])
    assert cell.next_state is None


def test_confrontation_def_holds_table_registry() -> None:
    """AC-1: the def carries a per-state table registry keyed by each table's
    ``starting_state``, alongside the legacy single ``interaction_table``."""
    merge = InteractionTable(
        version="1",
        starting_state="merge",
        maneuvers_consumed=["straight", "loop"],
        cells=[_cell(["straight", "loop"])],
    )
    d = ConfrontationDef(
        type="dogfight",
        label="Fighter Duel",
        category="combat",
        resolution_mode="sealed_letter_lookup",
        win_condition="hp_depletion",
        interaction_tables={"merge": merge},
    )
    assert set(d.interaction_tables) == {"merge"}
    assert isinstance(d.interaction_tables["merge"], InteractionTable)
    assert d.interaction_tables["merge"].starting_state == "merge"
    assert d.interaction_tables["merge"].maneuvers_consumed == ["straight", "loop"]


def test_confrontation_def_registry_defaults_empty() -> None:
    """AC-1 back-compat: a def that never declares the registry gets an empty
    dict — every existing non-dogfight ConfrontationDef construction must
    keep validating unchanged."""
    d = ConfrontationDef(
        type="melee",
        label="Close Quarters",
        category="combat",
        resolution_mode="beat_selection",
        win_condition="hp_depletion",
        # hp_depletion combat requires the reserved combat-seed keys
        # (ConfrontationDef validators: hp + armor_class for the HP pool,
        # dexterity for SWN 1d8+DEX initiative) — mirror the live melee def.
        opponent_default_stats={"hp": 7, "armor_class": 12, "dexterity": 12},
    )
    assert d.interaction_tables == {}


def test_cell_still_rejects_damage_field() -> None:
    """GREEN guard (ADR-153 §2 firewall): the cell model must keep rejecting a
    ``damage`` field — hull/hit/kill belong to the bound ruleset, never to
    positioning cells. This is the non-regression pin that Dev's new fields
    don't accidentally relax ``extra="forbid"``."""
    with pytest.raises(ValidationError, match="damage"):
        InteractionCell(
            pair=["straight", "loop"],
            red_view={},
            blue_view={},
            damage={"dice": "1d6"},
        )
