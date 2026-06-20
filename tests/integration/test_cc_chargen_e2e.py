"""End-to-end caverns_and_claudes chargen integration test.

The wiring gate required by sidequest-content/CLAUDE.md:
"Every Test Suite Needs a Wiring Test." Verifies the full path —
load real pack → walk all WWN scenes → produce a Character with class,
kit, and archetype-resolution all flowing through correctly.

WWN port (2026-06-12): caverns moved off the B/X visible-3d6 roll/arrange flow
to a point-buy chassis (rules.yaml stat_generation: point_buy), leaving a
4-scene flow (calling → story → kit → mouth) with the three WWN Callings
(Warrior/Expert/Mage). There is no the_roll / the_arrangement scene and no
edge_config (HP comes from the WWN chassis). The walk mirrors the generic
point-buy walk used for heavy_metal / elemental_harmony.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.builder import (
    CharacterBuilder,
    FreeformInput,
    SceneResult,
    StoryInput,
    qualifying_classes,
)
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models import MechanicalEffects
from sidequest.server.dispatch.equipment_tables_resolve import resolve_equipment_tables

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture
def cc_pack():
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _drive_chargen(pack, *, target_class: str, name: str = "Wiring", world_slug: str | None = None):
    """Walk the WWN 4-scene point-buy flow, picking the named Calling.

    Choice-bearing scenes select the choice whose class_hint matches; non-class
    choice-scenes (e.g. the_trade's six background choices) pick the first option;
    other scenes auto-advance (the_kit/the_mouth) or capture identity (the_story).
    If no scene offered the target class_hint, inject it as a late SceneResult.

    When ``world_slug`` is given the equipment tables are world-merged over genre
    via the production ``resolve_equipment_tables`` (mirrors connect.py chargen),
    so world-tier guaranteed_grants (the beneath_sunden heal potion) are applied.
    Left ``None`` the build uses the pure-genre WWN baseline.
    """
    equipment_tables = pack.equipment_tables
    if world_slug is not None:
        equipment_tables = resolve_equipment_tables(pack, world_slug)
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
        )
        .with_lobby_name(name)
        .with_equipment_tables(equipment_tables)
        .with_classes(pack.classes)
    )

    matched = False
    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 50, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup(name)
            continue
        scene = builder.current_scene()
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_response(
                    StoryInput(
                        pronouns="she/her",
                        background="Raised in the caverns.",
                        description="Tall, scarred, watchful.",
                    )
                )
            continue
        idx = next(
            (
                i
                for i, c in enumerate(scene.choices)
                if c.mechanical_effects and c.mechanical_effects.class_hint == target_class
            ),
            None,
        )
        if idx is None:
            # A non-class choice-scene (e.g. the_trade's six background choices,
            # which carry background/focus_id/skill_grants but no class_hint).
            # Pick the first choice to advance — the background does not affect
            # class, kit, archetype, or class_moves.
            idx = 0
        else:
            matched = True
        builder.apply_choice(idx)

    if not matched:
        builder._results.append(
            SceneResult(
                input_type=FreeformInput(text=""),
                effects_applied=MechanicalEffects(class_hint=target_class),
            )
        )
    return builder


def test_e2e_chargen_produces_classed_warrior(cc_pack):
    builder = _drive_chargen(cc_pack, target_class="Warrior")
    character = builder.build("Wiring")

    assert character.char_class == "Warrior"
    # WWN chassis seeds the ablative HP pool (no edge_config).
    assert character.core.hp.max >= 1
    assert character.core.hp.current == character.core.hp.max


def test_e2e_warrior_kit_always_includes_exactly_one_heal_potion(cc_pack):
    """Story 106-4 Part B: every Warrior starts with EXACTLY one heal potion —
    base ``potion_healing`` or (30%) the upgraded ``potion_healing_greater`` —
    never zero, never two. The guaranteed_grants primitive makes the kit
    deterministic so the beat-scan (Part C) tests against a known heal, and
    removes the old ~30%-of-Warriors-start-empty coin flip (playtest finding).

    The Potion of Mending has no WWN SRD analog, so after the 120-1/120-4 verbatim
    sweep (ADR-140/145) the heal guarantee is WORLD-tier bespoke
    (worlds/beneath_sunden/equipment_tables.yaml), merged over the WWN-pure genre
    baseline (which grants nothing). This drives chargen with the world-merged
    tables — exactly as production does (connect.py → resolve_equipment_tables) —
    so it exercises the real guarantee, not the heal-less genre baseline.

    25 fresh rolls: pre-fix the random consumable pool gave a heal only ~1-in-3.
    """
    heal_ids = {"potion_healing", "potion_healing_greater"}
    for i in range(25):
        builder = _drive_chargen(
            cc_pack, target_class="Warrior", name=f"W{i}", world_slug="beneath_sunden"
        )
        character = builder.build("Wiring")
        ids = [it["id"] for it in character.core.inventory.items]
        heals = [x for x in ids if x in heal_ids]
        assert len(heals) == 1, f"warrior {i} should get exactly one heal, got {heals} in {ids}"


def test_e2e_chargen_produces_classed_mage(cc_pack):
    builder = _drive_chargen(cc_pack, target_class="Mage")
    character = builder.build("Wiring")
    assert character.char_class == "Mage"
    assert character.core.hp.current == character.core.hp.max
    # mage_kit resolves to equipment; items come only from that kit.
    assert len(character.core.inventory.items) > 0
    rolled_ids = {i["id"] for i in character.core.inventory.items}
    mage_kit = cc_pack.equipment_tables.class_tables["mage_kit"]
    mage_items = {i for items in mage_kit.values() for i in items}
    # Story 106-4 Part B: guaranteed_grants add a heal potion (base or upgrade)
    # outside the random slot lists — include those ids in the allowed set.
    for grant in cc_pack.equipment_tables.guaranteed_grants.get("mage_kit", []):
        mage_items.add(grant.item)
        if grant.upgrade:
            mage_items.add(grant.upgrade)
    assert rolled_ids.issubset(mage_items), (
        f"Items {rolled_ids - mage_items} leaked from outside mage_kit"
    )
    # Mage kit has no armor.
    assert mage_kit.get("armor", []) == []


def test_e2e_archetype_resolution_gate_passes(cc_pack):
    """Story 45-6's archetype-resolution gate requires both jungian_hint and
    rpg_role_hint populated. The Mage Calling sets magician/control (a valid
    pairing in archetype_constraints.yaml)."""
    builder = _drive_chargen(cc_pack, target_class="Mage")
    acc = builder.accumulated()
    assert acc.jungian_hint == "magician"
    assert acc.rpg_role_hint == "control"
    character = builder.build("Wiring")
    assert character.resolved_archetype == "magician/control"


def test_e2e_qualifying_classes_observable_from_pack(cc_pack):
    """Smoke check: the public API surface for class qualification is
    reachable and behaves correctly with real WWN pack data. All three
    Callings share minimum_score 9 (point-buy), so a baseline-9 character
    qualifies for every Calling."""
    stats = {"STR": 9, "DEX": 9, "CON": 9, "INT": 9, "WIS": 9, "CHA": 9}
    qual = qualifying_classes(stats, cc_pack.classes)
    assert len(qual) == 3
    assert {c.id for c in qual} == {"warrior", "expert", "mage"}
