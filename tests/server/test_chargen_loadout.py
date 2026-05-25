"""Tests for ``sidequest.server.dispatch.chargen_loadout.apply_starting_loadout``.

Covers:
- Class-specific equipment is appended, gold is added.
- Case-insensitive class lookup (pack uses "Delver", character has "delver").
- Items not in ``item_catalog`` fall through to the minimal branch — still
  honored, never silently dropped.
- Pack with no ``inventory`` config is a no-op.
- Character class that isn't in ``starting_equipment`` is a no-op (no
  items added, no gold added) — Rust parity (find returns None).
- Builder-side item_hints already on inventory.items are preserved;
  loadout appends rather than replaces.
- Story 45-12: identity-aware dedup of ``starting_equipment[class]``
  against items already on ``character.core.inventory.items`` (typically
  builder-side ``item_hint`` rolls from ``equipment_tables``). The bug
  evidence (Playtest 3, Blutka save) was a 24-item starting kit where
  the catalogue specifies 13 — two extractors, both legitimately wired,
  both writing without identity check.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.genre.models.inventory import (
    CatalogItem,
    InventoryConfig,
)
from sidequest.server.dispatch.chargen_loadout import apply_starting_loadout


def _make_character(char_class: str = "Delver") -> Character:
    hp = HpPool(current=20, max=20, base_max=20)
    core = CreatureCore(
        name="Rux",
        description="A seasoned delver",
        personality="Curious",
        level=1,
        xp=0,
        hp=hp,
    )
    return Character(
        core=core,
        backstory="An orphan of the Reach.",
        char_class=char_class,
        race="Gnome",
    )


def _basic_catalog() -> list[CatalogItem]:
    return [
        CatalogItem(
            id="rusted_lantern",
            name="Rusted Lantern",
            description="Throws a weak amber glow.",
            category="tool",
            value=3,
            weight=1.5,
            rarity="common",
            tags=["light"],
        ),
        CatalogItem(
            id="short_rope",
            name="Short Rope",
            description="Ten feet of hemp.",
            category="tool",
            value=1,
            weight=2.0,
            rarity="",  # blank rarity → loadout fills in "common"
            tags=["climbing"],
            resource_ticks=3,
        ),
    ]


def test_class_equipment_and_gold_appended() -> None:
    char = _make_character("Delver")
    config = InventoryConfig(
        item_catalog=_basic_catalog(),
        starting_equipment={"Delver": ["rusted_lantern", "short_rope"]},
        starting_gold={"Delver": 7},
    )

    items_added, gold_added = apply_starting_loadout(char, config)

    assert items_added == 2
    assert gold_added == 7
    assert char.core.inventory.gold == 7
    assert [i["id"] for i in char.core.inventory.items] == [
        "rusted_lantern",
        "short_rope",
    ]
    lantern = char.core.inventory.items[0]
    assert lantern["name"] == "Rusted Lantern"
    assert lantern["narrative_weight"] == 0.3
    assert lantern["equipped"] is False
    assert lantern["quantity"] == 1
    assert lantern["state"] == "Carried"

    rope = char.core.inventory.items[1]
    assert rope["rarity"] == "common", "blank catalog rarity must default to 'common'"
    assert rope["uses_remaining"] == 3


def test_class_match_is_case_insensitive() -> None:
    char = _make_character("delver")  # character stores class lowercased
    config = InventoryConfig(
        item_catalog=_basic_catalog(),
        starting_equipment={"Delver": ["rusted_lantern"]},
        starting_gold={"Delver": 5},
    )

    items_added, gold_added = apply_starting_loadout(char, config)

    assert items_added == 1
    assert gold_added == 5


def test_item_not_in_catalog_uses_minimal_fallback() -> None:
    char = _make_character("Delver")
    config = InventoryConfig(
        item_catalog=_basic_catalog(),
        # mystery_token is NOT in the catalog — must still appear as an
        # inventory entry, never silently dropped.
        starting_equipment={"Delver": ["mystery_token"]},
        starting_gold={"Delver": 0},
    )

    items_added, _ = apply_starting_loadout(char, config)

    assert items_added == 1
    entry = char.core.inventory.items[0]
    assert entry["id"] == "mystery_token"
    assert entry["name"] == "mystery token"  # underscores → spaces
    assert entry["description"] == "Starting equipment"
    assert entry["rarity"] == "common"
    assert entry["narrative_weight"] == 0.2
    assert entry["tags"] == []


def test_no_inventory_config_is_noop() -> None:
    char = _make_character("Delver")
    items_added, gold_added = apply_starting_loadout(char, None)

    assert items_added == 0
    assert gold_added == 0
    assert char.core.inventory.items == []
    assert char.core.inventory.gold == 0


def test_unknown_class_is_noop() -> None:
    char = _make_character("Philosopher")  # class not present in the pack
    config = InventoryConfig(
        item_catalog=_basic_catalog(),
        starting_equipment={"Delver": ["rusted_lantern"]},
        starting_gold={"Delver": 3},
    )

    items_added, gold_added = apply_starting_loadout(char, config)

    assert items_added == 0
    assert gold_added == 0
    assert char.core.inventory.items == []
    assert char.core.inventory.gold == 0


def test_builder_item_hints_are_preserved() -> None:
    char = _make_character("Delver")
    # Simulate a builder-side item_hint already on the inventory.
    char.core.inventory.items.append(
        {
            "id": "family_charm",
            "name": "Family Charm",
            "description": "Given by a grandmother long gone.",
            "category": "trinket",
            "value": 0,
            "weight": 0.1,
            "rarity": "common",
            "narrative_weight": 0.5,
            "tags": ["sentimental"],
            "equipped": False,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        }
    )
    config = InventoryConfig(
        item_catalog=_basic_catalog(),
        starting_equipment={"Delver": ["rusted_lantern"]},
        starting_gold={"Delver": 2},
    )

    apply_starting_loadout(char, config)

    ids = [i["id"] for i in char.core.inventory.items]
    assert ids == ["family_charm", "rusted_lantern"], (
        "loadout must append to existing items, not replace them"
    )


def test_builder_item_hint_upgraded_from_catalog() -> None:
    """Builder stub (category=weapon, boilerplate description) is rewritten
    from the catalog when the id matches."""
    char = _make_character("Delver")
    # Simulate what CharacterBuilder emits for a scene item_hint — bogus
    # category "weapon" + "Starting equipment:" boilerplate description.
    char.core.inventory.items.append(
        {
            "id": "rusted_lantern",
            "name": "Rusted Lantern",
            "description": "Starting equipment: Rusted Lantern",
            "category": "weapon",
            "value": 10,
            "weight": 3.0,
            "rarity": "common",
            "narrative_weight": 0.3,
            "tags": [],
            "equipped": True,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        }
    )
    config = InventoryConfig(
        item_catalog=_basic_catalog(),
        starting_equipment={"Delver": []},
        starting_gold={"Delver": 0},
    )

    apply_starting_loadout(char, config)

    upgraded = char.core.inventory.items[0]
    assert upgraded["category"] == "tool", "catalog category must win over stub"
    assert upgraded["description"] == "Throws a weak amber glow."
    assert upgraded["tags"] == ["light"]
    assert upgraded["equipped"] is True, (
        "builder-set equipped flag must be preserved through upgrade"
    )
    assert upgraded["quantity"] == 1


def test_hint_upgrade_skipped_when_id_not_in_catalog() -> None:
    """Unknown item_hint ids keep their builder metadata — no silent drop."""
    char = _make_character("Delver")
    char.core.inventory.items.append(
        {
            "id": "unknown_trinket",
            "name": "Unknown Trinket",
            "description": "Starting equipment: Unknown Trinket",
            "category": "weapon",
            "value": 10,
            "weight": 3.0,
            "rarity": "common",
            "narrative_weight": 0.3,
            "tags": [],
            "equipped": True,
            "quantity": 1,
            "uses_remaining": None,
            "state": "Carried",
        }
    )
    config = InventoryConfig(
        item_catalog=_basic_catalog(),
        starting_equipment={"Delver": []},
        starting_gold={"Delver": 0},
    )

    apply_starting_loadout(char, config)

    # Item still present, unchanged — we don't silently drop it.
    assert [i["id"] for i in char.core.inventory.items] == ["unknown_trinket"]
    assert char.core.inventory.items[0]["category"] == "weapon"


# ---------------------------------------------------------------------------
# Story 45-12: starting-kit dedup
# ---------------------------------------------------------------------------


def _builder_hint_dict(item_id: str, name: str) -> dict:
    """Mirror the stub-form item dict the ``CharacterBuilder`` emits when
    rolling on ``equipment_tables`` (builder.py:1407–1422)."""
    return {
        "id": item_id,
        "name": name,
        "description": f"Starting equipment: {name}",
        "category": "weapon",
        "value": 10,
        "weight": 3.0,
        "rarity": "common",
        "narrative_weight": 0.3,
        "tags": [],
        "equipped": False,
        "quantity": 1,
        "uses_remaining": None,
        "state": "Carried",
    }


def _blutka_catalog() -> list[CatalogItem]:
    """Catalog covering the Blutka 11-overlap regression fixture."""
    rows = [
        ("torch", "Torch", "tool", 2, 1.0, ["light"]),
        ("rations_day", "Day Rations", "consumable", 5, 1.0, ["food"]),
        ("waterskin", "Waterskin", "tool", 1, 2.0, []),
        ("chalk", "Chalk", "tool", 1, 0.1, ["mark"]),
        ("ten_foot_pole", "Ten-Foot Pole", "tool", 2, 5.0, []),
        ("rope_hemp", "Hemp Rope", "tool", 2, 4.0, ["climbing"]),
        ("dagger_iron", "Iron Dagger", "weapon", 8, 1.0, ["weapon"]),
        ("iron_spikes", "Iron Spikes", "tool", 1, 0.5, ["climbing"]),
    ]
    return [
        CatalogItem(
            id=cid,
            name=name,
            description=f"{name}.",
            category=cat,
            value=val,
            weight=wt,
            rarity="common",
            tags=tags,
        )
        for cid, name, cat, val, wt, tags in rows
    ]


class TestStartingKitDedup:
    """AC1–AC4: identity-aware dedup of ``starting_equipment[class]``
    against items already on ``character.core.inventory.items``.

    Dedup keys: ``id`` (case-insensitive) + ``name`` (case-insensitive)
    fallback. Final inventory reflects the union, not the sum.
    """

    def test_partial_overlap_yields_union_blutka_regression(self) -> None:
        """AC1: builder-emitted stub items + catalogue items with
        overlapping ids → catalogue duplicates are skipped and only the
        disjoint catalogue ids land. The builder side is preserved as-is
        (the story's Out-of-Scope explicitly defers multi-quantity stack
        consolidation: "If the builder produced 6 torches as separate
        items, the dedup will collapse the catalogue's 1 torch against
        any of them — net result is the catalogue copy is dropped").

        This is the canonical Playtest 3 evidence: Blutka shipped with 24
        items because both batches were appended without identity check.
        The fix blocks the catalogue's overlap; intra-batch builder
        duplicates stay until a follow-up stack-consolidation story."""
        char = _make_character("Adventurer")
        # Builder-side stubs matching the playtest evidence breakdown
        # (torch ×3, rations ×2, waterskin ×2, chalk ×2, pole ×2 = 11
        # entries, 5 unique ids). Quantity-duplication on the builder
        # side is exactly what the story carves out as future work.
        for stub_id, stub_name in [
            ("torch", "Torch"),
            ("torch", "Torch"),
            ("torch", "Torch"),
            ("rations_day", "Day Rations"),
            ("rations_day", "Day Rations"),
            ("waterskin", "Waterskin"),
            ("waterskin", "Waterskin"),
            ("chalk", "Chalk"),
            ("chalk", "Chalk"),
            ("ten_foot_pole", "Ten-Foot Pole"),
            ("ten_foot_pole", "Ten-Foot Pole"),
        ]:
            char.core.inventory.items.append(_builder_hint_dict(stub_id, stub_name))

        config = InventoryConfig(
            item_catalog=_blutka_catalog(),
            starting_equipment={
                "Adventurer": [
                    "torch",
                    "rations_day",
                    "waterskin",
                    "chalk",
                    "ten_foot_pole",
                    "rope_hemp",
                    "dagger_iron",
                    "iron_spikes",
                ]
            },
            starting_gold={"Adventurer": 0},
        )

        apply_starting_loadout(char, config)

        items = char.core.inventory.items
        ids = [i.get("id") for i in items]

        # Pre-fix: 11 builder + 8 catalogue = 19 items, with overlapping
        # catalogue dupes (torch, rations_day, waterskin, chalk, pole)
        # appended a second time. Post-fix: 11 builder + 3 disjoint
        # catalogue = 14 items.
        assert len(items) == 14, (
            f"Catalogue overlap not collapsed — expected 14 items "
            f"(11 builder + 3 disjoint catalogue: rope_hemp, "
            f"dagger_iron, iron_spikes). Got {len(items)}: {ids!r}."
        )

        # The 5 catalogue ids that overlap with the builder MUST appear
        # exactly once each (the builder copy survives; the catalogue
        # copy is skipped). Count occurrences in the post-state by id.
        id_counts = {iid: ids.count(iid) for iid in set(ids) if iid}
        # Builder duplicates remain — those are out of scope.
        assert id_counts.get("torch") == 3
        assert id_counts.get("rations_day") == 2
        assert id_counts.get("waterskin") == 2
        assert id_counts.get("chalk") == 2
        assert id_counts.get("ten_foot_pole") == 2
        # Catalogue-side disjoint ids landed exactly once each.
        assert id_counts.get("rope_hemp") == 1
        assert id_counts.get("dagger_iron") == 1
        assert id_counts.get("iron_spikes") == 1
        # The pre-fix witness would show torch×6 (3 builder + 3 catalogue),
        # rations_day×4, etc. The post-fix counts above prove the
        # catalogue copies were skipped.

    def test_disjoint_case_appends_all(self) -> None:
        """AC2: builder-side and catalogue ids are fully disjoint →
        post-state count is the sum of both. Regression guard against
        over-eager dedup eating legitimate items."""
        char = _make_character("Delver")
        # Builder side: family_charm, mystery_compass.
        char.core.inventory.items.append(_builder_hint_dict("family_charm", "Family Charm"))
        char.core.inventory.items.append(_builder_hint_dict("mystery_compass", "Mystery Compass"))

        config = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern", "short_rope"]},
            starting_gold={"Delver": 0},
        )

        apply_starting_loadout(char, config)

        ids = [i.get("id") for i in char.core.inventory.items]
        assert ids == [
            "family_charm",
            "mystery_compass",
            "rusted_lantern",
            "short_rope",
        ], (
            "Disjoint case must append all catalogue items — over-eager "
            f"dedup is dropping legitimate items. Got: {ids!r}"
        )
        assert len(char.core.inventory.items) == 4

    def test_full_overlap_skips_all(self) -> None:
        """AC3: every id in ``starting_equipment[class]`` is already
        present from the builder side → ``items_added == 0``."""
        char = _make_character("Delver")
        # Builder pre-populates with both catalogue ids.
        char.core.inventory.items.append(_builder_hint_dict("rusted_lantern", "Rusted Lantern"))
        char.core.inventory.items.append(_builder_hint_dict("short_rope", "Short Rope"))

        config = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern", "short_rope"]},
            starting_gold={"Delver": 0},
        )

        items_added, _ = apply_starting_loadout(char, config)

        assert items_added == 0, (
            f"Full-overlap case must add zero items — every id already "
            f"present. Got items_added={items_added}."
        )
        # Final state: only the two builder items remain (upgraded in
        # place by ``_upgrade_hint_items_from_catalog`` if id matches).
        assert len(char.core.inventory.items) == 2

    def test_name_fallback_collision_id_differs(self) -> None:
        """AC4: builder emits ``id="torch_1", name="Torch"``; catalogue
        emits ``id="torch", name="Torch"``. Dedup detects the collision
        via name and skips the catalogue entry."""
        char = _make_character("Adventurer")
        # Builder ID is suffixed (slot index variant) but the display
        # name matches the catalogue exactly.
        char.core.inventory.items.append(_builder_hint_dict("torch_1", "Torch"))

        config = InventoryConfig(
            item_catalog=_blutka_catalog(),
            starting_equipment={"Adventurer": ["torch"]},
            starting_gold={"Adventurer": 0},
        )

        items_added, _ = apply_starting_loadout(char, config)

        ids = [i.get("id") for i in char.core.inventory.items]
        names = [i.get("name") for i in char.core.inventory.items]
        assert items_added == 0, (
            f"Name-collision dedup failed — catalogue 'torch' should be "
            f"skipped because the builder already shipped a Torch under a "
            f"different id. ids={ids!r}, items_added={items_added}."
        )
        assert names == ["Torch"]

    def test_name_dedup_is_case_insensitive(self) -> None:
        """Dedup keys must be case-insensitive on BOTH id and name —
        the builder's ``hint.lower().replace(" ", "_")`` id-shape and
        the catalogue's id may differ only in case for some packs."""
        char = _make_character("Adventurer")
        # Builder side: capitalized id and name.
        char.core.inventory.items.append(_builder_hint_dict("TORCH", "TORCH"))

        config = InventoryConfig(
            item_catalog=_blutka_catalog(),
            starting_equipment={"Adventurer": ["torch"]},
            starting_gold={"Adventurer": 0},
        )

        items_added, _ = apply_starting_loadout(char, config)

        assert items_added == 0, (
            "Case-insensitive id/name dedup failed. Catalogue 'torch' "
            "must be skipped because builder already shipped 'TORCH'."
        )

    def test_intra_batch_dedup_collapses_pack_duplicates(self) -> None:
        """The actual ``caverns_and_claudes/grimvault`` pack lists
        ``starting_equipment[Delver]`` with 3 torches and 2 rations. The
        dedup pass MUST collapse these intra-list duplicates so the
        persisted kit has unique ids — the same invariant catches
        intra-batch and inter-batch duplication."""
        char = _make_character("Delver")
        config = InventoryConfig(
            item_catalog=_blutka_catalog(),
            starting_equipment={
                "Delver": [
                    "torch",
                    "torch",
                    "torch",
                    "rations_day",
                    "rations_day",
                    "waterskin",
                ]
            },
            starting_gold={"Delver": 0},
        )

        apply_starting_loadout(char, config)

        ids = [i.get("id") for i in char.core.inventory.items]
        assert ids == ["torch", "rations_day", "waterskin"], (
            f"Intra-list duplicates not collapsed — final ids: {ids!r}. "
            f"Pack-side typos (3 torches in starting_equipment) shipped "
            f"to player; dedup must guard against this."
        )

    def test_pack_with_no_inventory_still_returns_zero_zero(self) -> None:
        """Negative path: ``inventory_config=None`` must short-circuit to
        ``(0, 0)`` exactly as it did pre-fix. The dedup pass is in the
        non-None branch only."""
        char = _make_character("Delver")
        items_added, gold_added = apply_starting_loadout(char, None)

        assert items_added == 0
        assert gold_added == 0


# ---------------------------------------------------------------------------
# Story 45-12: OTEL spans
# ---------------------------------------------------------------------------


class TestStartingKitDedupSpans:
    """AC5: ``chargen.starting_kit_dedup_evaluated`` fires on every call
    (Sebastien's negative-confirmation requirement per CLAUDE.md OTEL
    Observability Principle); ``chargen.starting_kit_dedup_fired`` fires
    only when ``skipped_count > 0``.
    """

    @staticmethod
    def _spans_named(otel_capture, name: str) -> list:
        return [s for s in otel_capture.get_finished_spans() if s.name == name]

    def test_evaluated_span_fires_on_disjoint_path(self, otel_capture) -> None:
        char = _make_character("Delver")
        config = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern", "short_rope"]},
            starting_gold={"Delver": 0},
        )

        apply_starting_loadout(char, config, genre="cnc", world="grimvault", player_id="pid")

        evaluated = self._spans_named(otel_capture, "chargen.starting_kit_dedup_evaluated")
        assert len(evaluated) == 1, (
            "chargen.starting_kit_dedup_evaluated MUST fire on every "
            "apply_starting_loadout call (negative-confirmation per "
            f"CLAUDE.md OTEL principle). Got {len(evaluated)} fires."
        )

    def test_fired_span_does_not_fire_on_disjoint_path(self, otel_capture) -> None:
        """Negative confirmation: dedup_fired MUST NOT fire when there
        are no skips — a half-fix that always emits the fire span breaks
        the GM-panel signal-to-noise ratio (sibling-shape with the
        scrapbook-coverage gap span)."""
        char = _make_character("Delver")
        config = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern", "short_rope"]},
            starting_gold={"Delver": 0},
        )

        apply_starting_loadout(char, config)

        fired = self._spans_named(otel_capture, "chargen.starting_kit_dedup_fired")
        assert fired == [], (
            f"chargen.starting_kit_dedup_fired MUST NOT fire on a "
            f"disjoint pack — got {len(fired)} fires. Sebastien's GM "
            f"panel would cry wolf and the alerting goes numb."
        )

    def test_fired_span_fires_on_full_overlap(self, otel_capture) -> None:
        char = _make_character("Delver")
        char.core.inventory.items.append(_builder_hint_dict("rusted_lantern", "Rusted Lantern"))
        char.core.inventory.items.append(_builder_hint_dict("short_rope", "Short Rope"))
        config = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern", "short_rope"]},
            starting_gold={"Delver": 0},
        )

        apply_starting_loadout(char, config)

        evaluated = self._spans_named(otel_capture, "chargen.starting_kit_dedup_evaluated")
        fired = self._spans_named(otel_capture, "chargen.starting_kit_dedup_fired")
        assert len(evaluated) == 1
        assert len(fired) == 1, (
            "Full overlap → fired span must fire exactly once (with "
            f"skipped_ids covering both ids). Got {len(fired)}."
        )

    def test_fired_span_fires_on_partial_overlap(self, otel_capture) -> None:
        char = _make_character("Adventurer")
        char.core.inventory.items.append(_builder_hint_dict("torch", "Torch"))
        config = InventoryConfig(
            item_catalog=_blutka_catalog(),
            starting_equipment={"Adventurer": ["torch", "rope_hemp"]},
            starting_gold={"Adventurer": 0},
        )

        apply_starting_loadout(char, config)

        fired = self._spans_named(otel_capture, "chargen.starting_kit_dedup_fired")
        assert len(fired) == 1

    def test_evaluated_span_attributes_carry_full_set(self, otel_capture) -> None:
        """Span attributes contract: ``class_name``, ``pre_dedup_count``,
        ``equipment_ids_count``, ``skipped_count``, ``items_added``,
        ``items_upgraded``, ``final_count``, ``genre``, ``world``,
        ``player_id``. The GM-panel renderer reads these verbatim — a
        payload-shape regression here quietly breaks the dashboard."""
        char = _make_character("Adventurer")
        char.core.inventory.items.append(_builder_hint_dict("torch", "Torch"))
        config = InventoryConfig(
            item_catalog=_blutka_catalog(),
            starting_equipment={"Adventurer": ["torch", "rope_hemp"]},
            starting_gold={"Adventurer": 0},
        )

        apply_starting_loadout(char, config, genre="cnc", world="grimvault", player_id="pid-7")

        evaluated = self._spans_named(otel_capture, "chargen.starting_kit_dedup_evaluated")
        assert len(evaluated) == 1
        attrs = dict(evaluated[0].attributes or {})

        # Required keys — fail loudly on missing keys so the dashboard
        # doesn't silently miss a column.
        required = {
            "class_name",
            "pre_dedup_count",
            "equipment_ids_count",
            "skipped_count",
            "items_added",
            "items_upgraded",
            "final_count",
            "genre",
            "world",
            "player_id",
        }
        missing = required - set(attrs.keys())
        assert not missing, (
            f"chargen.starting_kit_dedup_evaluated missing required "
            f"attributes: {sorted(missing)}. Full attrs={sorted(attrs)}."
        )

        # Spot-check semantic correctness.
        assert attrs["class_name"] == "Adventurer"
        assert attrs["pre_dedup_count"] == 1, (
            "pre_dedup_count must reflect items already on the inventory "
            f"BEFORE the dedup pass. Got {attrs['pre_dedup_count']}."
        )
        assert attrs["equipment_ids_count"] == 2
        assert attrs["skipped_count"] == 1, "torch was skipped (already present)"
        assert attrs["items_added"] == 1, "rope_hemp landed"
        # final_count = pre_dedup_count + items_added (no quantity merging).
        assert attrs["final_count"] == 2
        assert attrs["genre"] == "cnc"
        assert attrs["world"] == "grimvault"
        assert attrs["player_id"] == "pid-7"

    def test_fired_span_carries_skipped_ids_payload(self, otel_capture) -> None:
        """``chargen.starting_kit_dedup_fired`` MUST carry the
        ``skipped_ids`` list verbatim — the GM panel renders this list
        to show the player which catalogue ids were collapsed."""
        char = _make_character("Adventurer")
        char.core.inventory.items.append(_builder_hint_dict("torch", "Torch"))
        char.core.inventory.items.append(_builder_hint_dict("rations_day", "Day Rations"))
        config = InventoryConfig(
            item_catalog=_blutka_catalog(),
            starting_equipment={"Adventurer": ["torch", "rations_day", "rope_hemp"]},
            starting_gold={"Adventurer": 0},
        )

        apply_starting_loadout(char, config)

        fired = self._spans_named(otel_capture, "chargen.starting_kit_dedup_fired")
        assert len(fired) == 1
        attrs = dict(fired[0].attributes or {})
        skipped_ids = attrs.get("skipped_ids")
        assert skipped_ids is not None, (
            "skipped_ids attribute is the load-bearing payload — without "
            "it the GM panel can't render which ids were collapsed."
        )
        # OTEL stringifies sequence attributes; accept any repr that
        # names both skipped ids.
        skipped_str = str(skipped_ids)
        assert "torch" in skipped_str and "rations_day" in skipped_str, (
            f"skipped_ids must list both collapsed ids. Got: {skipped_ids!r}"
        )
        assert attrs.get("skipped_count") == 2

    def test_three_chargen_runs_evaluated_three_fired_two(self, otel_capture) -> None:
        """AC5 explicit: 3 ``apply_starting_loadout`` calls (disjoint,
        full-overlap, partial) → ``evaluated`` fires 3×, ``fired``
        fires 2× (full + partial), 0× on disjoint."""
        # Run 1: disjoint
        c1 = _make_character("Delver")
        cfg1 = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern"]},
            starting_gold={"Delver": 0},
        )
        apply_starting_loadout(c1, cfg1)

        # Run 2: full overlap
        c2 = _make_character("Delver")
        c2.core.inventory.items.append(_builder_hint_dict("rusted_lantern", "Rusted Lantern"))
        cfg2 = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern"]},
            starting_gold={"Delver": 0},
        )
        apply_starting_loadout(c2, cfg2)

        # Run 3: partial overlap
        c3 = _make_character("Delver")
        c3.core.inventory.items.append(_builder_hint_dict("rusted_lantern", "Rusted Lantern"))
        cfg3 = InventoryConfig(
            item_catalog=_basic_catalog(),
            starting_equipment={"Delver": ["rusted_lantern", "short_rope"]},
            starting_gold={"Delver": 0},
        )
        apply_starting_loadout(c3, cfg3)

        evaluated = self._spans_named(otel_capture, "chargen.starting_kit_dedup_evaluated")
        fired = self._spans_named(otel_capture, "chargen.starting_kit_dedup_fired")
        assert len(evaluated) == 3, (
            f"evaluated MUST fire once per call (3 runs → 3 spans). Got {len(evaluated)}."
        )
        assert len(fired) == 2, (
            f"fired MUST fire on full-overlap + partial-overlap only "
            f"(2 of 3 runs). Got {len(fired)}."
        )

    def test_evaluated_span_fires_when_inventory_config_is_none(self, otel_capture) -> None:
        """The negative-confirmation path: even when the pack has no
        inventory config, the dedup-pass evaluation MUST still emit so
        the GM panel knows the path was checked. Skipping this branch
        is the lie-detector blind spot CLAUDE.md calls out."""
        char = _make_character("Delver")
        apply_starting_loadout(char, None)

        evaluated = self._spans_named(otel_capture, "chargen.starting_kit_dedup_evaluated")
        assert len(evaluated) == 1, (
            "Even on the no-op (inventory_config=None) path, the "
            "evaluated span MUST fire once with equipment_ids_count=0 "
            "so Sebastien gets negative-confirmation."
        )
        attrs = dict(evaluated[0].attributes or {})
        assert attrs.get("equipment_ids_count") == 0
        assert attrs.get("items_added") == 0
        assert attrs.get("skipped_count") == 0


