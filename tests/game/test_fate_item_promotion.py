"""Significant items gained in play promote to invokable Fate aspects (spec
2026-06-18). Phase 1: authored-catalog gear → aspects + permissions; stunts
deferred; dedup; the promoted aspect is mechanically real (+2 on a 4dF roll)."""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_item_promotion import (
    ItemPromotionResult,
    match_gained_gear,
    promote_gained_item,
)
from sidequest.game.ruleset.fate_resolution import Opposition, resolve_action_from_faces
from sidequest.genre.models.inventory import GearDef, GearGrantAspect, GearGrantStunt


def _slippers() -> GearDef:
    return GearDef(
        id="silver_shoes",
        name="Silver Shoes",
        grants_aspects=[
            GearGrantAspect(text="The Silver Shoes of the Dead Witch", kind="character")
        ],
    )


def test_catalog_gear_promotes_aspect_with_back_link():
    sheet = FateSheet(skills={"Fight": 1})
    res = promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="Dorothy",
    )
    assert res == ItemPromotionResult(
        promoted=True, aspects_added=1, source="catalog", stunts_deferred=0, deduped=False
    )
    aspect = next(a for a in sheet.aspects if a.text == "The Silver Shoes of the Dead Witch")
    assert aspect.kind == "character"
    assert aspect.free_invokes == 0  # never free power; invoking costs a fate point
    assert aspect.source_gear == "narrator:silver_shoes"  # back-link is the item id


def test_permission_kind_is_preserved():
    gear = GearDef(
        id="ruby_lens",
        name="Ruby Lens",
        grants_aspects=[GearGrantAspect(text="Can See In The Dark", kind="permission")],
    )
    sheet = FateSheet()
    promote_gained_item(
        sheet=sheet, item_id="ruby_lens", item_name="Ruby Lens", gear_defs=[gear], actor="X"
    )
    assert sheet.aspects[0].kind == "permission"


def test_matched_gear_stunts_are_deferred_not_applied():
    gear = GearDef(
        id="click_heels",
        name="Charmed Heels",
        grants_aspects=[GearGrantAspect(text="Charmed Heels", kind="character")],
        grants_stunts=[GearGrantStunt(name="Click Three Times", description="Teleport home")],
    )
    sheet = FateSheet()
    res = promote_gained_item(
        sheet=sheet, item_id="click_heels", item_name="Charmed Heels", gear_defs=[gear], actor="X"
    )
    assert res.aspects_added == 1
    assert res.stunts_deferred == 1
    assert sheet.stunts == []  # the stunt was NOT applied (refresh-economy deferral)


def test_no_gear_match_is_not_promoted():
    sheet = FateSheet()
    res = promote_gained_item(
        sheet=sheet, item_id="narrator:banjo", item_name="Banjo", gear_defs=[_slippers()], actor="X"
    )
    assert res.promoted is False
    assert res.aspects_added == 0
    assert sheet.aspects == []


def test_second_grant_of_same_item_is_a_dedup_noop():
    sheet = FateSheet()
    promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="X",
    )
    res2 = promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="X",
    )
    assert res2.deduped is True
    assert res2.promoted is False
    assert len([a for a in sheet.aspects if a.source_gear == "narrator:silver_shoes"]) == 1


def test_match_gained_gear_mirrors_conservative_matching():
    gear_defs = [_slippers()]
    assert match_gained_gear("narrator:silver_shoes", "Silver Shoes", gear_defs) is not None
    assert match_gained_gear("", "silver shoes", gear_defs) is not None  # case-folded name
    assert match_gained_gear("", "Silver", gear_defs) is None  # no fuzzy/partial match


def test_promoted_aspect_is_mechanically_real_plus_two():
    sheet = FateSheet(skills={"Fight": 1}, fate_points=3)
    promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="Dorothy",
    )
    module = get_ruleset_module("fate")
    bonus = module.invoke_aspect(
        sheet=sheet, aspect_text="The Silver Shoes of the Dead Witch", actor="Dorothy"
    )
    assert bonus == 2
    out = resolve_action_from_faces(
        skill_rating=1,
        opposition=Opposition(value=0, kind="passive"),
        faces=(0, 0, 0, 0),
        invoke_bonus=bonus,
    )
    assert out.ladder_total == 3  # skill 1 + faces 0 + invoke +2


def test_emits_item_promoted_span():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sheet = FateSheet()
    promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="Dorothy",
        _tracer=provider.get_tracer("t"),
    )
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["fate.item_promoted"]
    assert spans[0].attributes["source"] == "catalog"
    assert spans[0].attributes["aspects_added"] == 1


def test_dedup_noop_is_logged_not_silent():
    """No-Silent-Fallbacks contract: a re-grant of an already-promoted item
    emits a fate.item_promoted span with deduped=True (source="" by design),
    rather than silently returning the no-op."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    sheet = FateSheet()
    promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="Dorothy",
        _tracer=tracer,
    )
    promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="Dorothy",
        _tracer=tracer,
    )
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["fate.item_promoted", "fate.item_promoted"]
    assert spans[1].attributes["deduped"] is True
    assert spans[1].attributes["aspects_added"] == 0


def test_stunt_only_gear_defers_without_promoting():
    """Matched gear with stunts but no aspects: nothing is appended (promoted
    False), the stunt is counted/deferred, and a span still fires so the GM panel
    sees the deferral."""
    gear = GearDef(
        id="ruby_charm",
        name="Ruby Charm",
        grants_aspects=[],
        grants_stunts=[GearGrantStunt(name="Click Three Times", description="Teleport home")],
    )
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sheet = FateSheet()
    res = promote_gained_item(
        sheet=sheet,
        item_id="ruby_charm",
        item_name="Ruby Charm",
        gear_defs=[gear],
        actor="X",
        _tracer=provider.get_tracer("t"),
    )
    assert res.promoted is False
    assert res.stunts_deferred == 1
    assert res.source == "catalog"
    assert sheet.aspects == []
    assert sheet.stunts == []
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["fate.item_promoted"]
    assert spans[0].attributes["aspects_added"] == 0
    assert spans[0].attributes["stunts_deferred"] == 1


def test_narrator_aspect_mints_one_capped_character_aspect():
    sheet = FateSheet(fate_points=3)
    res = promote_gained_item(
        sheet=sheet,
        item_id="narrator:locket",
        item_name="Mysterious Locket",
        gear_defs=[],
        actor="Dorothy",
        narrator_aspect="The Locket That Hums Near Magic",
    )
    assert res.source == "narrator"
    assert res.aspects_added == 1
    aspect = sheet.aspects[0]
    assert aspect.kind == "character"
    assert aspect.free_invokes == 0  # narrator can make it TRUE, never STRONG
    assert aspect.source_gear == "narrator:locket"


def test_catalog_match_wins_over_narrator_hint():
    sheet = FateSheet()
    res = promote_gained_item(
        sheet=sheet,
        item_id="narrator:silver_shoes",
        item_name="Silver Shoes",
        gear_defs=[_slippers()],
        actor="X",
        narrator_aspect="Some Improvised Aspect",
    )
    assert res.source == "catalog"
    assert [a.text for a in sheet.aspects] == ["The Silver Shoes of the Dead Witch"]


def test_blank_narrator_aspect_is_no_promotion():
    sheet = FateSheet()
    for blank in ("", "   ", None):
        res = promote_gained_item(
            sheet=sheet,
            item_id="narrator:rock",
            item_name="Rock",
            gear_defs=[],
            actor="X",
            narrator_aspect=blank,
        )
        assert res.promoted is False
    assert sheet.aspects == []
