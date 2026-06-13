"""Story 106-1 (RED) — equip kit-rolled armor at chargen + derive AC from content.

PLAYTEST BUG (caverns_and_claudes/beneath_sunden, WWN ruleset, 2026-06-13): every
Warrior fought at the unarmored base AC 10 even though the chargen kit hands each
one Leather Armor. The kit-roll loop appends every item ``equipped: False``
(builder.py:2570) and **nothing** ever equips the rolled armor or recomputes
``core.armor_class`` from it (creature_core.py:123 default = 10; dice.py:1636 reads
``core.armor_class`` for the opponent reprisal). At AC 10 the dungeon creatures hit
~65%; WWN leather (AC 13) drops that to ~45% — the lethality driver.

This story introduces a deterministic post-build chargen step that (a) EQUIPS the
kit-rolled armor and (b) recomputes ``character.core.armor_class`` from the equipped
armor's catalog ``armor_class`` (sourced from the WWN SRD via content, NOT an
invented engine constant). The natural home is alongside ``apply_starting_loadout``
in ``sidequest/server/dispatch/chargen_loadout.py``.

These are behavior tests on the new ``equip_starting_armor`` step + OTEL span
assertions (the acceptance gate, CLAUDE.md OTEL Observability Principle). RED until
the step exists.

Span-name contracts pinned here (the GM-panel lie-detector reads these):
- ``chargen.armor_equipped``   — INFO: an armor item was equipped and AC derived.
- ``chargen.armor_unresolved`` — WARN/ERROR: an equipped armor item has no catalog
  ``armor_class`` to derive from (the No-Silent-Fallback gate).
"""

from __future__ import annotations

# Span names the chargen armor-derivation step MUST emit. Pinned as the contract
# Dev implements (mirrors the reprisal e2e test pinning SPAN_OPPONENT_ATTACK).
SPAN_ARMOR_EQUIPPED = "chargen.armor_equipped"
SPAN_ARMOR_UNRESOLVED = "chargen.armor_unresolved"


# ---------------------------------------------------------------------------
# Fixture helpers (inline — no cross-test coupling)
# ---------------------------------------------------------------------------


def _armor_item(item_id: str = "leather_armor", *, equipped: bool = False) -> dict:
    """A kit-roll-produced armor item dict (builder.py shape: equipped:False)."""
    return {
        "id": item_id,
        "name": item_id.replace("_", " ").title(),
        "description": f"Starting equipment (armor): {item_id}",
        "category": "armor",
        "value": 0,
        "weight": 1.0,
        "rarity": "common",
        "narrative_weight": 0.3,
        "tags": [],
        "equipped": equipped,
        "quantity": 1,
        "uses_remaining": None,
        "state": "Carried",
    }


def _weapon_item(item_id: str, *, equipped: bool) -> dict:
    item = _armor_item(item_id, equipped=equipped)
    item["category"] = "weapon"
    return item


def _make_character(items: list[dict], *, char_class: str = "Warrior"):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    core = CreatureCore(
        name="Grix",
        description="A scarred delver.",
        personality="grim",
        inventory=Inventory(items=list(items)),
    )
    return Character(core=core, char_class=char_class, race="Human", backstory="Born underground.")


def _catalog(*, leather_ac: int | None = 13):
    """InventoryConfig whose item_catalog carries a leather_armor entry.

    ``leather_ac=None`` reproduces the content gap (catalog entry exists but
    declares no ``armor_class``) — the No-Silent-Fallback case.
    """
    from sidequest.genre.models.inventory import CatalogItem, InventoryConfig

    return InventoryConfig(
        item_catalog=[
            CatalogItem(
                id="leather_armor",
                name="Leather Armor",
                description="Hardened leather shaped to cover the vitals.",
                category="armor",
                value=20,
                weight=5.0,
                rarity="common",
                tags=["light-armor", "torso"],
                armor_class=leather_ac,
            ),
            CatalogItem(
                id="iron_mace",
                name="Iron Mace",
                description="A brutal bludgeon.",
                category="weapon",
            ),
        ]
    )


def _multi_armor_catalog(*, leather_ac: int | None, chain_ac: int | None):
    """InventoryConfig with two armor entries — torso (leather) + a heavier
    piece (chain) — for exercising the best-armor ``max()`` combination rule.
    ``None`` on either reproduces the missing-value gap for that piece."""
    from sidequest.genre.models.inventory import CatalogItem, InventoryConfig

    return InventoryConfig(
        item_catalog=[
            CatalogItem(
                id="leather_armor",
                name="Leather Armor",
                description="Hardened leather.",
                category="armor",
                tags=["light-armor", "torso"],
                armor_class=leather_ac,
            ),
            CatalogItem(
                id="chain_shirt",
                name="Chain Shirt",
                description="Interlocking iron rings.",
                category="armor",
                tags=["medium-armor", "torso"],
                armor_class=chain_ac,
            ),
        ]
    )


