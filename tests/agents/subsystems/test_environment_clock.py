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
from sidequest.game.status import StatusSeverity
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


def _give_torch(snap: GameSnapshot, character_name: str, *, charges: int) -> None:
    """Seat a real torch item in the PC's real inventory structure.

    Mirrors the runtime item dict produced by chargen loadout
    (``chargen_loadout._item_dict_from_catalog``): a genuine light source
    carrying the dedicated ``light_source`` tag with a ``quantity`` field. The
    relight path treats ``quantity`` as the charge count (one item = one relight
    to max), so ``charges`` seeds ``quantity``. The ``light`` tag is also
    present (the content torch keeps it) but the matcher keys on
    ``light_source`` so light-WEIGHT weapons are never eaten as fuel.
    """
    core = snap.find_creature_core(character_name)
    assert core is not None
    core.inventory.items.append(
        {
            "id": "torch",
            "name": "Torch",
            "description": "A pitch-soaked bundle of rags on a stick.",
            "category": "light",
            "tags": ["light", "light_source", "consumable", "essential"],
            "equipped": False,
            "quantity": charges,
            "uses_remaining": 6,
            "state": "Carried",
        }
    )


def _relight(character_name: str = "Delver") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="environment_clock",
        params={"mode": "relight", "character_name": character_name},
        idempotency_key="environment_clock_relight_2",
        confidence=0.9,
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
async def test_darkness_penalty_is_not_a_wound_and_stamps_current_turn():
    """153-31: the environmental darkness penalty is a cosmetic ambient status,
    not a bodily injury — it must NOT tier as ``Wound`` — and it stamps the real
    current turn (``turn_manager.interaction``), not the implicit ``created_turn=0``.
    The −2 ``roll_modifier`` and ``source`` are unchanged (cosmetic-fields-only)."""
    snap = _snap_with_light(1.0)
    snap.turn_manager.interaction = 3  # apply the penalty on a real (non-zero) turn
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)
    core = snap.find_creature_core("Delver")
    assert core is not None
    dark = [s for s in core.statuses if s.source == DARKNESS_STATUS_SOURCE]
    assert len(dark) == 1
    status = dark[0]
    # AC-1: no longer an injury tier; carries the lightest non-injury, scene-bounded tier.
    assert status.severity != StatusSeverity.Wound
    assert status.severity is StatusSeverity.Scratch
    # AC-2: created_turn reflects the real turn at application time, not 0.
    assert status.created_turn == 3
    # AC-3: the mechanical effect is unchanged.
    assert status.roll_modifier == DARKNESS_PENALTY
    assert status.source == DARKNESS_STATUS_SOURCE


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
async def test_relight_sets_light_to_max_consumes_torch_and_clears_penalty():
    snap = _snap_with_light(0.0)  # dark
    await run_environment_clock_dispatch(_dispatch(lit=False), snapshot=snap)  # penalty on
    _give_torch(snap, "Delver", charges=1)
    out = await run_environment_clock_dispatch(_relight("Delver"), snapshot=snap)
    assert snap.resources["light"].current == snap.resources["light"].max
    core = snap.find_creature_core("Delver")
    assert core is not None
    assert not [s for s in core.statuses if s.text == DARKNESS_STATUS_TEXT]
    assert out.data["relit"] is True
    assert out.data["torch_charges_remaining"] == 0


@pytest.mark.asyncio
async def test_relight_fails_loudly_with_no_torch():
    snap = _snap_with_light(0.0)
    out = await run_environment_clock_dispatch(_relight("Delver"), snapshot=snap)
    assert out.data["error"] == "no_torch"
    assert snap.resources["light"].current == 0.0  # unchanged — no silent fallback


@pytest.mark.asyncio
async def test_relight_decrements_quantity_when_multiple_torches():
    """A kit with several torches relights and reports the remaining count;
    the torch item is not removed until the last charge is consumed."""
    snap = _snap_with_light(0.0)
    _give_torch(snap, "Delver", charges=3)
    out = await run_environment_clock_dispatch(_relight("Delver"), snapshot=snap)
    assert out.data["relit"] is True
    assert out.data["torch_charges_remaining"] == 2
    core = snap.find_creature_core("Delver")
    assert core is not None
    torch = next(it for it in core.inventory.items if it.get("id") == "torch")
    assert torch["quantity"] == 2


@pytest.mark.asyncio
async def test_relight_does_not_burn_light():
    """The relight branch sets light to max and must not also burn a unit."""
    snap = _snap_with_light(0.0)
    _give_torch(snap, "Delver", charges=1)
    out = await run_environment_clock_dispatch(_relight("Delver"), snapshot=snap)
    assert out.data["burned"] is False
    assert snap.resources["light"].current == snap.resources["light"].max


@pytest.mark.asyncio
async def test_relight_never_consumes_a_light_weight_weapon():
    """Regression (content-review SEV): the ``light`` tag is overloaded — a
    light-WEIGHT weapon (``dagger_iron``: tags ``[melee, blade, one-handed,
    light]``) is NOT a light source. The relight matcher keys on the dedicated
    ``light_source`` tag, so a dagger is never consumed/destroyed as torch fuel
    once the real torch runs out. This proves the fix WITHOUT depending on
    content: a no-light_source weapon ⇒ ``no_torch`` and the weapon is untouched.

    (If the matcher is reverted to ``"light" in tags``, the dagger is found,
    relight succeeds, and these assertions fail.)
    """
    snap = _snap_with_light(0.0)
    core = snap.find_creature_core("Delver")
    assert core is not None
    dagger = {
        "id": "dagger_iron",
        "name": "Iron Dagger",
        "category": "weapon",
        "tags": ["melee", "blade", "one-handed", "light"],  # "light" = light-WEIGHT
        "equipped": True,
        "quantity": 1,
        "state": "Carried",
    }
    core.inventory.items.append(dagger)

    out = await run_environment_clock_dispatch(_relight("Delver"), snapshot=snap)

    assert out.data["error"] == "no_torch"
    assert snap.resources["light"].current == 0.0  # not relit
    # The weapon survives untouched — still carried, quantity unchanged.
    survivors = [it for it in core.inventory.items if it.get("id") == "dagger_iron"]
    assert len(survivors) == 1
    assert survivors[0]["quantity"] == 1


@pytest.mark.asyncio
async def test_relight_finds_torch_among_light_weight_weapons():
    """A genuine light source (``light_source`` tag) is found even when a
    light-WEIGHT weapon sharing the bare ``light`` tag sits in the same
    inventory; only the torch is consumed."""
    snap = _snap_with_light(0.0)
    core = snap.find_creature_core("Delver")
    assert core is not None
    core.inventory.items.append(
        {
            "id": "dagger_iron",
            "name": "Iron Dagger",
            "category": "weapon",
            "tags": ["melee", "blade", "one-handed", "light"],
            "equipped": True,
            "quantity": 1,
            "state": "Carried",
        }
    )
    _give_torch(snap, "Delver", charges=1)

    out = await run_environment_clock_dispatch(_relight("Delver"), snapshot=snap)

    assert out.data["relit"] is True
    assert out.data["torch_charges_remaining"] == 0
    # Torch (last charge) removed; dagger untouched.
    assert not [it for it in core.inventory.items if it.get("id") == "torch"]
    daggers = [it for it in core.inventory.items if it.get("id") == "dagger_iron"]
    assert len(daggers) == 1 and daggers[0]["quantity"] == 1


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
