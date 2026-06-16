"""Regression tests for playtest 2026-04-30 — Mira/Caverns Sünden
chargen-complete with no `magic.init` log.

Pre-fix flow:
1. Player starts a fresh `caverns_and_claudes` / `caverns_sunden`
   session.
2. Walks chargen (bone-dice stats, Delver class, opening, confirmation).
3. Clicks Create Character. Server log shows `chargen.starting_equipment`
   → `chargen.complete` with NO `magic.init` line in between (compare
   `space_opera`/`coyote_star` which logs `magic.init world=...
   plugins=['innate_v1', 'item_legacy_v1'] bars=4`).
4. Mira shows `Edge 10/10` and `No abilities.` in the UI — same surface
   as space_opera, no item-based magic differentiation.

Root cause:
- `init_magic_state_for_session` requires BOTH the genre-level
  `magic.yaml` AND `worlds/<world>/magic.yaml` to exist; absence at
  either tier silently returns False and skips magic init entirely.
- `caverns_and_claudes` had a draft genre `magic.yaml` in a
  non-canonical schema (everything nested under a `magic:` block,
  with `allowed_sources` carrying per-source plugin attribution).
  Even if the world yaml had existed, the loader would have rejected
  the genre yaml's shape.
- `caverns_and_claudes/worlds/caverns_sunden/` had NO `magic.yaml`
  file at all — the immediate bug.

Fix (content-only):
- Migrated `caverns_and_claudes/magic.yaml` to the canonical schema
  (matching `space_opera/magic.yaml` + `tests/magic/fixtures/
  space_opera_magic.yaml`). Original draft notes preserved as
  comments for future authoring reference.
- Created `caverns_and_claudes/worlds/caverns_sunden/magic.yaml`
  honoring the genre defaults (folkloric world_knowledge,
  item_legacy_v1 plugin, no character/world-scope bars — magic in
  C&C lives in items, instantiated by the plugin on pickup).

These tests verify the loader accepts both files end-to-end via
`init_magic_state_for_session` (the chargen-complete entry point) so
the bug doesn't regress on a future schema migration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.magic_loader import load_world_magic
from sidequest.server.magic_init import init_magic_state_for_session

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
CC_PACK = CONTENT_ROOT / "caverns_and_claudes"
SO_PACK = CONTENT_ROOT / "space_opera"


@pytest.fixture
def cc_pack_dir():
    if not CC_PACK.is_dir():
        pytest.skip("caverns_and_claudes content pack not found")
    return CC_PACK


@pytest.fixture
def so_pack_dir():
    if not SO_PACK.is_dir():
        pytest.skip("space_opera content pack not found")
    return SO_PACK


def test_caverns_and_claudes_genre_magic_yaml_loads_without_loader_error(cc_pack_dir):
    """The genre-tier yaml must parse via the canonical schema. Pre-fix
    it was nested under a `magic:` block and wouldn't have parsed even
    if the world yaml existed.
    """
    genre_yaml = cc_pack_dir / "magic.yaml"
    world_yaml = cc_pack_dir / "worlds" / "caverns_sunden" / "magic.yaml"
    assert genre_yaml.exists(), "C&C genre magic.yaml must exist"
    assert world_yaml.exists(), (
        "C&C caverns_sunden world magic.yaml must exist — "
        "init_magic_state_for_session requires both tiers"
    )
    # Should not raise.
    config = load_world_magic(genre_yaml=genre_yaml, world_yaml=world_yaml)
    assert config.world_slug == "caverns_sunden"
    assert config.genre_slug == "caverns_and_claudes"


def test_caverns_and_claudes_caverns_sunden_magic_init_fires(cc_pack_dir):
    """End-to-end: `init_magic_state_for_session` returns True for
    Mira's session shape and populates `snapshot.magic_state` with
    BOTH plugins active post-2026-05-07 B/X pivot (item_legacy_v1 +
    innate_v1).
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    result = init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Mira",
        character_class="Delver",
    )
    assert result is True, (
        "init_magic_state_for_session must return True for "
        "caverns_and_claudes/caverns_sunden — pre-fix returned "
        "False because the world yaml was missing"
    )
    assert snapshot.magic_state is not None
    assert snapshot.magic_state.config.world_slug == "caverns_sunden"
    assert snapshot.magic_state.config.genre_slug == "caverns_and_claudes"
    # Post-pivot: BOTH plugins are permitted/active for caverns_sunden.
    active = snapshot.magic_state.config.active_plugins
    assert "item_legacy_v1" in active
    assert "innate_v1" in active