# ---------------------------------------------------------------------------
# Story 45-12: SPAN_ROUTES registration (CLAUDE.md OTEL discipline)
# ---------------------------------------------------------------------------


class TestStartingKitDedupSpanRouting:
    """The dedup spans MUST be registered in ``SPAN_ROUTES`` so the
    watcher hub picks them up — without the route the spans fire into a
    void and the GM panel never sees them.

    The static lint at ``tests/telemetry/test_routing_completeness.py``
    requires every ``SPAN_*`` constant on ``sidequest.telemetry.spans``
    to be in either ``SPAN_ROUTES`` or ``FLAT_ONLY_SPANS`` — these tests
    pin the routing decision (routed, not flat-only) for both spans.
    """

    def test_evaluated_span_constant_exported(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED,
        )

        assert (
            SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED == "chargen.starting_kit_dedup_evaluated"
        ), (
            "Span constant must equal the documented name; the GM panel "
            "filters on this exact string."
        )

    def test_fired_span_constant_exported(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED,
        )

        assert SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED == "chargen.starting_kit_dedup_fired"

    def test_evaluated_span_registered_in_routes(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED,
            SPAN_ROUTES,
        )

        assert SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED in SPAN_ROUTES, (
            "Without an entry in SPAN_ROUTES the watcher hub sees "
            "agent_span_close only and the typed dedup-evaluated event "
            "never reaches the GM panel — silent failure mode."
        )
        route = SPAN_ROUTES[SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED]
        # Route must extract the load-bearing fields the GM panel renders.
        sample = type(
            "FakeSpan",
            (),
            {
                "name": SPAN_CHARGEN_STARTING_KIT_DEDUP_EVALUATED,
                "attributes": {
                    "class_name": "Delver",
                    "pre_dedup_count": 11,
                    "equipment_ids_count": 13,
                    "skipped_count": 11,
                    "items_added": 2,
                    "items_upgraded": 0,
                    "final_count": 13,
                    "genre": "cnc",
                    "world": "grimvault",
                    "player_id": "pid",
                },
            },
        )()
        extracted = route.extract(sample)
        for key in (
            "class_name",
            "pre_dedup_count",
            "equipment_ids_count",
            "skipped_count",
            "items_added",
            "final_count",
            "genre",
            "world",
            "player_id",
        ):
            assert key in extracted, (
                f"SpanRoute.extract for evaluated span dropped {key!r}; "
                f"GM-panel column will be empty."
            )

    def test_fired_span_registered_in_routes(self) -> None:
        from sidequest.telemetry.spans import (
            SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED,
            SPAN_ROUTES,
        )

        assert SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED in SPAN_ROUTES
        route = SPAN_ROUTES[SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED]
        sample = type(
            "FakeSpan",
            (),
            {
                "name": SPAN_CHARGEN_STARTING_KIT_DEDUP_FIRED,
                "attributes": {
                    "class_name": "Delver",
                    "skipped_count": 2,
                    "skipped_ids": ["torch", "rations_day"],
                    "items_added": 1,
                    "final_count": 3,
                    "genre": "cnc",
                    "world": "grimvault",
                    "player_id": "pid",
                },
            },
        )()
        extracted = route.extract(sample)
        # ``skipped_ids`` is the unique payload of fired vs evaluated —
        # without it the GM panel can't render which ids were collapsed.
        assert "skipped_ids" in extracted, (
            "SpanRoute.extract for fired span MUST surface skipped_ids "
            "— that's the load-bearing payload."
        )

    def test_routing_completeness_lint_still_passes(self) -> None:
        """Meta-check: the new constants MUST satisfy the routing
        completeness lint so we don't regress the broader rule.

        Adding a SPAN_* constant without registering it in SPAN_ROUTES
        or FLAT_ONLY_SPANS is the failure shape this guards against."""
        from sidequest.telemetry import spans as spans_pkg
        from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES

        all_spans = {
            v
            for name, v in vars(spans_pkg).items()
            if name.startswith("SPAN_") and isinstance(v, str)
        }
        missing = all_spans - set(SPAN_ROUTES.keys()) - set(FLAT_ONLY_SPANS)
        assert not missing, (
            f"Spans without a routing decision: {sorted(missing)}. Add "
            f"to SPAN_ROUTES (preferred) or FLAT_ONLY_SPANS."
        )
