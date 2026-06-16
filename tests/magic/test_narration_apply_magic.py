"""End-to-end: narrator emits magic_working → server applies."""

from __future__ import annotations

import pytest

from sidequest.magic.models import HardLimit, WorldMagicConfig


@pytest.fixture()
def coyote_world_config(world_config: WorldMagicConfig) -> WorldMagicConfig:
    """Conftest world_config + ``no_resurrection`` hard limit.

    Derived from the canonical conftest fixture via ``model_copy`` rather
    than re-declaring every bar — that way any future change to the shared
    Coyote Star config in ``tests/magic/conftest.py`` flows through
    automatically. We add ``no_resurrection`` so the validator's keyword
    detector (id → "resurrection") matches the test 2 narrator_basis
    "resurrection of the dead pilot via psychic touch"; the conftest's
    ``psionics_never_decisive`` limit alone would not match.
    """
    augmented = list(world_config.hard_limits) + [
        HardLimit(id="no_resurrection", description="death is permanent"),
    ]
    return world_config.model_copy(update={"hard_limits": augmented})


@pytest.fixture
def coyote_snapshot(coyote_world_config: WorldMagicConfig):
    from sidequest.game.session import GameSnapshot
    from sidequest.magic.state import MagicState

    state = MagicState.from_config(coyote_world_config)
    state.add_character("sira_mendes")
    return GameSnapshot.model_construct(magic_state=state)


def test_apply_magic_working_clean_pass(coyote_snapshot):
    """Clean working: ledger updates, no flags."""
    from sidequest.magic.state import BarKey
    from sidequest.server.narration_apply import apply_magic_working

    patch_field = {
        "plugin": "innate_v1",
        "mechanism": "condition",
        "actor": "sira_mendes",
        "costs": {"sanity": 0.12},
        "domain": "psychic",
        "narrator_basis": "alien-tech proximity triggers reflexive sympathetic-feel",
        "flavor": "acquired",
        "consent_state": "involuntary",
    }
    result = apply_magic_working(snapshot=coyote_snapshot, patch_field=patch_field)

    assert result.flags == []
    sanity = coyote_snapshot.magic_state.get_bar(
        BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity")
    )
    assert sanity.value == pytest.approx(0.88)


def test_apply_magic_working_deep_red_flagged(coyote_snapshot):
    """Hard-limit violation: ledger still updates but result.flags carries DEEP_RED."""
    from sidequest.magic.models import FlagSeverity
    from sidequest.server.narration_apply import apply_magic_working

    patch_field = {
        "plugin": "innate_v1",
        "mechanism": "condition",
        "actor": "sira_mendes",
        "costs": {"sanity": 0.12},
        "domain": "psychic",
        "narrator_basis": "resurrection of the dead pilot via psychic touch",
        "flavor": "acquired",
        "consent_state": "involuntary",
    }
    result = apply_magic_working(snapshot=coyote_snapshot, patch_field=patch_field)

    # Couple to the specific hard limit, not just "any DEEP_RED" — the
    # validator stamps the flag's reason as
    # f"hard_limit_violation:{limit.id}", so a future regression that
    # triggers a different DEEP_RED path won't pass this assertion.
    assert any(
        f.severity == FlagSeverity.DEEP_RED and f.reason == "hard_limit_violation:no_resurrection"
        for f in result.flags
    )


def test_apply_magic_working_malformed_patch_raises_parse_error(coyote_snapshot):
    from sidequest.server.narration_apply import (
        MagicWorkingParseError,
        apply_magic_working,
    )

    patch_field = {"plugin": "innate_v1"}  # missing required fields
    with pytest.raises(MagicWorkingParseError):
        apply_magic_working(snapshot=coyote_snapshot, patch_field=patch_field)


def test_apply_magic_working_returns_threshold_crossings(coyote_snapshot):
    from sidequest.magic.state import BarKey
    from sidequest.server.narration_apply import apply_magic_working

    # Pre-set sanity to 0.45 so a 0.10 cost crosses 0.40
    coyote_snapshot.magic_state.set_bar_value(
        BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity"), 0.45
    )

    patch_field = {
        "plugin": "innate_v1",
        "mechanism": "condition",
        "actor": "sira_mendes",
        "costs": {"sanity": 0.10},
        "domain": "psychic",
        "narrator_basis": "x",
        "flavor": "acquired",
        "consent_state": "involuntary",
    }
    result = apply_magic_working(snapshot=coyote_snapshot, patch_field=patch_field)

    assert len(result.crossings) == 1
    assert "Bleeding-Through" in result.crossings[0].consequence


def test_apply_magic_working_no_magic_state_raises_parse_error():
    """Snapshot without magic_state must fail loudly per CLAUDE.md no-silent-fallback."""
    from sidequest.game.session import GameSnapshot
    from sidequest.server.narration_apply import (
        MagicWorkingParseError,
        apply_magic_working,
    )

    snapshot = GameSnapshot.model_construct(magic_state=None)
    patch_field = {
        "plugin": "innate_v1",
        "mechanism": "condition",
        "actor": "sira_mendes",
        "costs": {"sanity": 0.10},
        "domain": "psychic",
        "narrator_basis": "x",
        "flavor": "acquired",
        "consent_state": "involuntary",
    }
    with pytest.raises(MagicWorkingParseError):
        apply_magic_working(snapshot=snapshot, patch_field=patch_field)


@pytest.mark.asyncio
async def test_dispatch_handler_invokes_apply_magic_working(coyote_snapshot):
    """Wiring test (CLAUDE.md): the dispatch handler must actually call
    apply_magic_working when the router dispatches a magic_working.

    Story 59-5 retired the narration_apply sidecar consumer; magic
    engagement now routes through the dispatch handler pre-narrator.
    """
    from sidequest.agents.subsystems.magic_working import run_magic_working_dispatch
    from sidequest.magic.state import BarKey
    from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

    dispatch = SubsystemDispatch(
        subsystem="magic_working",
        params={
            "plugin": "innate_v1",
            "mechanism": "condition",
            "actor": "sira_mendes",
            "costs": {"sanity": 0.05},
            "domain": "psychic",
            "narrator_basis": "x",
            "flavor": "acquired",
            "consent_state": "involuntary",
        },
        idempotency_key="k-magic-test",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )

    await run_magic_working_dispatch(
        dispatch,
        snapshot=coyote_snapshot,
        player_name="Sira",
    )

    sanity = coyote_snapshot.magic_state.get_bar(
        BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity")
    )
    assert sanity.value == pytest.approx(0.95)