def test_caverns_sunden_ships_character_scope_spell_slots_bar(cc_pack_dir):
    """Post-2026-05-07 B/X pivot: caverns_sunden ships character-scope
    ``spell_slots`` and (story 47-10) ``divine_favor`` bars with class-keyed
    ``starts_at_chargen``. A Delver-class chargen lands both at 0.0
    (non-caster, non-cleric); the bars exist so future caster commits
    see the same registry.
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Mira",
        character_class="Delver",
    )
    bars_by_id = {b.spec.id: b for b in snapshot.magic_state.ledger.values()}
    assert "spell_slots" in bars_by_id
    assert bars_by_id["spell_slots"].value == 0.0, (
        "Delver is a non-caster; spell_slots must seed at 0.0 per "
        "world magic.yaml class-keyed starts_at_chargen"
    )
    assert "divine_favor" in bars_by_id, (
        "Story 47-10: divine_favor character-scope bar must ship in caverns_sunden world magic.yaml"
    )
    assert bars_by_id["divine_favor"].value == 0.0, "Delver (non-Cleric) divine_favor seeds at 0.0"


# ---------------------------------------------------------------------------
# Story 47-10 — Init wiring for learned_v1 state (AC1)
# ---------------------------------------------------------------------------
# After init_magic_state_for_session runs for a Mage/Cleric in caverns_sunden,
# MagicState should be populated with the learned_v1 surface so that
# the prepared-list gate (AC4), context block (AC7), and cast resolution
# (AC5/AC6) can read it. Specifically:
#   1. MagicState.known_spells[actor] contains the actor's tradition's L1
#      catalog (12 arcane for Mage, 8 divine for Cleric).
#   2. MagicState.prepared_spells[actor] exists as an empty dict.
#   3. A per-level slot ledger bar exists for L1.


def test_mage_session_init_populates_known_spells_from_arcane_l1(cc_pack_dir):
    """A Mage chargen in caverns_sunden must end with all 12 arcane L1
    spells in known_spells[mage_id]. This is the seam where the prepared
    list (UI + context block) reads from."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Rux",
        character_class="Mage",
    )
    ms = snapshot.magic_state
    assert ms is not None
    known = ms.known_spells.get("Rux", [])
    assert len(known) == 12, (
        f"Mage 'Rux' must know all 12 arcane L1 spells after init; got "
        f"{len(known)}: {known!r}. Verify init_magic_state_for_session "
        f"calls seed_learned_v1_state for actors with magic_config."
    )
    # Spot-check canonical spells the playgroup will recognize:
    assert "magic_missile" in known
    assert "sleep" in known


def test_cleric_session_init_populates_known_spells_from_divine_l1(cc_pack_dir):
    """A Cleric chargen in caverns_sunden must end with all 8 divine L1
    spells in known_spells[cleric_id]."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Brother_Hesh",
        character_class="Cleric",
    )
    ms = snapshot.magic_state
    assert ms is not None
    known = ms.known_spells.get("Brother_Hesh", [])
    assert len(known) == 8, (
        f"Cleric 'Brother_Hesh' must know all 8 divine L1 spells after init; "
        f"got {len(known)}: {known!r}"
    )
    assert "cure_light_wounds" in known


def test_mage_session_init_creates_empty_prepared_spells_dict(cc_pack_dir):
    """prepared_spells must be present and empty after init. Empty dict
    (not absent) lets the prepared-list gate distinguish 'never prepared'
    from 'this actor has no MagicState'."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Rux",
        character_class="Mage",
    )
    ms = snapshot.magic_state
    prepared = ms.prepared_spells.get("Rux")
    assert prepared is not None, (
        "MagicState.prepared_spells['Rux'] must exist (empty dict) after init"
    )
    assert prepared == {} or all(not v for v in prepared.values()), (
        f"Prepared dict must be empty at chargen; got {prepared!r}"
    )


