"""Story 126-35 — activate the dormant narrator ``grants_aspect`` lever (Keith
design call 2026-06-20, PATH c).

The ENGINE half shipped in 126-21/126-25: ``promote_gained_item`` already mints a
capped ``kind="character"`` aspect and emits ``fate.item_promoted`` with
``source="narrator"`` when an item carries ``grants_aspect``, and
``_apply_narration_result_to_snapshot`` already consumes it
(``narration_apply.py`` reads ``entry.get("grants_aspect")``). What is DORMANT is
the narrator-CONTRACT surface: post-151-4 (ADR-150 step 4) ``items_gained`` is
sourced SOLELY from the sidecar extractor (``SidecarExtraction`` →
``merge_sidecar_extraction_transactional``), and that tool-schema never surfaces
``grants_aspect`` — so the narrator is never told it MAY mark an item significant.

AC1 (the genuine RED) — ``test_extraction_tool_schema_surfaces_grants_aspect`` —
pins the missing schema surface. AC2/AC3/AC4 are the full-wiring / OTEL / invariant
guards the story requires; several are GREEN-on-arrival because the engine already
exists, and they pin the end-to-end contract so a regression while wiring the
narrator surface is caught (No half-wired features; every suite needs a wiring test).

Aspect kind is ``"character"`` — per the shipped promoter AND the spec
(``docs/superpowers/specs/2026-06-18-significant-items-invokable-fate-aspects-design.md``).
The story AC2's "situation" wording is superseded; see the TEA deviation in the
session file.
"""

from __future__ import annotations

import copy

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.agents.sidecar_extractor import SidecarExtraction, _extraction_tool_schema
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.game.ruleset.fate_resolution import Opposition, resolve_action_from_faces
from sidequest.genre.models.rules import FateConfig
from sidequest.server.narration_apply import (
    _apply_narration_result_to_snapshot,
    merge_sidecar_extraction_transactional,
)
from tests._helpers.session_room import room_for

_ASPECT = "Knows the Hidden Trails"
_ITEM_NAME = "Surveyor's Map"
_ITEM_ID = "narrator:surveyors_map"


def _items_gained_item_schema(schema: dict) -> dict:
    """The JSON-schema node describing ONE ``items_gained`` entry, resolving a
    ``$ref`` into ``$defs`` if the entry is a typed submodel. Behavioral — reads
    the real ``model_json_schema()`` output, never greps source (CLAUDE.md
    No-Source-Text-Wiring-Tests)."""
    item = schema["properties"]["items_gained"].get("items", {})
    ref = item.get("$ref")
    if ref:
        item = schema.get("$defs", {}).get(ref.split("/")[-1], {})
    return item


# --- AC1: the narrator item-grant tool-contract surfaces grants_aspect (RED) ----


def test_extraction_tool_schema_surfaces_grants_aspect():
    """AC1: the LIVE narrator item-grant tool-contract — the post-151-4 sidecar
    extractor, the SOLE source of ``items_gained`` — must surface an OPTIONAL
    ``grants_aspect`` field on an item entry, carrying the instruction the narrator
    reads. Today ``items_gained`` is ``list[dict[str, Any]]`` (a bare object with no
    properties), so the narrator has no signal the field exists → dormant lever."""
    schema = _extraction_tool_schema()
    item_schema = _items_gained_item_schema(schema)
    props = item_schema.get("properties", {})

    assert "grants_aspect" in props, (
        "the items_gained tool-schema does not surface a grants_aspect field — the "
        "narrator has no way to deliberately mark a significant item (the dormant "
        "Phase-2 lever, story 126-35). Resolved item schema was: " + repr(item_schema)
    )
    # The "prompt-zone instruction" rides on the field description (the text the
    # extractor LLM actually reads), so a bare typed field is not enough.
    description = (props["grants_aspect"].get("description") or "").strip()
    assert description, (
        "grants_aspect must carry a description instructing the narrator WHEN to set "
        "it (narrator-authored promotion only — never auto-promote every item)"
    )


def test_grants_aspect_is_optional_not_required():
    """AC1/AC4 corollary: surfacing grants_aspect must NOT make it mandatory — a
    plain item (a hat is a hat) stays valid with no aspect (ADR-144, no equipment
    economy). Guards against a schema change that forces the field."""
    schema = _extraction_tool_schema()
    item_schema = _items_gained_item_schema(schema)
    required = item_schema.get("required", [])
    assert "grants_aspect" not in required, (
        "grants_aspect must be OPTIONAL — most granted items are pure flavor"
    )


def test_merge_sidecar_extraction_preserves_grants_aspect():
    """AC1/AC2 wiring: once the extractor emits grants_aspect, the live
    ``merge_sidecar_extraction_transactional`` carries the key onto the result
    verbatim (the field is not stripped en route to the promoter)."""
    result = NarrationTurnResult(narration="A drifter presses a folded map into your hands.")
    extraction = SidecarExtraction(
        items_gained=[{"name": _ITEM_NAME, "id": _ITEM_ID, "grants_aspect": _ASPECT}]
    )
    merged = merge_sidecar_extraction_transactional(result, extraction)
    assert merged.items_gained[0].get("grants_aspect") == _ASPECT


