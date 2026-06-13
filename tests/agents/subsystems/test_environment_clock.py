"""Tests for the environment_clock subsystem (Task 3.1).

Deterministic per-beat survival-clock burn of the ``light`` pool with a
state-reconciled darkness penalty. Mechanism deviates from spec §6.1 (no
exploration-beat taxonomy exists); the intent — undodgeable, deterministic
burn and an idempotent, resume-safe darkness penalty — is what these tests
pin.
"""

from __future__ import annotations

import pytest

from sidequest.agents.subsystems import SubsystemOutput, get_registered
from sidequest.agents.subsystems.environment_clock import (
    DARKNESS_PENALTY,
    DARKNESS_STATUS_SOURCE,
    DARKNESS_STATUS_TEXT,
    run_environment_clock_dispatch,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.resource_pool import ResourcePool, ResourceThreshold
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def _snap_with_light(current: float, character_name: str = "Delver") -> GameSnapshot:
    snap = GameSnapshot()
    snap.resources["light"] = ResourcePool(
        name="light",
        label="Light",
        current=current,
        min=0.0,
        max=6.0,
        voluntary=False,
        decay_per_turn=0.0,
        thresholds=[
            ResourceThreshold(at=1.0, event_id="guttering", narrator_hint="the torch is dying"),
            ResourceThreshold(at=0.0, event_id="dark", narrator_hint="the dark closes in"),
        ],
    )
    # Seat a controllable PC core named ``character_name`` so
    # ``find_creature_core`` resolves it (mirrors tests/conftest.py).
    snap.characters.append(
        Character(
            core=CreatureCore(
                name=character_name,
                description="A torch-bearing delver.",
                personality="cautious",
                inventory=Inventory(),
            ),
            char_class="Fighter",
            race="Human",
            backstory="Descends into the dark.",
        )
    )
    return snap


def _dispatch(
    *, region: str = "entrance", lit: bool = False, character_name: str = "Delver"
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="environment_clock",
        params={"region": region, "lit": lit, "character_name": character_name},
        idempotency_key="environment_clock_1",
        confidence=1.0,
        visibility=_tag_all(),
    )


def test_environment_clock_is_registered():
    """Wiring: the dispatcher's registry resolves ``environment_clock``."""
    assert "environment_clock" in get_registered()


@pytest.mark.asyncio
async def test_burn_decrements_one_in_unlit_region():
    snap = _snap_with_light(6.0)
    out = await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)
    assert isinstance(out, SubsystemOutput)
    assert snap.resources["light"].current == 5.0
    assert out.data["burned"] is True


@pytest.mark.asyncio
async def test_no_burn_in_lit_region():
    snap = _snap_with_light(6.0)
    out = await run_environment_clock_dispatch(_dispatch(lit=True), snapshot=snap)
    assert snap.resources["light"].current == 6.0
    assert out.data["burned"] is False


@pytest.mark.asyncio
async def test_reaching_zero_applies_darkness_penalty_once():
    snap = _snap_with_light(1.0)
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)
    core = snap.find_creature_core("Delver")
    assert core is not None
    dark = [s for s in core.statuses if s.text == DARKNESS_STATUS_TEXT]
    assert len(dark) == 1
    assert dark[0].roll_modifier == DARKNESS_PENALTY  # -2

    # Idempotent: a second tick at the floor does not stack a second penalty.
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)
    dark = [s for s in core.statuses if s.text == DARKNESS_STATUS_TEXT]
    assert len(dark) == 1


@pytest.mark.asyncio
async def test_penalty_cleared_when_region_lit():
    snap = _snap_with_light(0.0)
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)  # applies -2
    await run_environment_clock_dispatch(_dispatch(lit=True), snapshot=snap)  # lit ⇒ clear
    core = snap.find_creature_core("Delver")
    assert core is not None
    assert not [s for s in core.statuses if s.text == DARKNESS_STATUS_TEXT]


@pytest.mark.asyncio
async def test_starts_unlit_at_zero_applies_penalty_without_a_crossing():
    """Spec §12: a region entered already at 0 light gets the penalty
    immediately — there is no downward crossing to detect, so reconcile
    against state (not crossing-edge) is what makes this fire."""
    snap = _snap_with_light(0.0)
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)
    core = snap.find_creature_core("Delver")
    assert core is not None
    dark = [s for s in core.statuses if s.text == DARKNESS_STATUS_TEXT]
    assert len(dark) == 1
    assert dark[0].roll_modifier == DARKNESS_PENALTY


@pytest.mark.asyncio
async def test_missing_light_pool_returns_error_data():
    snap = GameSnapshot()
    out = await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)
    assert out.directives == []
    assert out.data["error"] == "no_light_pool"


@pytest.mark.asyncio
async def test_unresolved_character_is_surfaced_not_silently_skipped():
    """No Silent Fallbacks: a character_name that matches no seated PC still
    burns light but surfaces the miss in data rather than silently skipping
    the penalty reconcile."""
    snap = _snap_with_light(1.0, character_name="Delver")
    out = await run_environment_clock_dispatch(
        _dispatch(lit=False, character_name="Nobody"), snapshot=snap
    )
    assert out.data["character_unresolved"] == "Nobody"
    assert out.data["burned"] is True
    # Light still burned; no penalty applied to the real PC (it wasn't the target).
    assert snap.resources["light"].current == 0.0
    core = snap.find_creature_core("Delver")
    assert core is not None
    assert not [s for s in core.statuses if s.source == DARKNESS_STATUS_SOURCE]


@pytest.mark.asyncio
async def test_reconcile_keys_on_source_not_text():
    """The reconcile clears by structured source, so a pre-existing combat
    wound that shares the darkness wording is NOT removed when the region is lit."""
    snap = _snap_with_light(0.0)
    core = snap.find_creature_core("Delver")
    assert core is not None
    # A look-alike wound with the same display text but no machine source.
    from sidequest.game.status import Status, StatusSeverity

    core.statuses.append(
        Status(text=DARKNESS_STATUS_TEXT, severity=StatusSeverity.Wound, roll_modifier=-1)
    )
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)  # applies -2
    await run_environment_clock_dispatch(_dispatch(lit=True), snapshot=snap)  # clears source
    # The environment penalty is gone; the look-alike combat wound survives.
    sourced = [s for s in core.statuses if s.source == DARKNESS_STATUS_SOURCE]
    look_alikes = [s for s in core.statuses if s.text == DARKNESS_STATUS_TEXT and s.source is None]
    assert sourced == []
    assert len(look_alikes) == 1
