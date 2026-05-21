"""Unit tests for class-moves label/description resolution (views._resolve_class_moves).

Playtest 2026-05-21 (tea_and_murder/glenross): the Character → Abilities
"Class moves" pills rendered raw snake_case beat IDs (`cross_examine`,
`present_argument`, …) with no human-readable label or description. The beat
labels/descriptions already exist in each pack's confrontation BeatDefs; the
view layer just never resolved id → label before sending to the UI.

These are fixture-based unit tests (synthetic BeatDefs) — they do NOT load any
live genre pack. End-to-end resolution against real pack content is covered by
tests/integration/test_class_signature_wiring.py.
"""

from __future__ import annotations

import logging

from sidequest.game.beat_kinds import BeatKind
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.models import ClassMove
from sidequest.server.views import _resolve_class_moves


def _beat(beat_id: str, label: str, **kw: object) -> BeatDef:
    return BeatDef(id=beat_id, label=label, kind=BeatKind.strike, stat_check="Cunning", **kw)


def test_resolves_id_to_label_and_description():
    index = {
        "cross_examine": _beat(
            "cross_examine",
            "Cross-Examine",
            narrator_hint="Pick apart the testimony. Find contradictions.",
        ),
    }
    moves = _resolve_class_moves(["cross_examine"], index)
    assert moves == [
        ClassMove(
            id="cross_examine",
            label="Cross-Examine",
            description="Pick apart the testimony. Find contradictions.",
        )
    ]


def test_description_prefers_flavor_then_narrator_hint_then_effect():
    index = {
        "a": _beat("a", "A", flavor="flav", narrator_hint="hint", effect="eff"),
        "b": _beat("b", "B", narrator_hint="hint", effect="eff"),
        "c": _beat("c", "C", effect="eff"),
        "d": _beat("d", "D"),
    }
    out = {m.id: m.description for m in _resolve_class_moves(["a", "b", "c", "d"], index)}
    assert out == {"a": "flav", "b": "hint", "c": "eff", "d": None}


def test_missing_id_degrades_to_id_label_and_warns(caplog):
    """A beat id with no matching BeatDef must NOT crash the sheet — it
    degrades to label==id with a warning (loud, not a silent fallback)."""
    with caplog.at_level(logging.WARNING):
        moves = _resolve_class_moves(["ghost_beat"], {})
    assert moves == [ClassMove(id="ghost_beat", label="ghost_beat", description=None)]
    assert any("ghost_beat" in r.getMessage() for r in caplog.records), (
        "missing beat id must be logged loudly, not silently swallowed"
    )


def test_preserves_input_order():
    index = {x: _beat(x, x.title()) for x in ("one", "two", "three")}
    moves = _resolve_class_moves(["three", "one", "two"], index)
    assert [m.id for m in moves] == ["three", "one", "two"]
