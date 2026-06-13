"""Story 106-1 (RED) — equip starting armor at chargen + derive AC from the WWN SRD.

PLAYTEST BUG (caverns_and_claudes/beneath_sunden, WWN ruleset, 2026-06-13): every
Warrior fought at the unarmored base AC 10 even though the chargen kit hands each
one armor. The armor sits in inventory ``equipped: false`` and contributes nothing,
so opponent reprisals roll vs ``target_ac: 10`` (~65% hit) all session — the single
biggest lethality driver (1-of-5 Warriors survived). See sq-playtest-pingpong
2026-06-13 + sprint/context/context-story-106-1.md.

ROOT CAUSE (two-sided gap):
  - ENGINE: the kit-roll loop (builder.py:2570) appends every kit item — including
    armor — with a hardcoded ``equipped: False``, and NOTHING recomputes
    ``character.core.armor_class`` (creature_core.py:123 default = 10) from an
    equipped armor item. dice.py:1636 ``target_ac = int(player_core.armor_class)``
    therefore always reads 10.
  - CONTENT: the ``caverns_and_claudes/inventory.yaml`` armor entries declare no
    ``armor_class`` field, so there is no SRD value to derive from.

THE CONTRACT THESE TESTS PIN (the Dev implements to satisfy them):

  A new production step ``equip_starting_armor(character, inventory_config, *,
  genre, world, player_id) -> int`` in
  ``sidequest.server.dispatch.chargen_loadout`` that, called from the chargen-confirm
  wire (chargen_mixin.py:1239, AFTER ``apply_starting_loadout``):
    1. EQUIPS the kit-rolled armor item (flips ``equipped`` -> True);
    2. RECOMPUTES ``character.core.armor_class`` from the equipped armor's catalog
       ``CatalogItem.armor_class`` (inventory.py:164) — sourced from the WWN SRD
       (leather = 13), NEVER an invented engine constant;
    3. emits a ``chargen.armor_equipped`` OTEL span (the GM-panel lie-detector gate);
    4. on a kit armor item whose catalog entry has no ``armor_class``, FAILS LOUD —
       a WARNING log + a ``chargen.armor_class_missing`` span — rather than silently
       leaving the PC at AC 10 (CLAUDE.md No Silent Fallbacks);
    5. returns the resulting ``core.armor_class``.

This file is RED until those land (it imports ``equip_starting_armor``, which does
not yet exist — collection fails loudly, which is the intended RED signal).

Span-name + function-name contract is pinned here deliberately (TDD): the test IS
the spec. If the Dev relocates/renames, the test is the thing to update in lockstep.

No source-text wiring tests (sidequest-server CLAUDE.md): the wiring proof is an
OTEL span fired through the real builder + real catalog, plus a fixture-driven
behavior assertion — both survive refactor, fail on real breakage.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.genre.models.inventory import CatalogItem, InventoryConfig

# The contract under test. Missing until GREEN -> collection-time RED for the
# whole module (the single, unambiguous "implement me" signal).
from sidequest.server.dispatch.chargen_loadout import equip_starting_armor

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"

# WWN SRD leather value (sq-playtest-pingpong 2026-06-13: "WWN leather ≈ AC 13";
# standing ruling .pennyfarthing/sidecars/gm-decisions.md — WWN-bound values come
# from the WWN SRD, not an invented number).
WWN_LEATHER_AC = 13
UNARMORED_AC = 10  # creature_core.py:123 default


# ---------------------------------------------------------------------------
# Fixture helpers (mirror tests/server/test_chargen_loadout.py shapes so the
# synthetic Character + InventoryConfig match the production data shapes)
# ---------------------------------------------------------------------------


def _make_character(char_class: str = "Warrior") -> Character:
    core = CreatureCore(
        name="Zeppo",
        description="A scarred surface-folk fighter",
        personality="Grim",
        level=1,
        xp=0,
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(
        core=core,
        backstory="Climbed down the Dropmouth one rope too many.",
        char_class=char_class,
        race="Human",
    )


def _kit_item(
    item_id: str,
    name: str,
    category: str,
    *,
    equipped: bool = False,
) -> dict:
    """Mirror the dict the kit-roll loop appends (builder.py:2559-2574) —
    crucially ``equipped: False`` for kit items, ``state: "Carried"``."""
    return {
        "id": item_id,
        "name": name,
        "description": f"Starting equipment ({category}): {name}",
        "category": category,
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


def _leather_catalog(armor_class: int | None = WWN_LEATHER_AC) -> list[CatalogItem]:
    return [
        CatalogItem(
            id="leather_armor",
            name="Leather Armor",
            description="Boiled hide and strapping.",
            category="armor",
            value=20,
            weight=10.0,
            rarity="common",
            tags=["armor"],
            armor_class=armor_class,
        ),
        CatalogItem(
            id="sword_short",
            name="Short Sword",
            description="A dependable blade.",
            category="weapon",
            value=10,
            weight=2.0,
            rarity="common",
            tags=["weapon"],
        ),
    ]


def _config(catalog: list[CatalogItem] | None = None) -> InventoryConfig:
    return InventoryConfig(
        item_catalog=catalog if catalog is not None else _leather_catalog(),
        starting_equipment={},
        starting_gold={},
    )


def _armor_item(char: Character) -> dict | None:
    return next(
        (it for it in char.core.inventory.items if it.get("category") == "armor"),
        None,
    )


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — armor equips and AC derives from the WWN-SRD catalog value
# ---------------------------------------------------------------------------


def test_kit_armor_is_equipped_at_chargen() -> None:
    """AC-1: a kit-rolled Leather Armor (equipped:false) is flipped to
    equipped:true by the chargen armor step."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    equip_starting_armor(char, _config())

    armor = _armor_item(char)
    assert armor is not None, "the leather armor must still be on the sheet"
    assert armor["equipped"] is True, (
        "kit-rolled armor must be EQUIPPED at chargen — it shipped equipped:false "
        "(builder.py:2570) and nothing flipped it, so every Warrior fought at AC 10."
    )


