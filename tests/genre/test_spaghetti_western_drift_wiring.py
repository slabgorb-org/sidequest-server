"""Schema-drift wiring tests for the spaghetti_western pack.

When spaghetti_western was promoted from ``genre_workshopping/`` to
``genre_packs/`` (sidequest-content #237, 2026-05-19), 10 authored YAML
fields tripped strict pydantic ``extra_forbidden`` errors. Triage at
``docs/content-drift-triage.md`` routed 9 to ``wire`` (typed pass-through
+ TODO consumer) and 1 to ``prose`` (inert pass-through on ``Prompts``).

These tests prove every field round-trips into the typed pydantic shape
expected by future consumers, and that the pack still loads via the
normal pack-discovery flow.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.progression import AffinityTier, AffinityUnlocks
from sidequest.genre.models.rules import (
    LuckRecovery,
    LuckRules,
    LuckSpendEffect,
    ReputationEffects,
    ReputationFaction,
    StandoffPhase,
)
from tests._helpers.genre_paths import find_pack_path


def _has_real_content() -> bool:
    try:
        find_pack_path("spaghetti_western")
    except Exception:
        return False
    return True


@pytest.fixture(scope="module")
def sw_pack() -> GenrePack:
    if not _has_real_content():
        pytest.skip("sidequest-content/spaghetti_western not on disk")
    return load_genre_pack(find_pack_path("spaghetti_western"))


# ---------------------------------------------------------------------------
# Wiring test (mandatory per CLAUDE.md): proves spaghetti_western loads
# through normal pack-discovery without raising. This is the load-bearing
# test that proves the pack is no longer drift-blocked.
# ---------------------------------------------------------------------------


def test_spaghetti_western_loads_via_pack_discovery(sw_pack: GenrePack) -> None:
    """Normal-flow pack load must succeed and return a populated GenrePack."""
    assert sw_pack is not None
    assert sw_pack.rules is not None
    assert sw_pack.progression is not None
    assert sw_pack.prompts is not None


# ---------------------------------------------------------------------------
# Per-field assertions: one per drift-row in the triage doc.
# ---------------------------------------------------------------------------


def test_standoff_rules_typed_as_phase_map(sw_pack: GenrePack) -> None:
    """rules.yaml > standoff_rules → dict[str, StandoffPhase] with required description."""
    phases = sw_pack.rules.standoff_rules
    assert set(phases.keys()) == {"sizing_up", "focus_or_draw", "nerve_break"}
    for name, phase in phases.items():
        assert isinstance(phase, StandoffPhase), f"phase {name} not typed"
        assert phase.description, f"phase {name} missing description"


def test_reputation_factions_typed_list(sw_pack: GenrePack) -> None:
    """rules.yaml > reputation_factions → list[ReputationFaction]."""
    factions = sw_pack.rules.reputation_factions
    assert factions, "no reputation factions loaded"
    ids = {f.id for f in factions}
    assert {"outlaws", "law", "merchants", "guns_for_hire", "power_brokers"} <= ids
    for f in factions:
        assert isinstance(f, ReputationFaction)
        assert f.name and f.description


def test_reputation_effects_typed_bands(sw_pack: GenrePack) -> None:
    """rules.yaml > reputation_effects → ReputationEffects with high/neutral/low prose."""
    effects = sw_pack.rules.reputation_effects
    assert isinstance(effects, ReputationEffects)
    assert effects.high, "high-band reputation effects empty"
    assert effects.neutral, "neutral-band reputation effects empty"
    assert effects.low, "low-band reputation effects empty"


def test_luck_rules_typed_with_spend_menu(sw_pack: GenrePack) -> None:
    """rules.yaml > luck_rules → LuckRules with spend_effects + recovery."""
    luck = sw_pack.rules.luck_rules
    assert isinstance(luck, LuckRules)
    assert luck.starting_luck == 3
    assert luck.max_luck == 5
    assert luck.spend_effects, "spend_effects empty"
    for entry in luck.spend_effects:
        assert isinstance(entry, LuckSpendEffect)
        assert entry.cost >= 1
        assert entry.name and entry.effect
    assert isinstance(luck.recovery, LuckRecovery)
    assert luck.recovery.per_session == 1
    assert luck.recovery.bonus_triggers


def test_char_creation_reputation_bonus_pass_through(sw_pack: GenrePack) -> None:
    """char_creation.yaml > mechanical_effects.reputation_bonus → str pass-through.

    Wired as an Optional[str] on ``MechanicalEffects`` already. This test
    asserts at least one scene exposes the field with a real value, so a
    consumer story can later map ``reputation_bonus`` → starting-reputation
    delta on the proper faction.
    """
    seen_values: list[str] = []
    for scene in sw_pack.char_creation:
        for choice in scene.choices:
            if (
                choice.mechanical_effects is not None
                and choice.mechanical_effects.reputation_bonus is not None
            ):
                seen_values.append(choice.mechanical_effects.reputation_bonus)
    assert seen_values, "no reputation_bonus values authored — drift row not exercised"
    # Spot-check at least one of the four documented values surfaces.
    assert any(v in {"intimidation", "stealth", "network", "notoriety"} for v in seen_values)


def test_progression_unlocks_named_tier_access(sw_pack: GenrePack) -> None:
    """progression.yaml > affinities[N].unlocks.{novice,journeyman,expert,master}.

    Each named tier must be typed as ``AffinityTier`` and reachable both via
    its named attribute and via the dynamic ``.tiers`` dict (cross-pack
    reuse contract). The "The Gun" affinity is the canonical example.
    """
    gun = next(a for a in sw_pack.progression.affinities if a.name == "The Gun")
    unlocks = gun.unlocks
    assert isinstance(unlocks, AffinityUnlocks)

    # Named-attribute access
    assert isinstance(unlocks.novice, AffinityTier)
    assert unlocks.novice.name == "Steady Hand"
    assert isinstance(unlocks.journeyman, AffinityTier)
    assert unlocks.journeyman.name == "Dead Eye"
    assert isinstance(unlocks.expert, AffinityTier)
    assert unlocks.expert.name == "Legendary Shot"
    assert isinstance(unlocks.master, AffinityTier)
    assert unlocks.master.name == "The Fastest"

    # Generic dict access (cross-pack reuse path)
    assert set(unlocks.tiers.keys()) == {"novice", "journeyman", "expert", "master"}
    for tier in unlocks.tiers.values():
        assert isinstance(tier, AffinityTier)


def test_progression_unlocks_numbered_convention_still_works() -> None:
    """elemental_harmony numbered ``tier_0..tier_3`` convention must still load.

    Regression guard: the named-tier additions on AffinityUnlocks must not
    break the original numbered convention.
    """
    try:
        eh_path = find_pack_path("elemental_harmony")
    except Exception:
        pytest.skip("sidequest-content/elemental_harmony not on disk")
    pack = load_genre_pack(eh_path)
    affinity = pack.progression.affinities[0]
    assert affinity.unlocks is not None
    assert isinstance(affinity.unlocks.tier_0, AffinityTier)


def test_session_opener_template_prose_passthrough(sw_pack: GenrePack) -> None:
    """prompts.yaml > session_opener_template → Optional[str] on Prompts.

    Decision: ``prose`` pass-through (no consumer wiring). Confirm the
    field round-trips intact so a future opener-pipeline change can
    reach for it via the existing Prompts model.
    """
    assert sw_pack.prompts is not None
    template = sw_pack.prompts.session_opener_template
    assert template is not None
    # Spot-check load-bearing content rides through verbatim
    assert "sun sits high" in template
    assert "{world_specific_opener}" in template