# --- AC2: full narrator-set wiring — promote → project → invoke +2 in 4dF --------


def test_narrator_grants_aspect_full_wiring_to_4df(snapshot_with_pack, character_named_sam):
    """AC2: the drifter's-surveyor's-map case end to end. A Fate PC gains a
    narrator-INVENTED item (no catalog gear) flagged ``grants_aspect``; the aspect
    lands on the sheet, reaches the player-facing FATE_STATE projection, and adds +2
    to a real 4dF resolution. Mirrors the catalog-path wiring test for the
    narrator-authored route (the existing narrator test stops at the sheet)."""
    snap, base_pack = snapshot_with_pack
    sam = character_named_sam
    sam.core.fate_sheet = FateSheet(skills={"Survival": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.worlds = {}
    pack.rules.fate = FateConfig(gear_catalog=[])  # no authored gear → narrator path

    result = NarrationTurnResult(
        narration="The drifter presses a hand-inked surveyor's map into your palm.",
        items_gained=[{"name": _ITEM_NAME, "id": _ITEM_ID, "grants_aspect": _ASPECT}],
    )
    _apply_narration_result_to_snapshot(snap, result, sam.core.name, pack=pack, room=room_for(snap))

    # 1) inventory holds the item, flagged promoted
    item = next((it for it in sam.core.inventory.items if it.get("name") == _ITEM_NAME), None)
    assert item is not None and item.get("promoted") is True

    # 2) a capped invokable aspect landed, back-linked to the item id
    sheet = sam.core.fate_sheet
    aspect = next((a for a in sheet.aspects if a.text == _ASPECT), None)
    assert aspect is not None
    # kind is "character" per the shipped promoter + spec 2026-06-18 (NOT "situation"
    # as AC2 worded it — see TEA deviation). free_invokes 0: narrator makes it TRUE,
    # never STRONG.
    assert aspect.kind == "character" and aspect.free_invokes == 0
    # the item id is minted from the NAME at apply-time (_narrator_item_dict ignores
    # a passed id), so back-link against the ACTUAL stored id, not the hand-set one
    # (mirrors the 126-25 source_gear assertion fix).
    assert aspect.source_gear == item["id"]

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
    assert out.ladder_total == 3  # skill 1 + faces 0 + invoke +2


# --- AC3: OTEL fate.item_promoted fires with source=narrator (wiring level) ------


def test_narrator_promotion_emits_item_promoted_span_source_narrator(
    snapshot_with_pack, character_named_sam, otel_capture
):
    """AC3: driving the narrator-set path through the real apply machinery fires
    ``fate.item_promoted`` with ``source="narrator"`` — the GM-panel lie-detector
    that distinguishes the narrator-authored route from the catalog route
    (``source="catalog"``). The existing span test only covers ``source="catalog"``
    at the unit level."""
    snap, base_pack = snapshot_with_pack
    sam = character_named_sam
    sam.core.fate_sheet = FateSheet(skills={"Survival": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.worlds = {}
    pack.rules.fate = FateConfig(gear_catalog=[])

    result = NarrationTurnResult(
        narration="The drifter presses a surveyor's map into your palm.",
        items_gained=[{"name": _ITEM_NAME, "id": _ITEM_ID, "grants_aspect": _ASPECT}],
    )
    _apply_narration_result_to_snapshot(snap, result, sam.core.name, pack=pack, room=room_for(snap))

    promoted = [s for s in otel_capture.get_finished_spans() if s.name == "fate.item_promoted"]
    assert len(promoted) == 1, f"expected one fate.item_promoted span, got {len(promoted)}"
    attrs = promoted[0].attributes
    assert attrs["source"] == "narrator"
    assert attrs["aspects_added"] == 1
    assert attrs["deduped"] is False


# --- AC4: invariant — no grants_aspect → no promotion (no auto-economy) ----------


def test_fate_item_without_grants_aspect_does_not_promote(
    snapshot_with_pack, character_named_sam, otel_capture
):
    """AC4: a Fate PC gains an ad-hoc item with NO grants_aspect and NO catalog
    match → it stays inventory flavor. No aspect minted, no promoted flag, no
    fate.item_promoted span. Fate's no-equipment-economy model (ADR-144) is
    untouched — the engine never auto-promotes."""
    snap, base_pack = snapshot_with_pack
    sam = character_named_sam
    sam.core.fate_sheet = FateSheet(skills={"Survival": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.worlds = {}
    pack.rules.fate = FateConfig(gear_catalog=[])

    result = NarrationTurnResult(
        narration="You pocket a plain tin cup from the campsite.",
        items_gained=[{"name": "Tin Cup", "id": "narrator:tin_cup"}],
    )
    _apply_narration_result_to_snapshot(snap, result, sam.core.name, pack=pack, room=room_for(snap))

    cup = next((it for it in sam.core.inventory.items if it.get("name") == "Tin Cup"), None)
    assert cup is not None
    assert "promoted" not in cup
    # no aspect was minted for this item
    assert not any(a.source_gear == "narrator:tin_cup" for a in sam.core.fate_sheet.aspects)
    # and the promoter never fired
    assert not [s for s in otel_capture.get_finished_spans() if s.name == "fate.item_promoted"]