def test_armor_class_derived_from_catalog_value() -> None:
    """AC-2: ``core.armor_class`` equals the WWN-SRD leather value (13) sourced
    from the catalog ``armor_class``, and is strictly greater than the unarmored 10."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))
    assert char.core.armor_class == UNARMORED_AC, "precondition: starts unarmored at 10"

    derived = equip_starting_armor(char, _config())

    assert char.core.armor_class == WWN_LEATHER_AC, (
        f"AC must be recomputed from the equipped armor's catalog armor_class "
        f"(WWN leather = {WWN_LEATHER_AC}). Got {char.core.armor_class}."
    )
    assert char.core.armor_class > UNARMORED_AC
    assert derived == WWN_LEATHER_AC, "the function must return the resulting armor_class"


def test_armor_class_follows_content_not_a_hardcoded_constant() -> None:
    """AC-2 (derive, don't bake): mutate the catalog ``armor_class`` and the
    derived AC follows — proving the value flows FROM CONTENT, not a hardcoded 13
    in the engine. A Dev who hardcodes ``armor_class = 13`` fails this test."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    # Catalog declares 15 (not the canonical 13) — the engine must not override it.
    equip_starting_armor(char, _config(_leather_catalog(armor_class=15)))

    assert char.core.armor_class == 15, (
        "Derived AC must track the catalog armor_class (15 here), not a baked-in "
        f"engine constant. Got {char.core.armor_class} — the engine ignored content."
    )


# ---------------------------------------------------------------------------
# AC-4 — no silent fallback on a content gap (missing catalog armor_class)
# ---------------------------------------------------------------------------


def test_missing_catalog_armor_class_fails_loud(caplog) -> None:
    """AC-4: a kit armor item whose catalog entry has no ``armor_class`` must
    FAIL LOUD (a WARNING) — never silently leave the PC at AC 10. The content gap
    is the other half of this bug; it must surface at chargen, not be masked."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))
    # Catalog entry exists but armor_class is None — the real beneath_sunden
    # content state today (inventory.yaml armor entries have no armor_class).
    config = _config(_leather_catalog(armor_class=None))

    with caplog.at_level(logging.WARNING):
        equip_starting_armor(char, config, genre="caverns_and_claudes", world="beneath_sunden")

    gap = [r for r in caplog.records if "armor_class" in r.getMessage().lower()]
    assert gap, (
        "A kit armor item with no catalog armor_class must emit a loud WARNING "
        "(No Silent Fallbacks) — got none. The AC was left at 10 silently."
    )


def test_missing_catalog_armor_class_does_not_invent_an_ac(caplog) -> None:
    """AC-4 corollary: when the catalog armor_class is missing the engine must NOT
    invent a value — AC stays at the unarmored 10 (loudly), it does not silently
    fabricate 13. 'Derive, don't bake' cuts both ways."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    with caplog.at_level(logging.WARNING):
        equip_starting_armor(char, _config(_leather_catalog(armor_class=None)))

    assert char.core.armor_class == UNARMORED_AC, (
        "With no SRD value to derive from, AC must remain the unarmored 10 (loudly) "
        f"— the engine must not invent a number. Got {char.core.armor_class}."
    )


# ---------------------------------------------------------------------------
# AC-5 — no regression: unarmored stays 10, weapons untouched, idempotent
# ---------------------------------------------------------------------------


def test_unarmored_character_stays_at_ac_10() -> None:
    """AC-5: a character with no armor item (e.g. WWN Mage — warrior_kit.armor is
    empty for mage_kit) keeps the unarmored AC 10 and gains no phantom armor."""
    char = _make_character("Mage")
    char.core.inventory.items.append(_kit_item("dagger_iron", "Iron Dagger", "weapon"))

    derived = equip_starting_armor(char, _config())

    assert char.core.armor_class == UNARMORED_AC
    assert derived == UNARMORED_AC
    assert _armor_item(char) is None, "no armor must be conjured for a class with no kit armor"


def test_weapon_items_are_not_touched_by_the_armor_step() -> None:
    """AC-5 regression: the armor step must only equip ARMOR. A kit weapon's
    ``equipped`` flag is not its concern (weapons reach the sheet via their own
    path) — the step must not flip unrelated categories."""
    char = _make_character("Warrior")
    weapon = _kit_item("sword_short", "Short Sword", "weapon", equipped=False)
    char.core.inventory.items.append(weapon)
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    equip_starting_armor(char, _config())

    weap = next(it for it in char.core.inventory.items if it["id"] == "sword_short")
    assert weap["equipped"] is False, (
        "the armor step must not touch weapon items — it changed the short sword's "
        "equipped flag, which is out of scope and risks double-equip regressions."
    )


def test_idempotent_on_already_equipped_armor() -> None:
    """Paranoia: running the step twice (or on a reloaded save whose armor is
    already equipped + AC already derived) must be a stable no-op, not a
    double-application or a reset to 10."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    equip_starting_armor(char, _config())
    first = char.core.armor_class
    equip_starting_armor(char, _config())

    assert char.core.armor_class == first == WWN_LEATHER_AC, (
        "second pass must be idempotent — AC must stay at the derived value, not "
        f"double-apply or reset. first={first}, second={char.core.armor_class}."
    )
    assert _armor_item(char)["equipped"] is True


# ---------------------------------------------------------------------------
# AC-3 — the recomputed AC lands on the exact field the reprisal reads
# ---------------------------------------------------------------------------


def test_derived_ac_lands_on_the_field_the_reprisal_reads() -> None:
    """AC-3: dice.py:1636 reads ``player_core.armor_class`` for the opponent
    reprisal's ``target_ac`` (proven downstream by
    tests/integration/test_opponent_reprisal_e2e.py, which forces hit/miss by
    setting the player's AC). This test pins the upstream half: the chargen step
    raises THAT field (``core.armor_class``) — so once chargen runs, reprisals roll
    vs 13, not 10. Pins the field-name contract that ties the two halves."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    equip_starting_armor(char, _config())

    # The reprisal reads exactly this attribute (dice.py:1636).
    assert getattr(char.core, "armor_class") == WWN_LEATHER_AC, (
        "the reprisal's target_ac is int(player_core.armor_class) — chargen must "
        "raise this exact field so the opponent rolls vs 13."
    )


# ---------------------------------------------------------------------------
# OTEL spans — the gate (CLAUDE.md OTEL Observability Principle)
# ---------------------------------------------------------------------------


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def test_armor_equipped_span_fires_with_attributes(otel_capture) -> None:
    """The gate: equipping armor at chargen emits a ``chargen.armor_equipped``
    span carrying the load-bearing fields the GM panel renders — armor item id,
    the catalog armor_class, AC before (10) and after (13), and the equipped flip.
    Without the span the derivation is invisible to the lie-detector."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    equip_starting_armor(
        char, _config(), genre="caverns_and_claudes", world="beneath_sunden", player_id="pid"
    )

    spans = _spans_named(otel_capture, "chargen.armor_equipped")
    assert len(spans) == 1, (
        f"chargen.armor_equipped MUST fire once when armor is equipped at chargen. "
        f"Got {len(spans)}."
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("armor_item_id") == "leather_armor"
    assert attrs.get("armor_class") == WWN_LEATHER_AC, "span must carry the derived AC"
    assert attrs.get("ac_before") == UNARMORED_AC
    assert attrs.get("ac_after") == WWN_LEATHER_AC
    assert attrs.get("equipped") is True


def test_armor_class_missing_span_fires_on_content_gap(otel_capture) -> None:
    """AC-4 (OTEL half): a kit armor item with no catalog armor_class emits a
    ``chargen.armor_class_missing`` span so the GM panel surfaces the content gap
    the turn it happens — the lie-detector for the silent-AC-10 failure mode."""
    char = _make_character("Warrior")
    char.core.inventory.items.append(_kit_item("leather_armor", "Leather Armor", "armor"))

    equip_starting_armor(char, _config(_leather_catalog(armor_class=None)))

    spans = _spans_named(otel_capture, "chargen.armor_class_missing")
    assert len(spans) == 1, (
        "chargen.armor_class_missing MUST fire when the equipped armor has no "
        f"catalog armor_class. Got {len(spans)} — the content gap is silent."
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("armor_item_id") == "leather_armor", (
        "the missing-AC span must name the offending armor item for triage."
    )


def test_armor_equipped_span_does_not_fire_for_unarmored(otel_capture) -> None:
    """Negative confirmation: no armor item -> no armor_equipped span (and no
    spurious missing span). A half-fix that always emits would make the GM panel
    cry wolf."""
    char = _make_character("Mage")
    char.core.inventory.items.append(_kit_item("dagger_iron", "Iron Dagger", "weapon"))

    equip_starting_armor(char, _config())

    assert _spans_named(otel_capture, "chargen.armor_equipped") == []
    assert _spans_named(otel_capture, "chargen.armor_class_missing") == []


# ---------------------------------------------------------------------------
# SPAN_ROUTES registration + routing-completeness (CLAUDE.md OTEL discipline)
# ---------------------------------------------------------------------------


class TestArmorSpanRouting:
    """The new spans MUST be registered in ``SPAN_ROUTES`` (or FLAT_ONLY_SPANS)
    or the watcher hub never forwards them to the GM panel — a silent failure
    mode. Mirrors the dedup-span routing pins in test_chargen_loadout.py and the
    static lint in tests/telemetry/test_routing_completeness.py."""

    def test_armor_equipped_span_constant_and_route(self) -> None:
        from sidequest.telemetry.spans import SPAN_CHARGEN_ARMOR_EQUIPPED, SPAN_ROUTES

        assert SPAN_CHARGEN_ARMOR_EQUIPPED == "chargen.armor_equipped", (
            "span constant must equal the documented name; the GM panel filters "
            "on this exact string."
        )
        assert SPAN_CHARGEN_ARMOR_EQUIPPED in SPAN_ROUTES, (
            "without a SPAN_ROUTES entry the typed armor-equipped event never "
            "reaches the GM panel — silent failure."
        )

    def test_armor_class_missing_span_constant_and_route(self) -> None:
        from sidequest.telemetry.spans import SPAN_CHARGEN_ARMOR_CLASS_MISSING, SPAN_ROUTES

        assert SPAN_CHARGEN_ARMOR_CLASS_MISSING == "chargen.armor_class_missing"
        assert SPAN_CHARGEN_ARMOR_CLASS_MISSING in SPAN_ROUTES

    def test_routing_completeness_still_passes(self) -> None:
        """Meta-check: adding SPAN_* constants without a routing decision breaks
        the broader completeness lint. The new constants must satisfy it."""
        from sidequest.telemetry import spans as spans_pkg
        from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES

        all_spans = {
            v
            for name, v in vars(spans_pkg).items()
            if name.startswith("SPAN_") and isinstance(v, str)
        }
        missing = all_spans - set(SPAN_ROUTES.keys()) - set(FLAT_ONLY_SPANS)
        assert not missing, (
            f"Spans without a routing decision: {sorted(missing)}. Add to "
            f"SPAN_ROUTES (preferred) or FLAT_ONLY_SPANS."
        )


# ---------------------------------------------------------------------------
# Integration wiring — real pack + real builder + real catalog (the wiring gate)
# ---------------------------------------------------------------------------


def _real_caverns_pack():
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("sidequest-content not on disk in this checkout")
    from sidequest.genre.loader import load_genre_pack

    return load_genre_pack(CONTENT_ROOT / "caverns_and_claudes")


def _build_real_warrior(pack):
    """Build a real Warrior through the real CharacterBuilder against the real
    caverns_and_claudes pack (mirrors tests/integration/test_cc_chargen_e2e.py
    _drive_chargen) so the kit-roll produces a REAL armor item from
    equipment_tables ``warrior_kit.armor``."""
    from sidequest.game.builder import CharacterBuilder, StoryInput

    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
        )
        .with_lobby_name("Zeppo")
        .with_equipment_tables(pack.equipment_tables)
        .with_classes(pack.classes)
    )
    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup("Zeppo")
            continue
        scene = builder.current_scene()
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_response(
                    StoryInput(
                        pronouns="he/him",
                        background="Surface-folk fighter.",
                        description="Scarred, watchful.",
                    )
                )
            continue
        idx = next(
            (
                i
                for i, c in enumerate(scene.choices)
                if c.mechanical_effects and c.mechanical_effects.class_hint == "Warrior"
            ),
            0,
        )
        builder.apply_choice(idx)
    return builder.build("Zeppo")


def test_real_warrior_armor_equips_and_derives_against_real_content(otel_capture) -> None:
    """WIRING GATE (sidequest-server CLAUDE.md): the production
    ``equip_starting_armor`` runs against the REAL caverns_and_claudes pack — a
    real Warrior built through the real CharacterBuilder (real kit-roll) + the real
    inventory catalog.

    RNG-robust: ``warrior_kit.armor`` rolls exactly ONE of
    [leather_armor, shield_wood, helmet_iron] (rolls_per_slot has no ``armor`` key
    -> 1 roll), so we assert against WHICHEVER armor actually rolled rather than
    pinning leather.

    REDs on BOTH halves of the bug today: (1) ``equip_starting_armor`` does not
    exist; (2) once it does, the real inventory.yaml armor entries still have no
    ``armor_class`` (content half), so this drives the loud-fail path until the
    content PR lands — exactly the two coordinated PRs this story requires."""
    pack = _real_caverns_pack()
    char = _build_real_warrior(pack)

    armor = _armor_item(char)
    assert armor is not None, (
        "a real WWN Warrior must roll an armor item from warrior_kit.armor "
        f"([leather_armor, shield_wood, helmet_iron]); inventory: "
        f"{[(i.get('id'), i.get('category')) for i in char.core.inventory.items]}"
    )
    assert armor["equipped"] is False, "precondition: kit armor ships equipped:false (builder.py:2570)"

    from sidequest.server.dispatch.inventory_resolve import resolve_inventory

    inv = resolve_inventory(pack, "beneath_sunden")
    equip_starting_armor(
        char, inv, genre="caverns_and_claudes", world="beneath_sunden", player_id="pid"
    )

    # Look up the rolled armor's real catalog armor_class.
    catalog_by_id = {it.id: it for it in (inv.item_catalog if inv else [])}
    cat = catalog_by_id.get(armor["id"])
    assert cat is not None, f"rolled armor {armor['id']!r} must be in the real catalog"

    if cat.armor_class is not None:
        # Content half landed: armor equips + AC derives from the real SRD value.
        assert armor["equipped"] is True
        assert char.core.armor_class == cat.armor_class
        assert char.core.armor_class > UNARMORED_AC
        assert _spans_named(otel_capture, "chargen.armor_equipped"), (
            "real-content path must emit chargen.armor_equipped"
        )
    else:
        # Content half not yet landed: must FAIL LOUD, never silent AC 10.
        assert _spans_named(otel_capture, "chargen.armor_class_missing"), (
            f"real armor {armor['id']!r} has no catalog armor_class — the step MUST "
            "fail loud (chargen.armor_class_missing span), not leave AC silently at 10. "
            "This is the content half of the two-sided gap."
        )
