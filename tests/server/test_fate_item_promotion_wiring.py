"""WIRING: the production items_gained path promotes a significant catalog item
to an invokable Fate aspect that reaches the FATE_STATE projection and adds +2 to
a real 4dF resolution (spec 2026-06-18). Drives the real
``_apply_narration_result_to_snapshot`` — no source-text assertions."""

from __future__ import annotations

import copy

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.game.ruleset.fate_resolution import Opposition, resolve_action_from_faces
from sidequest.genre.models.inventory import GearDef, GearGrantAspect
from sidequest.genre.models.rules import FateConfig
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

_ASPECT = "The Silver Shoes of the Dead Witch"


def _slippers_gear() -> GearDef:
    return GearDef(
        id="silver_shoes",
        name="Silver Shoes",
        grants_aspects=[GearGrantAspect(text=_ASPECT, kind="character")],
    )


def test_gained_catalog_item_promotes_to_invokable_aspect(snapshot_with_pack, character_named_sam):
    snap, base_pack = snapshot_with_pack
    sam = character_named_sam
    sam.core.fate_sheet = FateSheet(skills={"Fight": 1}, fate_points=3)  # fate-bound PC
    snap.characters.append(sam)
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.worlds = {}  # force genre-tier resolution (see template test, Epic 94 note)
    pack.rules.fate = FateConfig(gear_catalog=[_slippers_gear()])

    result = NarrationTurnResult(
        narration="You lift the silver shoes from the dead witch's feet.",
        items_gained=[{"name": "Silver Shoes", "id": "narrator:silver_shoes"}],
    )
    _apply_narration_result_to_snapshot(snap, result, sam.core.name, pack=pack, room=room_for(snap))

    # 1) inventory still holds the item, now flagged promoted
    shoes = next((it for it in sam.core.inventory.items if it.get("name") == "Silver Shoes"), None)
    assert shoes is not None and shoes.get("promoted") is True

    # 2) an invokable aspect landed on the sheet, back-linked to the item id
    sheet = sam.core.fate_sheet
    aspect = next((a for a in sheet.aspects if a.text == _ASPECT), None)
    assert aspect is not None
    assert aspect.kind == "character" and aspect.free_invokes == 0
    assert aspect.source_gear == "narrator:silver_shoes"

    # 3) it reaches the player-facing FATE_STATE projection
    payload = build_fate_state_payload(snap)
    sam_entry = next(c for c in payload.characters if c.name == sam.core.name)
    assert any(a.text == _ASPECT for a in sam_entry.aspects)

    # 4) it is mechanically real: invoking adds +2 to a 4dF resolution
    bonus = get_ruleset_module("fate").invoke_aspect(
        sheet=sheet, aspect_text=_ASPECT, actor=sam.core.name
    )
    out = resolve_action_from_faces(
        skill_rating=1,
        opposition=Opposition(value=0, kind="passive"),
        faces=(0, 0, 0, 0),
        invoke_bonus=bonus,
    )
    assert out.ladder_total == 3


def test_non_fate_pc_is_untouched(snapshot_with_pack, character_named_sam):
    """A PC with no Fate sheet: items_gained behaves exactly as before — no
    aspect, no promoted flag, no crash."""
    snap, base_pack = snapshot_with_pack
    sam = character_named_sam
    assert sam.core.fate_sheet is None  # the gate signal is absent
    snap.characters.append(sam)
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.worlds = {}

    result = NarrationTurnResult(
        narration="You pocket a curious bauble.",
        items_gained=[{"name": "Curious Bauble", "category": "treasure"}],
    )
    _apply_narration_result_to_snapshot(snap, result, sam.core.name, pack=pack, room=room_for(snap))

    bauble = next(
        (it for it in sam.core.inventory.items if it.get("name") == "Curious Bauble"), None
    )
    assert bauble is not None
    assert "promoted" not in bauble
