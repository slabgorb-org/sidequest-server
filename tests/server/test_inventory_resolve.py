"""Tests for ``resolve_inventory`` and the chargen-loadout inventory wiring.

Epic 94 (genre/world boundary correction): a world's item catalog, class
starting-kits, gold, and currency are a world-tier CAST/CATALOG surface. The
chargen loadout, currency view, and gained-item catalog must read the inventory
world-first. Mirrors ``tests/server/test_class_resolve.py``.

Regression context (playtest 2026-06-06): content commit relocated
``inventory.yaml`` from the genre tier down to each world, but the server still
read ``pack.inventory`` (genre tier, now ``None`` for migrated packs) — so
``apply_starting_loadout`` silently no-op'd and the inventory tab rendered blank
in coyote_star + evropi.

Covers:
  - resolver semantics (world override replaces genre inventory; fall-through to
    genre for world-empty / unknown / no-slug; None when neither tier ships one),
  - the world-tier OTEL ``state_transition`` span fires with ``tier=world``,
  - the WIRING test: ``resolve_inventory`` → ``apply_starting_loadout`` actually
    populates a character's inventory from the world tier (the bug was that the
    genre-tier read returned None and the loadout no-op'd).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.genre.models.inventory import CatalogItem, CurrencyConfig, InventoryConfig
from sidequest.genre.models.pack import GenrePack, World
from sidequest.server.dispatch.chargen_loadout import apply_starting_loadout
from sidequest.server.dispatch.inventory_resolve import resolve_inventory


@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    from sidequest.telemetry import watcher_hub

    monkeypatch.setattr(watcher_hub, "publish_event", _capture)
    yield captured


def _catalog_item(item_id: str) -> CatalogItem:
    return CatalogItem(
        id=item_id,
        name=item_id.replace("_", " ").title(),
        description="A thing.",
        category="tool",
        value=1,
        weight=1.0,
        rarity="common",
        tags=[],
    )


def _inv(
    *,
    starting_equipment: dict[str, list[str]] | None = None,
    starting_gold: dict[str, int] | None = None,
    catalog: list[str] | None = None,
    currency: str | None = None,
) -> InventoryConfig:
    return InventoryConfig(
        currency=CurrencyConfig(name=currency) if currency else None,
        item_catalog=[_catalog_item(i) for i in (catalog or [])],
        starting_equipment=starting_equipment or {},
        starting_gold=starting_gold or {},
    )


def _make_pack(
    *,
    genre_inventory: InventoryConfig | None,
    worlds: dict[str, InventoryConfig | None],
) -> GenrePack:
    world_objs: dict[str, World] = {}
    for slug, inv in worlds.items():
        world_objs[slug] = cast(World, World.model_construct(inventory=inv))
    return cast(
        GenrePack,
        GenrePack.model_construct(inventory=genre_inventory, worlds=world_objs),
    )


class TestResolveInventory:
    def test_world_override_replaces_genre_inventory(self) -> None:
        pack = _make_pack(
            genre_inventory=_inv(currency="gold", catalog=["torch"]),
            worlds={"coyote_star": _inv(currency="credits", catalog=["blaster"])},
        )
        result = resolve_inventory(pack, "coyote_star")
        assert result is not None
        assert result.currency is not None and result.currency.name == "credits"
        assert [i.id for i in result.item_catalog] == ["blaster"]

    def test_world_without_inventory_falls_back_to_genre(self) -> None:
        pack = _make_pack(
            genre_inventory=_inv(currency="gold"),
            worlds={"w": None},
        )
        result = resolve_inventory(pack, "w")
        assert result is not None and result.currency is not None
        assert result.currency.name == "gold"

    def test_missing_slug_returns_genre_inventory(self) -> None:
        pack = _make_pack(
            genre_inventory=_inv(currency="gold"),
            worlds={"w": _inv(currency="credits")},
        )
        assert resolve_inventory(pack, None).currency.name == "gold"  # type: ignore[union-attr]
        assert resolve_inventory(pack, "").currency.name == "gold"  # type: ignore[union-attr]

    def test_unknown_world_returns_genre_inventory(self) -> None:
        pack = _make_pack(
            genre_inventory=_inv(currency="gold"),
            worlds={"w": _inv(currency="credits")},
        )
        assert resolve_inventory(pack, "nowhere").currency.name == "gold"  # type: ignore[union-attr]

    def test_none_at_both_tiers_returns_none(self) -> None:
        pack = _make_pack(genre_inventory=None, worlds={"w": None})
        assert resolve_inventory(pack, "w") is None


class TestResolveInventoryOtel:
    def test_world_resolution_emits_world_tier_span(
        self, captured_watcher_events: list[dict]
    ) -> None:
        pack = _make_pack(
            genre_inventory=_inv(currency="gold"),
            worlds={
                "coyote_star": _inv(
                    currency="credits", catalog=["a", "b"], starting_equipment={"Pilot": ["a"]}
                )
            },
        )
        resolve_inventory(pack, "coyote_star")

        spans = [
            e
            for e in captured_watcher_events
            if e["event_type"] == "state_transition"
            and e["fields"].get("field") == "resolved_inventory"
        ]
        assert spans, "no resolved_inventory span emitted"
        fields = spans[-1]["fields"]
        assert fields["tier"] == "world"
        assert fields["world_slug"] == "coyote_star"
        assert fields["catalog_count"] == 2
        assert fields["class_kit_count"] == 1
        assert fields["has_config"] is True
        assert spans[-1]["component"] == "genre"

    def test_genre_fallback_stamps_genre_tier(self, captured_watcher_events: list[dict]) -> None:
        pack = _make_pack(genre_inventory=_inv(currency="gold"), worlds={"w": None})
        resolve_inventory(pack, "w")
        spans = [
            e for e in captured_watcher_events if e["fields"].get("field") == "resolved_inventory"
        ]
        assert spans and spans[-1]["fields"]["tier"] == "genre"


class TestLoadoutReadsResolvedWorldInventory:
    """Wiring: resolve_inventory → apply_starting_loadout populates from world tier.

    This is the regression guard. Before the fix, the loadout read
    ``pack.inventory`` (genre tier = None for migrated packs) and added zero
    items. Now it must read the world-tier inventory and populate the kit.
    """

    def _character(self, char_class: str) -> Character:
        core = CreatureCore(
            name="Tester",
            description="d",
            personality="p",
            level=1,
            xp=0,
            hp=HpPool(current=10, max=10, base_max=10),
        )
        return Character(core=core, backstory="b", char_class=char_class, race="Human")

    def test_world_tier_loadout_populates_when_genre_is_none(self) -> None:
        # Genre tier ships NO inventory (the migrated-pack shape); the world tier
        # carries the class-keyed kit — exactly coyote_star / evropi after the
        # epic-94 relocation.
        pack = _make_pack(
            genre_inventory=None,
            worlds={
                "coyote_star": _inv(
                    catalog=["blaster_sidearm", "datapad"],
                    starting_equipment={"Pilot": ["blaster_sidearm", "datapad"]},
                    starting_gold={"Pilot": 500},
                )
            },
        )
        char = self._character("Pilot")

        resolved = resolve_inventory(pack, "coyote_star")
        items_added, gold_added = apply_starting_loadout(char, resolved, world="coyote_star")

        assert items_added == 2, "world-tier loadout must populate the kit"
        assert gold_added == 500
        names = {i["id"] for i in char.core.inventory.items}
        assert names == {"blaster_sidearm", "datapad"}

    def test_genre_none_without_resolver_is_empty_baseline(self) -> None:
        """Document the bug shape: feeding the genre-tier None no-ops the loadout.

        This is what the production code did before the repoint — proves the
        regression was real and that resolve_inventory is the fix, not cosmetics.
        """
        pack = _make_pack(
            genre_inventory=None,
            worlds={"coyote_star": _inv(starting_equipment={"Pilot": ["blaster_sidearm"]})},
        )
        char = self._character("Pilot")

        # Reading the genre tier directly (the OLD behaviour) adds nothing.
        items_added, _ = apply_starting_loadout(char, pack.inventory, world="coyote_star")
        assert items_added == 0
        assert char.core.inventory.items == []