def _equip_starting_armor(character, config, **kw):
    """Import the (not-yet-existing) step at call time so collection still
    succeeds and the RED failure is a clear ImportError at the assertion site."""
    from sidequest.server.dispatch.chargen_loadout import equip_starting_armor

    return equip_starting_armor(character, config, **kw)


# ---------------------------------------------------------------------------
# AC1 — the kit-rolled armor is equipped at chargen
# ---------------------------------------------------------------------------


def test_kit_rolled_armor_is_equipped_at_chargen():
    """AC1: a freshly rolled Warrior's Leather Armor flips equipped:false → true."""
    character = _make_character([_armor_item("leather_armor", equipped=False)])

    _equip_starting_armor(character, _catalog(leather_ac=13))

    leather = next(i for i in character.core.inventory.items if i["id"] == "leather_armor")
    assert leather["equipped"] is True, "kit-rolled armor must be equipped at chargen"


# ---------------------------------------------------------------------------
# AC2 — AC derived from the WWN SRD content value, never a hardcoded constant
# ---------------------------------------------------------------------------


def test_armor_class_derived_from_catalog_value():
    """AC2: core.armor_class equals the catalog (WWN-SRD) leather value, > 10."""
    character = _make_character([_armor_item("leather_armor")])
    assert character.core.armor_class == 10, "precondition: unarmored default"

    _equip_starting_armor(character, _catalog(leather_ac=13))

    assert character.core.armor_class == 13


def test_armor_class_follows_content_mutation():
    """AC2 (no invented numbers): change the content value and the derived AC
    follows — proving it flows from content, not a baked engine constant."""
    char_a = _make_character([_armor_item("leather_armor")])
    char_b = _make_character([_armor_item("leather_armor")])

    _equip_starting_armor(char_a, _catalog(leather_ac=13))
    _equip_starting_armor(char_b, _catalog(leather_ac=17))

    assert char_a.core.armor_class == 13
    assert char_b.core.armor_class == 17, "derived AC must track the content value"


# ---------------------------------------------------------------------------
# OTEL gate — the derivation span fires with proof attributes
# ---------------------------------------------------------------------------


def test_armor_equipped_span_carries_derivation_attrs(otel_capture):
    """OTEL gate: the chargen armor-derivation span records the item id, the
    catalog armor_class, AC before/after, and the equipped flip — so the GM
    panel can confirm the derivation fired (CLAUDE.md OTEL Observability)."""
    character = _make_character([_armor_item("leather_armor")])

    _equip_starting_armor(character, _catalog(leather_ac=13))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_EQUIPPED]
    assert len(spans) == 1, f"exactly one {SPAN_ARMOR_EQUIPPED} span must fire"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("item_id") == "leather_armor"
    assert attrs.get("armor_class") == 13
    assert attrs.get("ac_before") == 10
    assert attrs.get("ac_after") == 13
    assert attrs.get("equipped_after") is True


# ---------------------------------------------------------------------------
# AC4 — No Silent Fallback: armor with no catalog armor_class fails LOUD
# ---------------------------------------------------------------------------


def test_missing_catalog_armor_class_fails_loud(otel_capture):
    """AC4: a kit armor item whose catalog entry declares NO armor_class emits a
    loud unresolved span (the content gap surfaces at chargen) and the PC is NOT
    silently left at AC 10 with the gap hidden."""
    character = _make_character([_armor_item("leather_armor")])

    _equip_starting_armor(character, _catalog(leather_ac=None))

    unresolved = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_UNRESOLVED]
    assert len(unresolved) == 1, (
        "armor with no catalog armor_class must fire a loud unresolved span, "
        "not silently leave AC at 10"
    )
    attrs = dict(unresolved[0].attributes or {})
    assert attrs.get("item_id") == "leather_armor"
    # And it must NOT have fabricated an AC out of nothing.
    equipped_spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_EQUIPPED]
    assert not equipped_spans, "no derivation span when there is no value to derive"


# ---------------------------------------------------------------------------
# AC5 — No regression
# ---------------------------------------------------------------------------


