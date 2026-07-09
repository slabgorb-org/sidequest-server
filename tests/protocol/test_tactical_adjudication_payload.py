"""RED (Story 165-4, plan Task 9): additive protocol echoes for tactical math.

Track C surfaces the tactical adjudications 165-3 already computes (move budget,
reach/range verdicts) onto the wire so the client can *show the math* without
recomputing it — the Sebastien/Jade "see the numbers in the player UI" surface.

Everything here is ADDITIVE with empty defaults: ``TacticalGridPayload`` keeps its
existing shape and gains ``adjudications``; ``DiceResultPayload`` gains optional
``range_band``/``distance_cells``. Track B's SITE_MAP cutover must keep emitting the
same ``TacticalGridPayload`` untouched, so the default-empty back-compat assertions
below are load-bearing, not decoration.
"""

from __future__ import annotations

import pytest


def _valid_dice_result(**overrides):
    """A minimal, schema-valid DiceResultPayload; ``overrides`` patch fields."""
    from sidequest.protocol.dice import (
        DiceResultPayload,
        DieGroupResult,
        DieSides,
        DieSpec,
        RollOutcome,
        ThrowParams,
    )

    kwargs = dict(
        request_id="req-1",
        rolling_player_id="p1",
        character_name="Rux",
        rolls=[DieGroupResult(spec=DieSpec(sides=DieSides.D20, count=1), faces=[14])],
        modifier=2,
        total=16,
        difficulty=12,
        outcome=RollOutcome.Success,
        seed=42,
        throw_params=ThrowParams(
            velocity=(0.0, 0.0, 0.0), angular=(0.0, 0.0, 0.0), position=(0.5, 0.5)
        ),
    )
    kwargs.update(overrides)
    return DiceResultPayload(**kwargs)


# ── TacticalAdjudication model ───────────────────────────────────────────────


def test_adjudication_carries_the_reach_denial_math():
    """A denied reach echo carries distance vs max reach + the human reason."""
    from sidequest.protocol.models import TacticalAdjudication

    adj = TacticalAdjudication(
        actor="Rux",
        kind="reach",
        valid=False,
        distance_cells=3,
        max_cells=1,
        mode="melee",
        reason="target is 3 cells away; your reach is 1",
        cells=[(2, 1), (3, 1)],
    )
    assert adj.actor == "Rux"
    assert adj.kind == "reach"
    assert adj.valid is False
    assert adj.distance_cells == 3
    assert adj.max_cells == 1
    assert adj.mode == "melee"
    assert "3 cells away" in adj.reason


def test_adjudication_optional_math_fields_default_none():
    """Only actor/kind/valid are required; the numeric echo fields are optional."""
    from sidequest.protocol.models import TacticalAdjudication

    adj = TacticalAdjudication(actor="Rux", kind="move", valid=True)
    assert adj.cells_spent is None
    assert adj.cells_budget is None
    assert adj.distance_cells is None
    assert adj.max_cells is None
    assert adj.mode is None
    assert adj.reason == ""
    assert adj.cells == []


def test_adjudication_cells_default_is_not_shared_between_instances():
    """Lang-review rule #2 (mutable default): the empty ``cells`` default must be
    per-instance. A ``cells: list = []`` class default would leak mutations across
    every adjudication; ``Field(default_factory=list)`` is the correct idiom."""
    from sidequest.protocol.models import TacticalAdjudication

    a = TacticalAdjudication(actor="Rux", kind="move", valid=True)
    b = TacticalAdjudication(actor="Vex", kind="move", valid=True)
    a.cells.append((9, 9))
    assert b.cells == [], "cells default leaked across instances (shared mutable default)"


def test_grid_payload_adjudications_default_is_not_shared_between_instances():
    """Lang-review rule #2: the ``adjudications`` default must be per-instance too."""
    from sidequest.protocol.models import TacticalAdjudication, TacticalGridPayload

    p1 = TacticalGridPayload(room_id="r1", room_name="A", room_type="cavern")
    p2 = TacticalGridPayload(room_id="r2", room_name="B", room_type="cavern")
    p1.adjudications.append(TacticalAdjudication(actor="Rux", kind="move", valid=True))
    assert p2.adjudications == [], "adjudications default leaked across payload instances"


def test_adjudication_serializes_cells_as_json_arrays():
    """cells are tuples in Python but MUST hit the wire as [x, y] arrays.

    Mirrors TacticalFeature._ser_cell — the UI parser reads position[0]/[1].
    """
    from sidequest.protocol.models import TacticalAdjudication

    adj = TacticalAdjudication(actor="Rux", kind="aoe", valid=True, cells=[(2, 1), (3, 1)])
    dumped = adj.model_dump()
    assert dumped["cells"] == [[2, 1], [3, 1]]
    assert all(isinstance(c, list) for c in dumped["cells"])


# ── TacticalGridPayload.adjudications (additive) ─────────────────────────────


def test_grid_payload_adjudications_default_empty_for_back_compat():
    """A payload built the pre-165-4 way has an empty adjudications list.

    This is the Track B SITE_MAP back-compat guard: the field is additive, so
    every existing emit site keeps producing a valid payload.
    """
    from sidequest.protocol.models import TacticalGridPayload

    p = TacticalGridPayload(room_id="r1", room_name="Cavern", room_type="cavern")
    assert p.adjudications == []


def test_grid_payload_carries_move_summary_adjudication():
    from sidequest.protocol.models import TacticalAdjudication, TacticalGridPayload

    p = TacticalGridPayload(
        room_id="r1",
        room_name="Cavern",
        room_type="cavern",
        adjudications=[
            TacticalAdjudication(
                actor="Rux", kind="move", valid=True, cells_spent=2, cells_budget=6
            )
        ],
    )
    assert len(p.adjudications) == 1
    assert p.adjudications[0].cells_spent == 2
    assert p.adjudications[0].cells_budget == 6


# ── DiceResultPayload additive range echo ────────────────────────────────────


def test_dice_result_range_echo_defaults_none():
    """range_band/distance_cells are additive — absent on today's rolls."""
    result = _valid_dice_result()
    assert result.range_band is None
    assert result.distance_cells is None


def test_dice_result_carries_range_band_and_distance():
    """A ranged strike echoes the weapon band + measured distance for the card."""
    result = _valid_dice_result(range_band="rifle", distance_cells=4)
    assert result.range_band == "rifle"
    assert result.distance_cells == 4
    dumped = result.model_dump()
    assert dumped["range_band"] == "rifle"
    assert dumped["distance_cells"] == 4


# ── Export wiring ────────────────────────────────────────────────────────────


def test_tactical_adjudication_is_exported_from_protocol_package():
    """TacticalAdjudication must be reachable from ``sidequest.protocol`` so the
    emit path and any consumer import it the same way as TacticalGridPayload."""
    import sidequest.protocol as protocol

    assert hasattr(protocol, "TacticalAdjudication"), (
        "TacticalAdjudication must be exported from sidequest.protocol.__init__"
    )
    assert "TacticalAdjudication" in protocol.__all__


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
