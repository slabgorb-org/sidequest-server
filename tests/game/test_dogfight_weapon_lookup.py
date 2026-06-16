"""Story 114-15 — ``build_dogfight_weapon_lookup``: the dogfight resolves its
ship weapon from the genre-tier ``ship_weapons`` collection ONLY.

The production dogfight path (narration_apply) used to build an inline lambda that
resolved ``player_weapon`` / ``opponent_weapon`` against
``resolve_inventory(world).item_catalog``. Story 114-15 moves the ship weapon out
of the personal ``item_catalog`` into ``ship_weapons`` and extracts the lookup into
a named, testable ``build_dogfight_weapon_lookup(resolved_inventory)`` helper that:

  - resolves the weapon id against ``resolved_inventory.ship_weapons`` ONLY (never
    the personal ``item_catalog``) — keeping ship weapons off the personal-gear
    surface (the category-error this story fixes),
  - returns ``None`` for an unknown id so ``_resolve_weapon`` keeps failing loud
    (no silent fallback),
  - emits a ``dogfight.weapon_resolved`` OTEL span on each hit carrying
    ``source=ship_weapons`` + the weapon id + ``armor_piercing`` (AC5 — the GM-panel
    lie-detector for "the dogfight is using the real ship weapon").
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.dogfight_shot import build_dogfight_weapon_lookup
from sidequest.genre.models.inventory import (
    CatalogItem,
    DamageSpec,
    InventoryConfig,
    ItemProvenance,
)


def _ship_weapon(item_id: str = "multifocal_laser", *, ap: int = 20) -> CatalogItem:
    return CatalogItem(
        id=item_id,
        name="Multifocal Laser",
        description="A strike-fighter ship weapon.",
        category="weapon",
        tags=["ranged", "ship-weapon"],
        damage=DamageSpec(dice="1d4", armor_piercing=ap),
        provenance=ItemProvenance(mode="bespoke"),
    )


def _personal_item(item_id: str) -> CatalogItem:
    return CatalogItem(
        id=item_id,
        name=item_id,
        description="d",
        category="weapon",
        damage=DamageSpec(dice="1d6", armor_piercing=0),
    )


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """In-memory span exporter (mirrors tests/telemetry/spans/test_dogfight_shot_spans.py).

    Monkeypatching ``sidequest.telemetry.spans.tracer`` leaves the global provider
    untouched while capturing spans emitted during the test.
    """
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


class TestBuildDogfightWeaponLookup:
    def test_resolves_ship_weapon_with_armor_piercing_intact(self) -> None:
        # AC2: the dogfight weapon resolves to a DamageSpec carrying AP 20.
        inv = InventoryConfig(ship_weapons=[_ship_weapon()])
        lookup = build_dogfight_weapon_lookup(inv)
        item = lookup("multifocal_laser")
        assert item is not None, "ship weapon must resolve from ship_weapons"
        assert item.damage is not None
        assert item.damage.armor_piercing == 20

    def test_unknown_id_returns_none(self) -> None:
        # AC4 (preserved fail-loud): an unknown id resolves to None so the
        # downstream _resolve_weapon raises rather than silently substituting.
        inv = InventoryConfig(ship_weapons=[_ship_weapon()])
        assert build_dogfight_weapon_lookup(inv)("unknown_cannon") is None

    def test_does_not_resolve_from_personal_item_catalog(self) -> None:
        # Exclusivity regression: a weapon that lives ONLY in the personal
        # item_catalog (not ship_weapons) must NOT resolve through the dogfight
        # lookup. Catches a Dev who unions item_catalog back in — which would
        # re-leak ship weapons onto the personal surface.
        inv = InventoryConfig(
            item_catalog=[_personal_item("multifocal_laser")],
            ship_weapons=[],
        )
        assert build_dogfight_weapon_lookup(inv)("multifocal_laser") is None

    def test_none_inventory_returns_none(self) -> None:
        # resolve_inventory can return None (no inventory at either tier). The
        # lookup must tolerate it (returning None), leaving the fail-loud to
        # _resolve_weapon rather than raising AttributeError mid-build.
        lookup = build_dogfight_weapon_lookup(None)
        assert lookup("multifocal_laser") is None

    def test_emits_weapon_resolved_span_on_hit(self, exporter: InMemorySpanExporter) -> None:
        # AC5: a successful resolution emits the dogfight.weapon_resolved span with
        # source=ship_weapons + weapon id + armor_piercing for the GM panel.
        inv = InventoryConfig(ship_weapons=[_ship_weapon()])
        build_dogfight_weapon_lookup(inv)("multifocal_laser")

        spans = [s for s in exporter.get_finished_spans() if s.name == "dogfight.weapon_resolved"]
        assert spans, "no dogfight.weapon_resolved span emitted on a successful lookup"
        attrs = spans[-1].attributes or {}
        assert attrs.get("source") == "ship_weapons"
        assert attrs.get("weapon_id") == "multifocal_laser"
        assert attrs.get("armor_piercing") == 20

    def test_no_span_on_miss(self, exporter: InMemorySpanExporter) -> None:
        # A miss is not a resolution — no weapon_resolved span should fire (the span
        # is the lie-detector for "we used a REAL ship weapon", so a miss must not
        # forge one).
        inv = InventoryConfig(ship_weapons=[_ship_weapon()])
        build_dogfight_weapon_lookup(inv)("unknown_cannon")
        assert not [
            s for s in exporter.get_finished_spans() if s.name == "dogfight.weapon_resolved"
        ]