def test_mage_session_init_creates_l1_slot_bar(cc_pack_dir):
    """A Mage at chargen must have a per-level L1 slot bar for the
    cast_spell beat to drain. The bar's exact name is at the Dev's
    discretion (slots_l1_<actor> or spell_slots_l1_<actor>) — this test
    asserts existence + non-zero value, not naming.
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Rux",
        character_class="Mage",
    )
    ms = snapshot.magic_state
    # Find a per-level L1 slot bar for actor 'Rux'.
    l1_bars = [
        (key, bar)
        for key, bar in ms.ledger.items()
        if "Rux" in key and ("slots_l1" in key or "spell_slots_l1" in key)
    ]
    assert len(l1_bars) >= 1, (
        f"Expected a per-level L1 slot bar for Mage 'Rux' after init. "
        f"Found ledger keys: {list(ms.ledger.keys())!r}. The "
        f"seed_learned_v1_state helper creates slots_l<N> bars; "
        f"init_magic_state_for_session must invoke it for casters."
    )
    _key, bar = l1_bars[0]
    assert bar.value >= 1.0, (
        f"L1 slot bar must seed at >=1 for a Mage; got {bar.value}. "
        f"B/X canon: Magic-User L1 = 1 slot/day; spec amends to 2 — "
        f"either is acceptable; zero is not."
    )


# ---------------------------------------------------------------------------
# Story 47-10 — World magic.yaml: learned_v1 + divine_favor bar (AC3)
# ---------------------------------------------------------------------------
# The caverns_sunden world magic.yaml must:
#   1. Permit learned_v1 alongside item_legacy_v1 and innate_v1 (data-layer
#      activation so the spell-catalog loader fires).
#   2. Declare the divine_favor ledger bar (Cleric-class-scope, bidirectional,
#      thresholds at +/-0.7).


def test_caverns_sunden_active_plugins_include_learned_v1(cc_pack_dir):
    genre_yaml = cc_pack_dir / "magic.yaml"
    world_yaml = cc_pack_dir / "worlds" / "caverns_sunden" / "magic.yaml"
    config = load_world_magic(genre_yaml=genre_yaml, world_yaml=world_yaml)
    active = list(config.active_plugins)
    assert "item_legacy_v1" in active
    assert "innate_v1" in active
    assert "learned_v1" in active, (
        f"caverns_sunden must activate learned_v1 (data-layer infra) so "
        f"the spell-catalog loader binds arcane_l1.yaml + divine_l1.yaml "
        f"into MagicState. Currently active: {active!r}"
    )


def test_caverns_sunden_declares_divine_favor_ledger_bar(cc_pack_dir):
    """Cleric class-scope bar with bidirectional range and ±0.7 thresholds.

    The bar's downstream consequences (cleric cannot Turn when bar low,
    free reliquary effect when high) are narrator-discretion — the YAML
    only needs to declare the bar and its mechanical shape.
    """
    genre_yaml = cc_pack_dir / "magic.yaml"
    world_yaml = cc_pack_dir / "worlds" / "caverns_sunden" / "magic.yaml"
    config = load_world_magic(genre_yaml=genre_yaml, world_yaml=world_yaml)
    bars = [b for b in config.ledger_bars if b.id == "divine_favor"]
    assert len(bars) == 1, (
        f"Expected exactly one ledger bar with id 'divine_favor' in "
        f"caverns_sunden/magic.yaml; found {len(bars)}. Bar IDs: "
        f"{[b.id for b in config.ledger_bars]!r}"
    )
    bar = bars[0]
    assert bar.direction == "bidirectional", (
        f"divine_favor must be bidirectional (acts of faith raise it; "
        f"betrayals lower it); got {bar.direction!r}"
    )
    # Bar range / threshold shape — exact field names are at the
    # LedgerBarSpec author's discretion (range, threshold_high/low). The
    # contract: thresholds at ±0.7 (cleric drift bands).
    bar_dump = bar.model_dump()
    range_value = bar_dump.get("range") or [bar_dump.get("range_min"), bar_dump.get("range_max")]
    assert range_value is not None and len(range_value) == 2
    assert float(range_value[0]) <= -0.7 and float(range_value[1]) >= 0.7, (
        f"divine_favor range must span [-1.0, 1.0] or wider so the ±0.7 "
        f"thresholds are reachable; got {range_value!r}"
    )


def test_non_caster_session_init_does_not_create_known_spells(cc_pack_dir):
    """A Fighter or Thief chargen must NOT populate known_spells —
    seed_learned_v1_state is gated on ClassDef.magic_config, which
    fighter/thief do not declare."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Sam",
        character_class="Fighter",
    )
    ms = snapshot.magic_state
    assert ms is not None
    assert ms.known_spells.get("Sam", []) == [], (
        "Fighter must not have known_spells populated after init"
    )


