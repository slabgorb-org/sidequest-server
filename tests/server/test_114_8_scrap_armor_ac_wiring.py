"""Story 114-8 (RED) — scrap_armor's AWN AC is WIRED, not just present in YAML.

The companion content test (`tests/genre/test_114_8_mutant_wasteland_awn_inventory.py`)
asserts `scrap_armor` carries `armor_class: 15` (AWN Scrap Mail). This file proves
that value is actually *consumed*: equipping scrap_armor through the real chargen
armor-derivation seam (`equip_starting_armor`, story 106-1) must raise the
character's `core.armor_class` to 15 and fire the GM-panel lie-detector span
`chargen.armor_equipped` — NOT the loud `chargen.armor_unresolved` gap span.

This is the wiring test (CLAUDE.md "Verify Wiring, Not Just Existence" + the OTEL
Observability Principle). Today scrap_armor has no catalog `armor_class`, so the
derivation can't read it: the PC stays at the unarmored AC 10 and a
`chargen.armor_unresolved` (reason=catalog_armor_class_missing) span fires. These
assertions are RED until the content carries the AWN AC.

Driven against the REAL mutant_wasteland pack (not a synthetic fixture) so a green
result proves the production content + the production derivation agree end to end.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_PACK_SLUG = "mutant_wasteland"
_SPAN_ARMOR_EQUIPPED = "chargen.armor_equipped"
_SPAN_ARMOR_UNRESOLVED = "chargen.armor_unresolved"
_AWN_SCRAP_MAIL_AC = 15


def _load_inventory():
    try:
        pack = load_genre_pack(find_pack_path(_PACK_SLUG))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))
    assert pack.inventory is not None, "mutant_wasteland must ship a genre-tier inventory"
    return pack.inventory


def _scrap_armor_item() -> dict:
    """An inventory item dict for genre `scrap_armor` (builder shape, unequipped),
    so `equip_starting_armor` resolves it against the real catalog by id."""
    return {
        "id": "scrap_armor",
        "name": "Scrap Armor",
        "description": "Sheet metal plates riveted over leather.",
        "category": "armor",
        "value": 6,
        "weight": 8.0,
        "rarity": "common",
        "narrative_weight": 0.1,
        "tags": ["armor", "heavy", "salvage"],
        "equipped": False,
        "quantity": 1,
        "uses_remaining": None,
        "state": "Carried",
    }


def _make_character(items: list[dict], *, char_class: str = "Scavenger"):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name="Rusk",
        description="A wary scavenger in welded plate.",
        personality="cautious",
        inventory=Inventory(items=list(items)),
    )
    return Character(
        core=core, char_class=char_class, race="Human", backstory="Born in the wastes."
    )


def _equip(character, inventory_config):
    from sidequest.server.dispatch.chargen_loadout import equip_starting_armor

    return equip_starting_armor(character, inventory_config, genre=_PACK_SLUG, world="")


def test_equipping_scrap_armor_derives_awn_ac_15():
    """RED: a PC who dons genre scrap_armor derives core.armor_class = 15 (AWN
    Scrap Mail). Today scrap_armor has no catalog armor_class → AC stays at the
    unarmored 10."""
    inv = _load_inventory()
    character = _make_character([_scrap_armor_item()])
    assert character.core.armor_class == 10, "precondition: unarmored default"

    derived = _equip(character, inv)

    assert derived == _AWN_SCRAP_MAIL_AC, (
        f"scrap_armor must derive AWN Scrap Mail AC {_AWN_SCRAP_MAIL_AC}; got {derived}"
    )
    assert character.core.armor_class == _AWN_SCRAP_MAIL_AC


def test_scrap_armor_fires_equipped_span_not_unresolved(otel_capture):
    """RED + OTEL gate: equipping scrap_armor must fire `chargen.armor_equipped`
    (carrying armor_class=15) and must NOT fire `chargen.armor_unresolved`. Today
    the opposite holds — the gap span fires and the PC fights at AC 10."""
    inv = _load_inventory()
    character = _make_character([_scrap_armor_item()])

    _equip(character, inv)

    equipped = [s for s in otel_capture.get_finished_spans() if s.name == _SPAN_ARMOR_EQUIPPED]
    unresolved = [s for s in otel_capture.get_finished_spans() if s.name == _SPAN_ARMOR_UNRESOLVED]

    assert not unresolved, (
        "scrap_armor with a real AWN armor_class must NOT fire the content-gap span "
        f"{_SPAN_ARMOR_UNRESOLVED}"
    )
    assert len(equipped) == 1, f"exactly one {_SPAN_ARMOR_EQUIPPED} span must fire"
    attrs = dict(equipped[0].attributes or {})
    assert attrs.get("item_id") == "scrap_armor"
    assert attrs.get("armor_class") == _AWN_SCRAP_MAIL_AC
    assert attrs.get("ac_before") == 10
    assert attrs.get("ac_after") == _AWN_SCRAP_MAIL_AC
