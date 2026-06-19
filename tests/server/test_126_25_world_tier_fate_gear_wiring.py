"""Story 126-25 RED — WIRING: world-tier ``worlds/<world>/gear.yaml`` merges into
the effective Fate gear catalog the #945 item-promoter reads, so a world-specific
found item promotes to an invokable aspect WHEN that world is active.

Today (the gap this story closes) the promoter at ``narration_apply.py:4967`` reads
``pack.rules.fate.gear_catalog`` — the GENRE tier ONLY. A world-tier GearDef is
never seen, so the authored Oz silver shoes (content already written in 126-21,
inert until this lands) can never promote. The fix mirrors the ADR-145 §D3 by-id
inventory merge (``resolve_inventory``): merge world gear over genre gear, world
wins on a shared id, and the genre catalog stays unpolluted for sibling worlds.

These tests drive the REAL production consumer
(``_apply_narration_result_to_snapshot``) — no source-text assertions, no resolver
function named by hand. They are the sibling of
``tests/server/test_fate_item_promotion_wiring.py`` (which injects gear at the
GENRE tier with ``pack.worlds = {}``); here the gear lives at the WORLD tier with
an EMPTY genre catalog, so a green result PROVES the world merge engaged.

Authored content this contracts against (GearDef shape from the 126-21 oz/gear.yaml):
  id=oz_silver_shoes, name='The Silver Shoes of the Dead Witch',
  grants_aspects=[{text: 'Three Steps Home', kind: permission}].
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.game.ruleset.fate_resolution import Opposition, resolve_action_from_faces
from sidequest.genre.models.inventory import GearDef, GearGrantAspect
from sidequest.genre.models.pack import World
from sidequest.genre.models.rules import FateConfig
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

_ASPECT = "Three Steps Home"
_GEAR_ID = "oz_silver_shoes"
_GEAR_NAME = "The Silver Shoes of the Dead Witch"


def _silver_shoes(*, aspect_text: str = _ASPECT, kind: str = "permission") -> GearDef:
    """The Oz silver-shoes GearDef as authored at the world tier (126-21)."""
    return GearDef(
        id=_GEAR_ID,
        name=_GEAR_NAME,
        grants_aspects=[GearGrantAspect(text=aspect_text, kind=kind)],
    )


def _world_with_gear(gear: list[GearDef]) -> World:
    """A World carrying world-tier gear, built without the full required-field
    set (mirrors ``test_inventory_resolve._make_pack``'s ``World.model_construct``).
    The new ``World.gear`` field is what the loader populates from
    ``worlds/<world>/gear.yaml`` and the resolver reads."""
    return World.model_construct(gear=gear)


def _fate_pc(sam: Any, **sheet_kwargs: Any) -> Any:
    sam.core.fate_sheet = FateSheet(**sheet_kwargs)
    return sam


def _gain(snap: Any, pack: Any, actor_name: str, items_gained: list[dict[str, Any]]) -> None:
    result = NarrationTurnResult(
        narration="You lift them from the dead witch's feet; the silver catches the light.",
        items_gained=items_gained,
    )
    _apply_narration_result_to_snapshot(
        snap, result, actor_name, pack=pack, room=room_for(snap)
    )


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture every ``watcher_hub.publish_event`` call (the AC5 observability
    channel — the same hub ``_emit_inventory_merged`` uses for the inventory merge
    this story mirrors)."""
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    from sidequest.telemetry import watcher_hub

    monkeypatch.setattr(watcher_hub, "publish_event", _capture)
    yield captured


# ── AC2: the full end-to-end path (the must-land deliverable) ──────────────────


def test_world_tier_gear_promotes_to_invokable_aspect_when_world_active(
    snapshot_with_pack, character_named_sam
):
    """active world=oz, genre catalog EMPTY, the silver shoes live ONLY at the
    world tier → gaining them promotes 'Three Steps Home' onto the sheet, it
    reaches the FATE_STATE projection, and it adds +2 to a real 4dF resolution.

    RED today: the promoter reads the genre catalog only (empty) → no promotion."""
    snap, base_pack = snapshot_with_pack
    sam = _fate_pc(character_named_sam, skills={"Athletics": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.world_slug = "oz"
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.rules.fate = FateConfig(gear_catalog=[])  # genre catalog is clean/empty
    pack.worlds = {"oz": _world_with_gear([_silver_shoes()])}

    _gain(snap, pack, sam.core.name, [{"name": _GEAR_NAME, "id": f"narrator:{_GEAR_ID}"}])

    # 1) inventory holds the item, now flagged promoted
    shoes = next((it for it in sam.core.inventory.items if it.get("name") == _GEAR_NAME), None)
    assert shoes is not None and shoes.get("promoted") is True

    # 2) the world-authored aspect landed on the sheet, back-linked to the item id
    sheet = sam.core.fate_sheet
    aspect = next((a for a in sheet.aspects if a.text == _ASPECT), None)
    assert aspect is not None
    assert aspect.source_gear == f"narrator:{_GEAR_ID}"

    # 3) it reaches the player-facing FATE_STATE projection
    payload = build_fate_state_payload(snap)
    sam_entry = next(c for c in payload.characters if c.name == sam.core.name)
    assert any(a.text == _ASPECT for a in sam_entry.aspects)

    # 4) it is mechanically real: invoking it adds +2 to a 4dF resolution
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


# ── AC1: merge semantics — world wins by id, union (not replace), world-scoped ─


def test_world_gear_wins_over_genre_gear_on_shared_id(snapshot_with_pack, character_named_sam):
    """A shared id (``oz_silver_shoes``) exists at BOTH tiers; world=oz active.
    Per the by-id merge (world wins), the WORLD aspect lands, not the genre one.

    RED today: the genre catalog is read, so the genre aspect lands instead."""
    snap, base_pack = snapshot_with_pack
    sam = _fate_pc(character_named_sam, skills={"Athletics": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.world_slug = "oz"
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.rules.fate = FateConfig(
        gear_catalog=[_silver_shoes(aspect_text="GENRE — the merge did not let the world win")]
    )
    pack.worlds = {"oz": _world_with_gear([_silver_shoes()])}  # world grants 'Three Steps Home'

    _gain(snap, pack, sam.core.name, [{"name": _GEAR_NAME, "id": f"narrator:{_GEAR_ID}"}])

    texts = {a.text for a in sam.core.fate_sheet.aspects}
    assert _ASPECT in texts, "world-tier GearDef must win on the shared id"
    assert "GENRE — the merge did not let the world win" not in texts


def test_merge_is_union_genre_gear_still_resolves_with_a_world_active(
    snapshot_with_pack, character_named_sam
):
    """world=oz active, but the gained item is a GENRE-tier GearDef. A by-id UNION
    keeps the genre baseline visible, so it still promotes — a wholesale world
    REPLACE would have dropped it.

    Guard: passes today (genre is read); after the fix it proves the merge is a
    union and a world binding does not clobber the genre catalog."""
    snap, base_pack = snapshot_with_pack
    sam = _fate_pc(character_named_sam, skills={"Athletics": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.world_slug = "oz"
    snap.turn_manager.record_interaction()

    lantern = GearDef(
        id="genre_everlit_lantern",
        name="The Everlit Lantern",
        grants_aspects=[GearGrantAspect(text="A Light in Dark Places", kind="character")],
    )
    pack = copy.deepcopy(base_pack)
    pack.rules.fate = FateConfig(gear_catalog=[lantern])  # genre baseline
    pack.worlds = {"oz": _world_with_gear([_silver_shoes()])}  # world adds shoes

    _gain(
        snap,
        pack,
        sam.core.name,
        [{"name": "The Everlit Lantern", "id": "narrator:genre_everlit_lantern"}],
    )

    assert any(a.text == "A Light in Dark Places" for a in sam.core.fate_sheet.aspects), (
        "the genre baseline must survive the world merge (union, not replace)"
    )


def test_world_gear_does_not_leak_to_a_sibling_world(snapshot_with_pack, character_named_sam):
    """The genre catalog is EMPTY and the shoes live only under world 'oz'. With a
    DIFFERENT world active ('wonderland', unknown to the pack), the shoes must NOT
    promote — the world artifact is scoped to its world (genre stays unpolluted).

    Pairs with the AC2 positive (same gain, world=oz, DOES promote) to prove
    world-scoping rather than a globally-dead catalog."""
    snap, base_pack = snapshot_with_pack
    sam = _fate_pc(character_named_sam, skills={"Athletics": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.world_slug = "wonderland"  # a sibling world; oz gear must not be visible
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.rules.fate = FateConfig(gear_catalog=[])  # clean genre catalog
    pack.worlds = {"oz": _world_with_gear([_silver_shoes()])}  # gear only under 'oz'

    _gain(snap, pack, sam.core.name, [{"name": _GEAR_NAME, "id": f"narrator:{_GEAR_ID}"}])

    shoes = next((it for it in sam.core.inventory.items if it.get("name") == _GEAR_NAME), None)
    assert shoes is not None and "promoted" not in shoes
    assert all(a.text != _ASPECT for a in sam.core.fate_sheet.aspects)


# ── AC3: the conservative-exact matcher discipline holds on the world path ─────


def test_world_path_matcher_promotes_on_exact_name(snapshot_with_pack, character_named_sam):
    """The placement item resolves to the world GearDef by its exact (case-folded)
    NAME — proof the authored chain actually connects in real play, with NO id hint.

    RED today: genre catalog empty → nothing promotes."""
    snap, base_pack = snapshot_with_pack
    sam = _fate_pc(character_named_sam, skills={"Athletics": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.world_slug = "oz"
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.rules.fate = FateConfig(gear_catalog=[])
    pack.worlds = {"oz": _world_with_gear([_silver_shoes()])}

    _gain(snap, pack, sam.core.name, [{"name": _GEAR_NAME}])  # name only, no id

    assert any(a.text == _ASPECT for a in sam.core.fate_sheet.aspects)


def test_world_path_matcher_rejects_partial_name(snapshot_with_pack, character_named_sam):
    """``match_gained_gear`` is conservative-exact: 'Silver' must NOT bind 'The
    Silver Shoes of the Dead Witch'. The world merge must not relax that — a
    partial name grants no aspect even when the world gear IS in scope.

    Guard (the exact-name test above proves this path CAN promote, so a
    no-promotion here is real discipline, not a dead catalog)."""
    snap, base_pack = snapshot_with_pack
    sam = _fate_pc(character_named_sam, skills={"Athletics": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.world_slug = "oz"
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.rules.fate = FateConfig(gear_catalog=[])
    pack.worlds = {"oz": _world_with_gear([_silver_shoes()])}

    _gain(snap, pack, sam.core.name, [{"name": "Silver", "id": "narrator:silver"}])

    assert all(a.text != _ASPECT for a in sam.core.fate_sheet.aspects)
    silver = next((it for it in sam.core.inventory.items if it.get("name") == "Silver"), None)
    assert silver is not None and "promoted" not in silver


# ── AC5: the world-tier merge is observable on the watcher hub ─────────────────


def test_world_gear_merge_emits_observable_event(
    snapshot_with_pack, character_named_sam, captured_watcher_events
):
    """OTEL Observability Principle (the GM panel is the lie detector): when the
    world-tier gear merges into the effective catalog, an observable event fires
    naming the active world AND the world gear id that merged — so the GM panel
    can prove the world artifact was WIRED, not merely authored. Mirrors the
    ``state_transition`` 'merged' event ``_emit_inventory_merged`` publishes.

    RED today: no merge happens, so no such event fires."""
    snap, base_pack = snapshot_with_pack
    sam = _fate_pc(character_named_sam, skills={"Athletics": 1}, fate_points=3)
    snap.characters.append(sam)
    snap.world_slug = "oz"
    snap.turn_manager.record_interaction()

    pack = copy.deepcopy(base_pack)
    pack.rules.fate = FateConfig(gear_catalog=[])
    pack.worlds = {"oz": _world_with_gear([_silver_shoes()])}

    _gain(snap, pack, sam.core.name, [{"name": _GEAR_NAME, "id": f"narrator:{_GEAR_ID}"}])

    merge_events = [
        e
        for e in captured_watcher_events
        if e["event_type"] == "state_transition"
        and "gear" in str(e["fields"].get("field", "")).lower()
        and e["fields"].get("world_slug") == "oz"
        and _GEAR_ID in str(e["fields"])
    ]
    assert merge_events, (
        "the world-tier gear merge must emit an observable state_transition event "
        f"naming world=oz and the merged gear id {_GEAR_ID!r}; "
        f"captured fields: {[e['fields'] for e in captured_watcher_events]}"
    )