def test_caverns_sunden_class_aware_spell_slot_allocation(cc_pack_dir):
    """B/X canon at L1: Magic-User gets 1 slot/day; Cleric gets 0
    (casting begins L2); Fighter and Thief get 0 (non-casters);
    Delver gets 0 (pre-class default). The class-keyed
    ``starts_at_chargen`` dict in caverns_sunden/magic.yaml encodes
    these values; ``add_character`` resolves them by
    ``character.char_class`` (display-cased).
    """
    expected = {
        "Mage": 1.0,
        "Cleric": 0.0,
        "Fighter": 0.0,
        "Thief": 0.0,
        "Delver": 0.0,
    }
    for char_class, expected_value in expected.items():
        snapshot = GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="caverns_sunden",
            turn_manager=TurnManager(),
        )
        init_magic_state_for_session(
            snapshot=snapshot,
            genre_pack_source_dir=cc_pack_dir,
            world_slug="caverns_sunden",
            character_id=f"Test_{char_class}",
            character_class=char_class,
        )
        bars_by_id = {b.spec.id: b for b in snapshot.magic_state.ledger.values()}
        assert "spell_slots" in bars_by_id, (
            f"{char_class!r} session must have a spell_slots bar; got {list(bars_by_id.keys())!r}"
        )
        assert bars_by_id["spell_slots"].value == expected_value, (
            f"{char_class!r} expected spell_slots={expected_value}, "
            f"got {bars_by_id['spell_slots'].value}"
        )


def test_caverns_sunden_mage_cast_routes_to_spell_slots_bar(cc_pack_dir):
    """Playtest 2026-05-08 regression: a magic_working with cost_type
    `spell_slots` MUST debit the `spell_slots` ledger bar end-to-end.

    Pre-fix, world cost_types_active listed `slots` while the bar id
    was `spell_slots`; the engine routes by ``BarKey(scope, owner,
    bar_id=cost_type)`` so a `slots` cost found no matching bar, fell
    through to the world/item-scope-pending guard, and emitted a
    `magic.unrouted_cost` warning. Every Mage cast was a no-op against
    the ledger — the textbook SOUL.md illusionism failure.

    The fix aligns cost_type with bar id (`spell_slots` everywhere).
    This test asserts that alignment by exercising the routing path
    end-to-end against the shipped content (no synthetic fixture).
    """
    from sidequest.magic.models import MagicWorking

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=cc_pack_dir,
        world_slug="caverns_sunden",
        character_id="Ponder",
        character_class="Mage",
    )

    # Pre-cast: B/X L1 Mage starts with 1 prepared spell slot.
    spell_slots_key = next(iter(snapshot.magic_state.ledger))
    assert snapshot.magic_state.ledger[spell_slots_key].value == 1.0

    working = MagicWorking(
        plugin="innate_v1",
        mechanism="native",
        actor="Ponder",
        costs={"spell_slots": 1.0},
        domain="elemental",
        narrator_basis="Mage casts the prepared Light spell",
        flavor="acquired",
        consent_state="willing",
    )
    result = snapshot.magic_state.apply_working(working)

    # Post-cast: the slot is gone, the bar dropped 1.0 → 0.0, and the
    # routing actually engaged (bar_changes is non-empty — a routed
    # cost; an unrouted cost would skip the assignment and leave
    # bar_changes empty with a `magic.unrouted_cost` warning).
    assert "spell_slots" in result.bar_changes, (
        "spell_slots cost must route to the spell_slots bar — empty "
        "bar_changes means the cost dropped silently (the playtest bug)"
    )
    prev, post = result.bar_changes["spell_slots"]
    assert prev == 1.0
    assert post == 0.0


def test_caverns_sunden_cost_types_match_character_scope_bar_ids(cc_pack_dir):
    """Engine convention: ``MagicState.apply_working`` looks up bars by
    ``BarKey(scope, owner, bar_id=cost_type)``. For every character-scope
    bar a world ships, the bar's id MUST appear in cost_types — else a
    narrator-emitted cost against that bar will drop silently.

    Authored cost_types may legitimately exceed character bar ids when
    the cost targets world/item scope (engine routing for those scopes
    is pending; ``magic.state.apply_working`` logs `magic.unrouted_cost`
    for them). The reverse direction is the bug: an authored character
    bar with no matching cost_type means there's no narrator-side path
    to reach it. This test pins that direction.
    """
    config = load_world_magic(
        genre_yaml=cc_pack_dir / "magic.yaml",
        world_yaml=cc_pack_dir / "worlds" / "caverns_sunden" / "magic.yaml",
    )
    character_bar_ids = {b.id for b in config.ledger_bars if b.scope == "character"}
    cost_type_set = set(config.cost_types)
    missing = character_bar_ids - cost_type_set
    assert not missing, (
        f"caverns_sunden ships character-scope bar(s) {sorted(missing)} "
        f"with no matching cost_type; narrator-emitted costs cannot reach "
        f"them. Add to cost_types_active in worlds/caverns_sunden/magic.yaml."
    )