def test_unarmored_class_stays_ac_10_silently(otel_capture):
    """AC5: a class with NO kit armor stays at AC 10 and fires NEITHER the
    derivation span NOR the loud unresolved span — legitimately unarmored is not
    a content gap (distinguishes from AC4)."""
    character = _make_character([_weapon_item("staff_wood", equipped=False)], char_class="Mage")

    _equip_starting_armor(character, _catalog(leather_ac=13))

    assert character.core.armor_class == 10
    names = {s.name for s in otel_capture.get_finished_spans()}
    assert SPAN_ARMOR_EQUIPPED not in names
    assert SPAN_ARMOR_UNRESOLVED not in names, "no-armor is not a fail-loud condition"


def test_weapons_remain_equipped_after_armor_step():
    """AC5: the armor step touches ONLY armor-category items — equipped weapons
    stay equipped, unequipped weapons stay unequipped."""
    items = [
        _weapon_item("short_sword", equipped=True),  # item_hint path → already equipped
        _weapon_item("hand_axe", equipped=False),  # kit-roll weapon → stays as-is
        _armor_item("leather_armor", equipped=False),
    ]
    character = _make_character(items)

    _equip_starting_armor(character, _catalog(leather_ac=13))

    by_id = {i["id"]: i for i in character.core.inventory.items}
    assert by_id["short_sword"]["equipped"] is True, "equipped weapon must stay equipped"
    assert by_id["hand_axe"]["equipped"] is False, "armor step must not touch weapons"


# ---------------------------------------------------------------------------
# AC2 (best-armor combination) — multiple equipped armor pieces resolve to the
# MAX catalog armor_class (WWN best-armor rule, NOT additive). Reviewer
# [MEDIUM][TEST]: the ``ac_after = max(...)`` branch in equip_starting_armor
# was implemented but no test exercised >1 armor item — a dead-on-arrival
# branch. A later story that mistakenly made armor additive (sum) or picked
# the FIRST/last piece instead of the best would slip through without these.
# ---------------------------------------------------------------------------


def test_best_armor_max_wins_over_lower_piece():
    """Two equipped armor pieces (leather 13 + chain 15) → core.armor_class is
    the MAX (15), not the sum (28), not the first-listed (13). WWN uses
    best-armor AC, not additive stacking."""
    character = _make_character(
        [
            _armor_item("leather_armor", equipped=False),
            _armor_item("chain_shirt", equipped=False),
        ]
    )

    ac = _equip_starting_armor(character, _multi_armor_catalog(leather_ac=13, chain_ac=15))

    assert ac == 15, "best-armor rule: derived AC is the max piece, not sum/first"
    assert character.core.armor_class == 15
    # BOTH pieces flip equipped — the step equips every armor-category item,
    # the AC is just derived from the best of them.
    by_id = {i["id"]: i for i in character.core.inventory.items}
    assert by_id["leather_armor"]["equipped"] is True
    assert by_id["chain_shirt"]["equipped"] is True


def test_best_armor_max_independent_of_item_order():
    """Order-independence guard: listing the heavier piece FIRST must still
    yield the max — catches a regression that took the first/last piece's AC
    rather than the best."""
    character = _make_character(
        [
            _armor_item("chain_shirt", equipped=False),  # higher AC, listed first
            _armor_item("leather_armor", equipped=False),
        ]
    )

    ac = _equip_starting_armor(character, _multi_armor_catalog(leather_ac=13, chain_ac=15))

    assert ac == 15, "max must win regardless of inventory order"


def test_best_armor_fires_one_equipped_span_per_armor_piece(otel_capture):
    """Each equipped armor piece emits its own ``chargen.armor_equipped`` span,
    and EVERY span carries the same derived ``ac_after`` (the max). The GM panel
    must see one row per equipped piece, all agreeing on the final AC."""
    character = _make_character(
        [
            _armor_item("leather_armor", equipped=False),
            _armor_item("chain_shirt", equipped=False),
        ]
    )

    _equip_starting_armor(character, _multi_armor_catalog(leather_ac=13, chain_ac=15))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_EQUIPPED]
    assert len(spans) == 2, "one equipped span per armor piece"
    item_ids = {dict(s.attributes or {}).get("item_id") for s in spans}
    assert item_ids == {"leather_armor", "chain_shirt"}
    # Both spans report the derived max as ac_after — not each piece's own AC.
    assert all(dict(s.attributes or {}).get("ac_after") == 15 for s in spans)
    # ac_before is the pre-derivation unarmored 10 on every span.
    assert all(dict(s.attributes or {}).get("ac_before") == 10 for s in spans)


