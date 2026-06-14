"""RED tests for the ADR-145 D3 union-by-id, per-field inventory catalog merge.

Story 114-11. The schema delta (D2 — ``ItemProvenance`` + TL/range/magazine) is
already landed; this story changes the genre↔world *resolution* of ``item_catalog``
from today's wholesale REPLACE (``inventory_resolve.resolve_inventory``) to a
**union-by-id, per-field** merge in which the genre baseline is non-droppable.

Contract pinned here (ADR-145 D1/D3/D4 + the project OTEL principle):

  R1. Genre baseline is non-droppable. A baseline ``item_catalog`` entry survives
      into the resolved catalog even when the world ships its own inventory.yaml.
      (The ``power_glove`` regression: today's REPLACE drops the whole baseline.)
  R2. Union by ``id``. resolved = genre baseline ∪ world items, keyed by id.
      World-only ids added; genre-only ids retained; shared ids merge per-field.
  R3. Per-field merge on a shared id: mechanical fields inherit+lock from the
      genre baseline; presentation fields (name/description/lore/narrative_weight)
      take the world override when present. Provenance is preserved from baseline
      (a reskin is still the SRD item, mode stays verbatim).
  R4. Verbatim field-lock validator (fail loud): a world override that CHANGES a
      mechanical field of a mode=verbatim baseline item is a hard error naming the
      item id and the offending field. Presentation-only override succeeds.
  R5. PRESERVE world-replace for non-catalog fields: starting_equipment,
      starting_gold, currency still REPLACE wholesale per world — NOT unioned.
  R6. OTEL: the merge decision emits a state_transition span recording that a
      union merge happened and the counts (baseline / world-override / world-added).

Plus edges: world ships no inventory (pure baseline); genre has no baseline
(world catalog stands alone); bespoke baseline item NOT field-locked; brand-new
world bespoke id added cleanly.

Fixtures are synthetic ``InventoryConfig``/``CatalogItem`` built in-test — NO real
pack loading (project rule). Mirrors ``tests/server/test_inventory_resolve.py``.

ALL of these are expected to FAIL in RED: the merge helper, the validator, and the
union-merge OTEL span do not exist yet — ``resolve_inventory`` still wholesale
REPLACES the catalog.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from sidequest.genre.models.inventory import (
    CatalogItem,
    CurrencyConfig,
    DamageSpec,
    InventoryConfig,
    ItemProvenance,
)
from sidequest.genre.models.pack import GenrePack, World
from sidequest.server.dispatch.inventory_resolve import resolve_inventory


# ---------------------------------------------------------------------------
# OTEL capture — reuse the established watcher_hub.publish_event monkeypatch
# pattern from tests/server/test_inventory_resolve.py (do NOT invent a new harness).
# ---------------------------------------------------------------------------
@pytest.fixture
def captured_watcher_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    from sidequest.telemetry import watcher_hub

    monkeypatch.setattr(watcher_hub, "publish_event", _capture)
    yield captured


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _verbatim(srd: str = "wwn") -> ItemProvenance:
    """A mode=verbatim WN-free provenance (the baseline/SRD-sourced shape)."""
    return ItemProvenance(
        mode="verbatim", srd=srd, srd_ref=f"{srd.upper()} SRD §3.0.0", license="wn-free"
    )


def _bespoke() -> ItemProvenance:
    return ItemProvenance(mode="bespoke", srd=None, license="na")


def _weapon(
    item_id: str,
    *,
    name: str | None = None,
    description: str = "A bound weapon.",
    dice: str = "1d8",
    value: int = 10,
    weight: float = 3.0,
    lore: str = "",
    provenance: ItemProvenance | None = None,
    tech_level: int | None = None,
) -> CatalogItem:
    return CatalogItem(
        id=item_id,
        name=name or item_id.replace("_", " ").title(),
        description=description,
        category="weapon",
        value=value,
        weight=weight,
        lore=lore,
        damage=DamageSpec(dice=dice),
        tech_level=tech_level,
        provenance=provenance,
    )


def _inv(
    *,
    catalog: list[CatalogItem] | None = None,
    starting_equipment: dict[str, list[str]] | None = None,
    starting_gold: dict[str, int] | None = None,
    currency: str | None = None,
) -> InventoryConfig:
    return InventoryConfig(
        currency=CurrencyConfig(name=currency) if currency else None,
        item_catalog=catalog or [],
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


def _by_id(inv: InventoryConfig) -> dict[str, CatalogItem]:
    return {i.id: i for i in inv.item_catalog}


# ===========================================================================
# R1 + R2 — genre baseline non-droppable; union by id
# ===========================================================================
class TestBaselineNonDroppableUnion:
    def test_baseline_item_survives_when_world_ships_inventory(self) -> None:
        """R1 (the power_glove regression): a genre baseline item is NOT dropped
        merely because the world ships its own inventory.yaml. Today's REPLACE
        drops it; the union merge must retain it."""
        pack = _make_pack(
            genre_inventory=_inv(catalog=[_weapon("power_glove", provenance=_verbatim())]),
            worlds={
                "seaboard_of_saints": _inv(catalog=[_weapon("scrap_rifle", provenance=_bespoke())])
            },
        )
        resolved = resolve_inventory(pack, "seaboard_of_saints")
        assert resolved is not None
        ids = {i.id for i in resolved.item_catalog}
        assert "power_glove" in ids, "genre baseline item must survive a world inventory (R1)"

    def test_world_only_item_is_added(self) -> None:
        """R2: a world-only id is added to the union."""
        pack = _make_pack(
            genre_inventory=_inv(catalog=[_weapon("power_glove", provenance=_verbatim())]),
            worlds={
                "seaboard_of_saints": _inv(catalog=[_weapon("scrap_rifle", provenance=_bespoke())])
            },
        )
        resolved = resolve_inventory(pack, "seaboard_of_saints")
        assert resolved is not None
        ids = {i.id for i in resolved.item_catalog}
        assert ids == {"power_glove", "scrap_rifle"}, "resolved catalog = baseline ∪ world (R2)"

    def test_no_duplicate_ids_on_shared_id(self) -> None:
        """R2: a shared id appears exactly once in the resolved union (merged, not appended)."""
        pack = _make_pack(
            genre_inventory=_inv(catalog=[_weapon("longsword", provenance=_verbatim())]),
            worlds={
                "w": _inv(catalog=[_weapon("longsword", name="Vibroblade", provenance=_verbatim())])
            },
        )
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        ids = [i.id for i in resolved.item_catalog]
        assert ids.count("longsword") == 1, "shared id must merge to one entry, not duplicate (R2)"


# ===========================================================================
# R3 — per-field merge on a shared id (presentation reskin)
# ===========================================================================
class TestPerFieldReskin:
    def test_presentation_overrides_mechanics_inherit(self) -> None:
        """R3: world reskins name/description/lore; mechanical fields inherit from
        the baseline; provenance is preserved (still the SRD item)."""
        baseline = _weapon(
            "longsword",
            name="Sword, Long",
            description="A bound WWN longsword.",
            dice="1d8",
            value=15,
            weight=3.0,
            lore="Forged in the old smithies.",
            provenance=_verbatim(),
        )
        # World reskin: presentation ONLY. The world item OMITS the mechanical
        # fields (they sit at the model defaults: no damage, value=0, weight=0.0).
        # Under today's REPLACE this would lose the baseline's 1d8/15/3.0 — under
        # the per-field merge they are INHERITED from the baseline. This is the
        # discriminator that proves merge, not replace. (The world MUST NOT
        # re-stat a verbatim item — see R4 — so omitting is the correct authoring.)
        world_item = CatalogItem(
            id="longsword",
            name="Vibroblade",
            description="A humming energy blade.",
            category="weapon",
            lore="Standard issue aboard the cruiser.",
            provenance=_verbatim(),
        )
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        merged = _by_id(resolved)["longsword"]

        # Presentation = world override
        assert merged.name == "Vibroblade"
        assert merged.description == "A humming energy blade."
        assert merged.lore == "Standard issue aboard the cruiser."
        # Mechanics = baseline (inherited+locked)
        assert merged.damage is not None and merged.damage.dice == "1d8"
        assert merged.value == 15
        assert merged.weight == 3.0
        # Provenance preserved — a reskin is still the SRD item
        assert merged.provenance is not None
        assert merged.provenance.mode == "verbatim"
        assert merged.provenance.srd == "wwn"

    def test_genre_only_item_passes_through_unchanged(self) -> None:
        """R2/R3: a genre-only id the world never mentions is retained verbatim."""
        baseline = _weapon("dagger", dice="1d4", provenance=_verbatim())
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline, _weapon("longsword", provenance=_verbatim())]),
            worlds={
                "w": _inv(catalog=[_weapon("longsword", name="Vibroblade", provenance=_verbatim())])
            },
        )
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        dagger = _by_id(resolved)["dagger"]
        assert dagger.name == "Dagger"
        assert dagger.damage is not None and dagger.damage.dice == "1d4"


# ===========================================================================
# R4 — verbatim field-lock validator (fail loud)
# ===========================================================================
# Every mechanical field, with a baseline value and a differing world value. Each
# row is a discriminator that the corresponding field is in _MECHANICAL_FIELDS and
# therefore lock-tested — a truncated tuple drops a row and fails this test.
_MECHANICAL_LOCK_CASES = [
    ("category", "weapon", "tool"),
    ("damage", DamageSpec(dice="1d8"), DamageSpec(dice="2d6")),
    ("armor_class", 12, 16),
    ("mitigation", 1, 5),
    ("tech_level", 3, 5),
    ("range_band", "pistol", "rifle"),
    ("magazine", 6, 30),
    ("weight", 3.0, 9.0),
    ("value", 15, 999),
    ("resource_ticks", 2, 8),
    ("heal_amount", "1d6", "3d8"),
]


def _full_stat_baseline(item_id: str = "longsword") -> CatalogItem:
    """A verbatim baseline item with every mechanical field populated, so a
    single-field world override can be tested in isolation."""
    return CatalogItem(
        id=item_id,
        name="Sword, Long",
        description="A bound WWN item.",
        category="weapon",
        value=15,
        weight=3.0,
        lore="Forged in the old smithies.",
        damage=DamageSpec(dice="1d8"),
        armor_class=12,
        mitigation=1,
        tech_level=3,
        range_band="pistol",
        magazine=6,
        resource_ticks=2,
        heal_amount="1d6",
        provenance=_verbatim(),
    )


class TestVerbatimFieldLock:
    @pytest.mark.parametrize(
        ("field", "_baseline_value", "world_value"),
        _MECHANICAL_LOCK_CASES,
        ids=[c[0] for c in _MECHANICAL_LOCK_CASES],
    )
    def test_every_mechanical_field_locks_on_verbatim(
        self, field: str, _baseline_value: Any, world_value: Any
    ) -> None:
        """R4 (hardening): a world override of ANY mechanical field on a verbatim
        baseline raises, and the error names the offending field. Parameterized
        over every entry in ``_MECHANICAL_FIELDS`` so a truncated lock tuple is
        caught — not just the two fields the original tests pinned."""
        from sidequest.server.dispatch.inventory_resolve import VerbatimFieldLockError

        baseline = _full_stat_baseline()
        world_item = baseline.model_copy(update={"name": "Vibroblade", field: world_value})
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        with pytest.raises(VerbatimFieldLockError) as exc_info:
            resolve_inventory(pack, "w")
        msg = str(exc_info.value)
        assert "longsword" in msg, "field-lock error must name the offending item id"
        assert field in msg, f"field-lock error must name the offending field {field!r}"

    def test_category_change_on_verbatim_raises_not_silently_dropped(self) -> None:
        """FIX 2 (No Silent Fallbacks): ``category`` is a required field; a world
        reskin that changes it on a verbatim baseline must FAIL LOUD via the lock,
        not silently discard the change. (Regression: category was in neither
        bucket and was dropped without error.)"""
        from sidequest.server.dispatch.inventory_resolve import VerbatimFieldLockError

        baseline = _weapon("longsword", provenance=_verbatim())  # category="weapon"
        world_item = baseline.model_copy(update={"name": "Toolblade", "category": "tool"})
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        with pytest.raises(VerbatimFieldLockError) as exc_info:
            resolve_inventory(pack, "w")
        msg = str(exc_info.value)
        assert "longsword" in msg
        assert "category" in msg

    def test_nested_damage_shock_change_on_verbatim_raises(self) -> None:
        """R4 (hardening): changing a NESTED damage field (``shock``) while leaving
        ``dice`` unchanged still re-stats the verbatim mechanical envelope and must
        raise — the lock compares the whole DamageSpec, not just the dice string."""
        from sidequest.server.dispatch.inventory_resolve import VerbatimFieldLockError

        baseline = CatalogItem(
            id="katana",
            name="Katana",
            description="A bound CWN blade.",
            category="weapon",
            damage=DamageSpec(dice="1d8", shock=2, shock_ac=15),
            provenance=_verbatim(srd="cwn"),
        )
        # dice unchanged (1d8), but shock 2 -> 3 (shock_ac kept valid). A re-stat.
        world_item = baseline.model_copy(
            update={
                "name": "Vibrokatana",
                "damage": DamageSpec(dice="1d8", shock=3, shock_ac=15),
            }
        )
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        with pytest.raises(VerbatimFieldLockError) as exc_info:
            resolve_inventory(pack, "w")
        assert "katana" in str(exc_info.value)
        assert "damage" in str(exc_info.value)

    def test_mechanical_override_on_verbatim_raises(self) -> None:
        """R4: a world override that CHANGES a mechanical field (damage) of a
        mode=verbatim baseline item is a hard error naming the id and field."""
        baseline = _weapon("longsword", dice="1d8", provenance=_verbatim())
        # World tries to re-stat the bound item: 1d8 -> 2d6. Forbidden (ADR-143 trap).
        world_item = _weapon("longsword", name="Vibroblade", dice="2d6", provenance=_verbatim())
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        with pytest.raises(Exception) as exc_info:
            resolve_inventory(pack, "w")
        msg = str(exc_info.value)
        assert "longsword" in msg, "field-lock error must name the offending item id"
        assert "damage" in msg.lower() or "dice" in msg.lower(), (
            "field-lock error must name the offending mechanical field"
        )

    def test_value_override_on_verbatim_raises(self) -> None:
        """R4: re-pricing a verbatim item (value) is also a locked-field violation."""
        baseline = _weapon("longsword", value=15, provenance=_verbatim())
        world_item = _weapon("longsword", name="Vibroblade", value=999, provenance=_verbatim())
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        with pytest.raises(Exception) as exc_info:
            resolve_inventory(pack, "w")
        assert "longsword" in str(exc_info.value)

    def test_presentation_only_override_does_not_raise(self) -> None:
        """R4 (the success case): a presentation-only reskin of a verbatim item
        merges cleanly with no validation error."""
        baseline = _weapon(
            "longsword", name="Sword, Long", dice="1d8", value=15, provenance=_verbatim()
        )
        world_item = _weapon(
            "longsword", name="Vibroblade", dice="1d8", value=15, provenance=_verbatim()
        )
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        resolved = resolve_inventory(pack, "w")  # must NOT raise
        assert resolved is not None
        assert _by_id(resolved)["longsword"].name == "Vibroblade"

    def test_bespoke_baseline_is_not_field_locked(self) -> None:
        """Edge: the field-lock fires ONLY for verbatim baseline items. A
        non-verbatim (bespoke) baseline does not lock mechanics — a same-id world
        override may differ without raising (ADR-145: only verbatim binds math)."""
        baseline = _weapon("homebrew_axe", dice="1d8", provenance=_bespoke())
        world_item = _weapon("homebrew_axe", name="World Axe", dice="2d6", provenance=_bespoke())
        pack = _make_pack(
            genre_inventory=_inv(catalog=[baseline]),
            worlds={"w": _inv(catalog=[world_item])},
        )
        resolved = resolve_inventory(pack, "w")  # must NOT raise
        assert resolved is not None
        assert "homebrew_axe" in {i.id for i in resolved.item_catalog}


# ===========================================================================
# R5 — non-catalog fields STILL replace wholesale (NOT unioned)
# ===========================================================================
class TestNonCatalogFieldsReplace:
    def test_currency_replaces_not_unioned(self) -> None:
        """R5: the world currency REPLACES the genre currency (existing semantics)."""
        pack = _make_pack(
            genre_inventory=_inv(
                currency="gold", catalog=[_weapon("longsword", provenance=_verbatim())]
            ),
            worlds={
                "w": _inv(currency="credits", catalog=[_weapon("blaster", provenance=_bespoke())])
            },
        )
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        assert resolved.currency is not None
        assert resolved.currency.name == "credits", "world currency replaces genre currency (R5)"

    def test_starting_equipment_replaces_not_merged(self) -> None:
        """R5: starting_equipment is world-replaces-genre, NOT merged by class key.

        The genre baseline kit must NOT bleed into the resolved starting_equipment
        when the world ships its own kit map."""
        pack = _make_pack(
            genre_inventory=_inv(
                catalog=[_weapon("longsword", provenance=_verbatim())],
                starting_equipment={"Warrior": ["longsword"], "Mage": ["dagger"]},
                starting_gold={"Warrior": 50},
            ),
            worlds={
                "w": _inv(
                    catalog=[_weapon("blaster", provenance=_bespoke())],
                    starting_equipment={"Pilot": ["blaster"]},
                    starting_gold={"Pilot": 500},
                )
            },
        )
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        # Replace, NOT merge: only the world's kit keys survive.
        assert set(resolved.starting_equipment.keys()) == {"Pilot"}, (
            "starting_equipment must REPLACE wholesale, not union by class key (R5)"
        )
        assert set(resolved.starting_gold.keys()) == {"Pilot"}
        # But the CATALOG did union (the genre longsword survives alongside blaster).
        assert {i.id for i in resolved.item_catalog} == {"longsword", "blaster"}


# ===========================================================================
# R6 — OTEL: union-merge span fires with counts
# ===========================================================================
class TestUnionMergeOtel:
    def test_merge_emits_span_with_counts(self, captured_watcher_events: list[dict]) -> None:
        """R6: the merge decision emits a state_transition span recording that a
        union merge happened, with baseline / world-override / world-added counts."""
        baseline = [
            _weapon("longsword", provenance=_verbatim()),  # shared id, will be overridden
            _weapon("dagger", provenance=_verbatim()),  # baseline-only, retained
        ]
        world = [
            _weapon("longsword", name="Vibroblade", provenance=_verbatim()),  # override
            _weapon("blaster", provenance=_bespoke()),  # world-added
        ]
        pack = _make_pack(
            genre_inventory=_inv(catalog=baseline),
            worlds={"coyote_star": _inv(catalog=world)},
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
        # The span must prove a union merge engaged (not a wholesale replace).
        assert fields.get("op") == "merged", "merge span op must be 'merged' (R6)"
        assert fields.get("tier") == "world"
        assert fields.get("baseline_catalog_count") == 2, "baseline count (R6)"
        assert fields.get("world_override_count") == 1, "shared-id override count (R6)"
        assert fields.get("world_added_count") == 1, "world-only added count (R6)"
        # Resolved catalog count = union size = 3 (longsword, dagger, blaster).
        assert fields.get("catalog_count") == 3
        assert spans[-1]["component"] == "genre"


# ===========================================================================
# Edges
# ===========================================================================
class TestMergeEdges:
    def test_world_ships_no_inventory_is_pure_baseline(self) -> None:
        """Edge: world with no inventory.yaml → resolved == pure genre baseline."""
        baseline = [
            _weapon("longsword", provenance=_verbatim()),
            _weapon("dagger", provenance=_verbatim()),
        ]
        pack = _make_pack(genre_inventory=_inv(catalog=baseline), worlds={"w": None})
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        assert {i.id for i in resolved.item_catalog} == {"longsword", "dagger"}

    def test_no_baseline_world_catalog_stands_alone(self) -> None:
        """Edge: genre ships NO baseline catalog; world catalog stands alone
        (nothing to merge into)."""
        pack = _make_pack(
            genre_inventory=None,
            worlds={"w": _inv(catalog=[_weapon("blaster", provenance=_bespoke())])},
        )
        resolved = resolve_inventory(pack, "w")
        assert resolved is not None
        assert {i.id for i in resolved.item_catalog} == {"blaster"}

    def test_new_world_bespoke_item_added_cleanly(self) -> None:
        """Edge: a brand-new world id with mode=bespoke is added with no validation
        error (it is not claiming to reskin a baseline item)."""
        pack = _make_pack(
            genre_inventory=_inv(catalog=[_weapon("longsword", provenance=_verbatim())]),
            worlds={
                "w": _inv(
                    catalog=[_weapon("radium_rifle", name="Radium Rifle", provenance=_bespoke())]
                )
            },
        )
        resolved = resolve_inventory(pack, "w")  # must NOT raise
        assert resolved is not None
        radium = _by_id(resolved)["radium_rifle"]
        assert radium.provenance is not None and radium.provenance.mode == "bespoke"


# ===========================================================================
# WIRING — the production consumer (damage strike resolution) gets the MERGED
# catalog, not a wholesale replace. This is the regression-as-behavior guard:
# resolve_damage_spec_from_beat_and_actor looks up an equipped weapon by id in
# the world-resolved catalog. Under today's REPLACE, a genre-baseline weapon is
# dropped and strike damage falls through to the unarmed floor. Under the merge,
# the baseline weapon survives and its damage resolves.
# ===========================================================================
class TestMergedCatalogReachesDamageResolution:
    def _strike_beat(self):
        from sidequest.genre.models.rules import BeatDef

        return BeatDef.model_validate(
            {
                "id": "punch",
                "label": "Strike",
                "kind": "strike",
                "base": 2,
                "stat_check": "STRENGTH",
                "damage_channel": "strike",
            }
        )

    def _actor_with_equipped(self, item_id: str):
        from sidequest.game.creature_core import CreatureCore, Inventory

        return CreatureCore(
            name="Saint",
            description="armed",
            personality="grim",
            inventory=Inventory(items=[{"id": item_id}]),  # id only; damage comes from the catalog
            hp={"current": 12, "max": 12, "base_max": 12},
        )

    def test_baseline_weapon_damage_resolves_through_merged_catalog(self) -> None:
        """WIRING: a genre-baseline weapon equipped in a world that ships its OWN
        inventory still resolves its damage — because the merge kept the baseline
        item in the world-resolved catalog. Today's REPLACE would drop it and the
        strike would fall through to the unarmed floor."""
        from sidequest.server.dispatch.damage_roll import (
            resolve_damage_spec_from_beat_and_actor,
        )

        # Genre baseline ships the bound weapon; the world ships only its own gear.
        pack = _make_pack(
            genre_inventory=_inv(
                catalog=[_weapon("power_glove", dice="1d8", provenance=_verbatim())]
            ),
            worlds={
                "seaboard_of_saints": _inv(
                    catalog=[_weapon("scrap_rifle", dice="1d6", provenance=_bespoke())]
                )
            },
        )
        # Actor equips the BASELINE weapon by id.
        actor = self._actor_with_equipped("power_glove")

        spec = resolve_damage_spec_from_beat_and_actor(
            beat=self._strike_beat(),
            actor_core=actor,
            pack=pack,
            world_slug="seaboard_of_saints",
        )
        assert spec is not None, (
            "baseline weapon must resolve its damage via the merged world-resolved catalog"
        )
        assert spec.dice == "1d8", "the bound baseline weapon's damage must survive the merge"