def test_caverns_sunden_unknown_class_raises_loudly(cc_pack_dir):
    """No silent fallback: if a character is committed with a class
    that isn't in the world's ``starts_at_chargen`` dict, ``add_character``
    raises ValueError. Production callers (websocket_session_handler /
    connect handler) cannot reach this state because Character validates
    char_class at build time, but the schema enforcement here catches
    YAML/class-list drift early.
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    with pytest.raises(ValueError, match=r"missing from starts_at_chargen"):
        init_magic_state_for_session(
            snapshot=snapshot,
            genre_pack_source_dir=cc_pack_dir,
            world_slug="caverns_sunden",
            character_id="Test_Bard",
            character_class="Bard",  # not in classes.yaml; not in dict
        )


def test_caverns_sunden_missing_class_param_raises_loudly(cc_pack_dir):
    """Class-keyed ``starts_at_chargen`` requires the caller to pass
    ``character_class``. Omitting it raises ValueError — defends against
    a future caller forgetting to thread the class through (the bug this
    whole change exists to fix).
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="caverns_sunden",
        turn_manager=TurnManager(),
    )
    with pytest.raises(ValueError, match=r"no character_class was supplied"):
        init_magic_state_for_session(
            snapshot=snapshot,
            genre_pack_source_dir=cc_pack_dir,
            world_slug="caverns_sunden",
            character_id="Mira",
            # character_class intentionally omitted
        )


def test_caverns_and_claudes_intensity_and_world_knowledge(cc_pack_dir):
    """Smoke: the migration preserved C&C's authored values
    (folkloric world-knowledge, hard limits) — guards against
    accidental shape drift if the yaml is touched again. Note:
    intensity moved from 0.3 (genre default) to 0.4 (world layer)
    in the 2026-05-07 B/X pivot to reflect the addition of the
    caster surface.
    """
    config = load_world_magic(
        genre_yaml=cc_pack_dir / "magic.yaml",
        world_yaml=cc_pack_dir / "worlds" / "caverns_sunden" / "magic.yaml",
    )
    assert config.intensity == 0.4
    assert config.world_knowledge.primary == "folkloric"
    assert "components" in config.cost_types
    assert "backlash" in config.cost_types
    # B/X spell-slot debits route by `cost_type == bar_id` (engine convention
    # at magic/state.py); cost_type name must match the `spell_slots` ledger
    # bar id or every cast drops silently (playtest 2026-05-08).
    assert "spell_slots" in config.cost_types
    # The pre-fix cost_type name `slots` is gone — keep the regression pinned.
    assert "slots" not in config.cost_types
    # Hard limits authored as five entries — guard count + a known id.
    assert len(config.hard_limits) == 5
    limit_ids = {hl.id for hl in config.hard_limits}
    assert "no_resurrection" in limit_ids


def test_space_opera_magic_init_still_fires(so_pack_dir):
    """Companion: ensure the C&C migration didn't accidentally regress
    the space_opera path. The Coyote Star world must load the four
    authored bars (sanity / notice / vitality / hegemony_heat).
    """
    world_slug = "coyote_star"
    assert (so_pack_dir / "worlds" / world_slug / "magic.yaml").is_file(), (
        f"coyote_star magic.yaml missing under {so_pack_dir / 'worlds'}"
    )
    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug=world_slug,
        turn_manager=TurnManager(),
    )
    result = init_magic_state_for_session(
        snapshot=snapshot,
        genre_pack_source_dir=so_pack_dir,
        world_slug=world_slug,
        character_id="Parsley",
    )
    assert result is True
    assert snapshot.magic_state is not None
    # Coyote Star: 1 world-scope bar (hegemony_heat) + 3 character-
    # scope bars (sanity/notice/vitality) seeded for Parsley.
    assert len(snapshot.magic_state.ledger) == 4