def test_best_armor_skips_missing_piece_but_derives_from_valued_one(otel_capture):
    """Mixed batch: leather has a catalog armor_class (13), chain does NOT
    (None). The valued piece derives AC 13; the value-less piece fires a loud
    unresolved span and is NOT equipped — partial coverage must not poison the
    derivable piece, nor silently swallow the gap."""
    character = _make_character(
        [
            _armor_item("leather_armor", equipped=False),
            _armor_item("chain_shirt", equipped=False),
        ]
    )

    ac = _equip_starting_armor(character, _multi_armor_catalog(leather_ac=13, chain_ac=None))

    assert ac == 13, "derive from the one valued piece"
    by_id = {i["id"]: i for i in character.core.inventory.items}
    assert by_id["leather_armor"]["equipped"] is True, "valued piece is equipped"
    assert by_id["chain_shirt"]["equipped"] is False, "value-less piece is NOT equipped"
    names = {s.name for s in otel_capture.get_finished_spans()}
    assert SPAN_ARMOR_EQUIPPED in names, "the valued piece derives + equips"
    assert SPAN_ARMOR_UNRESOLVED in names, "the value-less piece fails loud"


# ---------------------------------------------------------------------------
# AC4 (No Silent Fallback) — reason discriminator coverage. Reviewer
# [MEDIUM][TEST]: only the ``catalog_armor_class_missing`` reason was
# exercised; the ``no_catalog_entry`` path and ``inventory_config=None`` (both
# real production branches) were untested, and the ``reason`` span attribute —
# the GM-panel discriminator that tells "author forgot the value" apart from
# "item isn't in the catalog at all" — was never asserted. A regression that
# collapsed both reasons to one string would blunt the GM panel and pass
# unnoticed.
# ---------------------------------------------------------------------------


def _unresolved_span(otel_capture):
    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_UNRESOLVED]
    assert len(spans) == 1, f"exactly one {SPAN_ARMOR_UNRESOLVED} span must fire"
    return dict(spans[0].attributes or {})


def test_unresolved_reason_is_catalog_armor_class_missing(otel_capture):
    """AC4 discriminator: a catalog entry that EXISTS but declares no
    armor_class fires reason='catalog_armor_class_missing' (author forgot the
    value)."""
    character = _make_character([_armor_item("leather_armor")])

    ac = _equip_starting_armor(character, _catalog(leather_ac=None))

    assert character.core.armor_class == 10, "no silent AC fabrication"
    assert ac == 10
    attrs = _unresolved_span(otel_capture)
    assert attrs.get("reason") == "catalog_armor_class_missing", (
        "the reason discriminator distinguishes 'value missing' from "
        "'no catalog entry' — the GM panel filters on it"
    )
    assert attrs.get("item_id") == "leather_armor"


def test_unresolved_reason_is_no_catalog_entry(otel_capture):
    """AC4 discriminator: an armor item whose id is absent from the catalog
    ENTIRELY fires reason='no_catalog_entry' (item isn't catalogued at all) —
    a distinct production branch from the missing-value case above."""
    # ``mystery_plate`` is not in _catalog() (only leather_armor + iron_mace).
    character = _make_character([_armor_item("mystery_plate")])

    ac = _equip_starting_armor(character, _catalog(leather_ac=13))

    assert character.core.armor_class == 10, "uncatalogued armor → no silent AC"
    assert ac == 10
    attrs = _unresolved_span(otel_capture)
    assert attrs.get("reason") == "no_catalog_entry", (
        "an armor id with no catalog row must be discriminated from a row "
        "that merely lacks armor_class"
    )
    assert attrs.get("item_id") == "mystery_plate"
    # And it must NOT have fabricated a derivation span.
    assert not [s for s in otel_capture.get_finished_spans() if s.name == SPAN_ARMOR_EQUIPPED], (
        "no derivation span when there is nothing to derive from"
    )


def test_none_inventory_config_armor_fails_loud_no_catalog_entry(otel_capture):
    """AC4: when ``inventory_config`` is None (pack has no inventory.yaml) but
    the character still carries a kit-rolled armor item, the step must fail
    LOUD (reason='no_catalog_entry') rather than silently leave AC 10 with the
    gap hidden — the empty-catalog branch is a real production path."""
    character = _make_character([_armor_item("leather_armor")])

    ac = _equip_starting_armor(character, None)

    assert character.core.armor_class == 10
    assert ac == 10
    attrs = _unresolved_span(otel_capture)
    assert attrs.get("reason") == "no_catalog_entry", (
        "None config → no catalog row for the armor → no_catalog_entry, NOT a silent AC-10 fallback"
    )
    assert attrs.get("item_id") == "leather_armor"
