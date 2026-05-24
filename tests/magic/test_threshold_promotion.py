"""Threshold crossings auto-promote into status_changes (Task 3.4).

The pipeline reads ``promote_to_status`` from the per-bar
``LedgerBarSpec`` (world content, not engine code) so different worlds
can map the same bar id to different status text/severity. A bar that
omits ``promote_to_status`` produces no promotion (silent skip is the
intended behavior — not every bar surfaces as a Status).

The wiring test at the bottom exercises the full
``_apply_narration_result_to_snapshot`` pipeline and asserts that an
auto-promoted threshold crossing actually lands as a
``Status`` on the actor's ``core.statuses`` list — proving the
promotion is reachable from production code paths
(CLAUDE.md "Verify Wiring, Not Just Existence").
"""

from __future__ import annotations

import pytest

from sidequest.magic.models import HardLimit, WorldMagicConfig


@pytest.fixture()
def coyote_world_config(world_config: WorldMagicConfig) -> WorldMagicConfig:
    """Conftest world_config with extra hard limit (matches sibling test pattern)."""
    augmented = list(world_config.hard_limits) + [
        HardLimit(id="no_resurrection", description="death is permanent"),
    ]
    return world_config.model_copy(update={"hard_limits": augmented})


def test_sanity_low_crossing_adds_bleeding_through_wound(coyote_world_config):
    from sidequest.game.session import GameSnapshot
    from sidequest.magic.state import BarKey, MagicState
    from sidequest.server.narration_apply import (
        apply_magic_working,
        promote_crossings_to_status_changes,
    )

    state = MagicState.from_config(coyote_world_config)
    state.add_character("sira_mendes")
    state.set_bar_value(BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity"), 0.45)

    snapshot = GameSnapshot.model_construct(magic_state=state)
    result = apply_magic_working(
        snapshot=snapshot,
        patch_field={
            "plugin": "innate_v1",
            "mechanism": "condition",
            "actor": "sira_mendes",
            "costs": {"sanity": 0.10},
            "domain": "psychic",
            "narrator_basis": "x",
            "flavor": "acquired",
            "consent_state": "involuntary",
        },
    )

    promotions = promote_crossings_to_status_changes(result=result, snapshot=snapshot)
    assert len(promotions) == 1
    assert promotions[0].actor == "sira_mendes"
    assert "Bleeding" in promotions[0].status_text
    assert promotions[0].severity == "Wound"


def test_notice_high_crossing_adds_quiet_word_wound(coyote_world_config):
    from sidequest.game.session import GameSnapshot
    from sidequest.magic.state import BarKey, MagicState
    from sidequest.server.narration_apply import (
        apply_magic_working,
        promote_crossings_to_status_changes,
    )

    state = MagicState.from_config(coyote_world_config)
    state.add_character("sira_mendes")
    state.set_bar_value(BarKey(scope="character", owner_id="sira_mendes", bar_id="notice"), 0.70)

    snapshot = GameSnapshot.model_construct(magic_state=state)
    result = apply_magic_working(
        snapshot=snapshot,
        patch_field={
            "plugin": "item_legacy_v1",
            "mechanism": "discovery",
            "actor": "sira_mendes",
            "costs": {"notice": 0.10},
            "domain": "physical",
            "narrator_basis": "named gun's last shot in a quiet alley",
            "item_id": "lassiter",
            "alignment_with_item_nature": 0.85,
        },
    )

    promotions = promote_crossings_to_status_changes(result=result, snapshot=snapshot)
    assert any(
        "Quiet Word" in p.status_text or "noticed" in p.status_text.lower() for p in promotions
    )


def test_no_crossings_no_promotions(coyote_world_config):
    from sidequest.game.session import GameSnapshot
    from sidequest.magic.state import MagicState
    from sidequest.server.narration_apply import (
        apply_magic_working,
        promote_crossings_to_status_changes,
    )

    state = MagicState.from_config(coyote_world_config)
    state.add_character("sira_mendes")
    snapshot = GameSnapshot.model_construct(magic_state=state)
    result = apply_magic_working(
        snapshot=snapshot,
        patch_field={
            "plugin": "innate_v1",
            "mechanism": "condition",
            "actor": "sira_mendes",
            "costs": {"sanity": 0.05},  # no crossing (1.0 -> 0.95)
            "domain": "psychic",
            "narrator_basis": "x",
            "flavor": "acquired",
            "consent_state": "involuntary",
        },
    )

    assert promote_crossings_to_status_changes(result=result, snapshot=snapshot) == []


def test_bar_without_promote_to_status_silently_skipped(world_config):
    """A bar that lacks ``promote_to_status`` produces zero promotions for its
    crossings — silent skip is the intended behavior per architect §5.3.

    Build a stripped config where ``sanity`` has NO ``promote_to_status``
    set; cross its threshold; assert promotions == [].
    """
    from sidequest.game.session import GameSnapshot
    from sidequest.magic.models import LedgerBarSpec
    from sidequest.magic.state import BarKey, MagicState
    from sidequest.server.narration_apply import (
        apply_magic_working,
        promote_crossings_to_status_changes,
    )

    # Override sanity bar to have NO promote_to_status.
    bare_sanity = LedgerBarSpec(
        id="sanity",
        scope="character",
        direction="down",
        range=(0.0, 1.0),
        threshold_low=0.40,
        consequence_on_low_cross="auto-fire The Bleeding-Through",
        starts_at_chargen=1.0,
        # promote_to_status omitted intentionally
    )
    other_bars = [b for b in world_config.ledger_bars if b.id != "sanity"]
    stripped = world_config.model_copy(update={"ledger_bars": [bare_sanity] + other_bars})
    state = MagicState.from_config(stripped)
    state.add_character("sira_mendes")
    state.set_bar_value(BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity"), 0.45)
    snapshot = GameSnapshot.model_construct(magic_state=state)

    result = apply_magic_working(
        snapshot=snapshot,
        patch_field={
            "plugin": "innate_v1",
            "mechanism": "condition",
            "actor": "sira_mendes",
            "costs": {"sanity": 0.10},
            "domain": "psychic",
            "narrator_basis": "x",
            "flavor": "acquired",
            "consent_state": "involuntary",
        },
    )

    # Crossing happened…
    assert len(result.crossings) == 1
    # …but no status promotion (silent skip is correct).
    assert promote_crossings_to_status_changes(result=result, snapshot=snapshot) == []


@pytest.mark.asyncio
async def test_pipeline_wires_promotion_into_character_statuses(coyote_world_config):
    """Wiring test (CLAUDE.md): the dispatch handler must end-to-end add the
    auto-promoted Status onto the rolling actor's ``core.statuses``.

    Story 59-5 moved magic engagement from the narration_apply sidecar
    consumer to the dispatch handler. The threshold promotion chain
    (apply_magic_working → _apply_magic_status_promotions) now fires
    inside ``run_magic_working_dispatch``.
    """
    from sidequest.agents.subsystems.magic_working import run_magic_working_dispatch
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore
    from sidequest.game.session import GameSnapshot
    from sidequest.game.status import StatusSeverity
    from sidequest.game.turn import TurnManager
    from sidequest.magic.state import BarKey, MagicState
    from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

    state = MagicState.from_config(coyote_world_config)
    state.add_character("sira_mendes")
    state.set_bar_value(BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity"), 0.45)

    snapshot = GameSnapshot.model_construct(magic_state=state)
    snapshot.turn_manager = TurnManager()
    snapshot.characters = [
        Character.model_construct(
            core=CreatureCore.model_construct(name="sira_mendes", statuses=[]),
            backstory="x",
            char_class="pilot",
            race="human",
        )
    ]
    snapshot.encounter = None

    dispatch = SubsystemDispatch(
        subsystem="magic_working",
        params={
            "plugin": "innate_v1",
            "mechanism": "condition",
            "actor": "sira_mendes",
            "costs": {"sanity": 0.10},  # 0.45 -> 0.35 crosses 0.40
            "domain": "psychic",
            "narrator_basis": "x",
            "flavor": "acquired",
            "consent_state": "involuntary",
        },
        idempotency_key="k-magic-promo",
        visibility=VisibilityTag(visible_to="all"),
    )

    await run_magic_working_dispatch(
        dispatch,
        snapshot=snapshot,
        player_name="Sira",
    )

    target = next(c for c in snapshot.characters if c.core.name == "sira_mendes")
    statuses = target.core.statuses
    bleeding = [s for s in statuses if "Bleeding" in s.text and s.severity == StatusSeverity.Wound]
    assert len(bleeding) == 1, (
        f"expected one auto-promoted 'Bleeding through' Wound, got {statuses!r}"
    )
