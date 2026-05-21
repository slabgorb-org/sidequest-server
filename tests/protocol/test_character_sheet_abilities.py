"""Story 2026-05-10 — protocol contract for full AbilityDefinition + class_moves."""

from __future__ import annotations

from sidequest.game.ability import AbilitySource
from sidequest.game.character import AbilityDefinition
from sidequest.protocol.models import CharacterSheetDetails, ClassMove


def _ab(name: str, source: AbilitySource = AbilitySource.Class) -> AbilityDefinition:
    return AbilityDefinition(
        name=name,
        genre_description=f"{name} prose.",
        mechanical_effect=f"{name} effect.",
        involuntary=False,
        source=source,
    )


def _move(beat_id: str, label: str, description: str | None = None) -> ClassMove:
    return ClassMove(id=beat_id, label=label, description=description)


def test_abilities_serializes_as_full_objects_not_strings():
    sheet = CharacterSheetDetails(
        race="Human",
        stats={"STR": 10},
        abilities=[_ab("Turn Undead")],
        backstory="Backstory",
        personality="Devout",
        equipment=[],
        class_moves=[_move("turn_undead", "Turn Undead")],
    )
    dumped = sheet.model_dump()
    assert isinstance(dumped["abilities"], list)
    assert dumped["abilities"][0]["name"] == "Turn Undead"
    assert dumped["abilities"][0]["source"] == "Class"
    assert dumped["abilities"][0]["genre_description"] == "Turn Undead prose."


def test_class_moves_serialize_as_resolved_objects():
    """class_moves carry id + label + description so the UI renders human
    labels and tooltips, not raw snake_case beat ids."""
    sheet = CharacterSheetDetails(
        race="Human",
        stats={},
        abilities=[],
        backstory="x",
        personality="y",
        equipment=[],
        class_moves=[
            _move("pray", "Pray", "Beseech your deity."),
            _move("shield_bash", "Shield Bash"),
        ],
    )
    # ProtocolBase omits None fields on dump (wire optimization) — a move with
    # no description serializes without the key; the UI reads it as undefined.
    dumped = sheet.model_dump()
    assert dumped["class_moves"] == [
        {"id": "pray", "label": "Pray", "description": "Beseech your deity."},
        {"id": "shield_bash", "label": "Shield Bash"},
    ]


def test_views_build_filters_universal_beats_and_autofilled():
    """The view layer drops attack/defend/flee + 'auto-filled' before sending to UI."""
    from sidequest.server.views import _filter_class_moves

    raw = ["attack", "defend", "flee", "shield_bash", "turn_undead", "pray", "thing-auto-filled"]
    filtered = _filter_class_moves(raw)
    assert filtered == ["shield_bash", "turn_undead", "pray"]
