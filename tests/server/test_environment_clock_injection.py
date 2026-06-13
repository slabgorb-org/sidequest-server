"""Tests for the deterministic environment_clock injection (Task 3.2).

``inject_environment_clock`` appends a server-minted ``environment_clock``
dispatch into the DispatchPackage after the precondition gate and before the
bank, iff (a) a ``light`` pool exists AND (b) the package already contains a
time-advancing dispatch ({movement, confrontation}). The signal is derived from
the router's OWN emission, not from re-judging phrasing — phrasing cannot dodge
the burn.

The ``lit`` flag is read from the acting PC's cartography region. A region that
is absent from ``cartography.regions`` (e.g. beneath_sunden's procedurally
generated DEEP) defaults to ``lit=False`` (unlit → burns) — the intended
default, NOT a silent fallback.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.resource_pool import ResourcePool
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.server.intent_router_pass import (
    TIME_ADVANCING_SUBSYSTEMS,
    inject_environment_clock,
)

ACTING_PC = "Delver"


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def _pkg(*subsystems: str) -> DispatchPackage:
    return DispatchPackage(
        turn_id="t1",
        confidence_global=1.0,
        per_player=[
            PlayerDispatch(
                player_id="seat-1",
                raw_action="x",
                dispatch=[
                    SubsystemDispatch(
                        subsystem=s,
                        params={},
                        idempotency_key=f"k_{s}",
                        confidence=1.0,
                        visibility=_tag_all(),
                    )
                    for s in subsystems
                ],
            )
        ],
    )


def _snap_with_light(*, region: str = "entrance") -> GameSnapshot:
    snap = GameSnapshot()
    snap.world_slug = "beneath_sunden"
    snap.resources["light"] = ResourcePool(
        name="light",
        label="Light",
        current=6.0,
        min=0.0,
        max=6.0,
        voluntary=False,
        decay_per_turn=0.0,
    )
    # Seat a controllable PC core so find_creature_core(ACTING_PC) resolves and
    # region_for(perspective=ACTING_PC) reads its graph region.
    snap.characters.append(
        Character(
            core=CreatureCore(
                name=ACTING_PC,
                description="A torch-bearing delver.",
                personality="cautious",
                inventory=Inventory(),
            ),
            char_class="Fighter",
            race="Human",
            backstory="Descends into the dark.",
        )
    )
    snap.player_seats["seat-1"] = ACTING_PC
    snap.pc_regions[ACTING_PC] = region
    return snap


class _Region(dict):
    """Stub region exposing ``lit`` as a dict key (mirrors the test in the
    plan). The REAL runtime object is a pydantic ``world.Region`` with
    ``extra='allow'`` that exposes ``lit`` as an attribute — both paths are
    covered by ``inject_environment_clock``."""


class _Carto:
    regions = {"entrance": _Region(lit=False), "the_dropmouth": _Region(lit=True)}


class _World:
    cartography = _Carto()


class _Pack:
    worlds = {"beneath_sunden": _World()}


def test_injects_on_movement_turn():
    pkg, snap = _pkg("movement"), _snap_with_light()
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    kinds = [d.subsystem for pd in pkg.per_player for d in pd.dispatch]
    assert "environment_clock" in kinds
    clock = next(
        d for pd in pkg.per_player for d in pd.dispatch if d.subsystem == "environment_clock"
    )
    assert clock.params["lit"] is False
    assert clock.params["region"] == "entrance"
    assert clock.params["character_name"] == ACTING_PC
    assert clock.confidence == 1.0


def test_injects_on_confrontation_turn():
    pkg, snap = _pkg("confrontation"), _snap_with_light()
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    kinds = [d.subsystem for pd in pkg.per_player for d in pd.dispatch]
    assert "environment_clock" in kinds


def test_lit_region_injects_with_lit_true():
    pkg = _pkg("movement")
    snap = _snap_with_light(region="the_dropmouth")
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    clock = next(
        d for pd in pkg.per_player for d in pd.dispatch if d.subsystem == "environment_clock"
    )
    assert clock.params["lit"] is True


def test_absent_region_defaults_unlit():
    """beneath_sunden's procedurally generated DEEP is not in
    cartography.regions — the injector must DEFAULT lit=False (unlit → burns)."""
    pkg = _pkg("movement")
    snap = _snap_with_light(region="deep_generated_room_42")
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    clock = next(
        d for pd in pkg.per_player for d in pd.dispatch if d.subsystem == "environment_clock"
    )
    assert clock.params["lit"] is False
    assert clock.params["region"] == "deep_generated_room_42"


def test_lit_resolves_on_real_pydantic_region_attribute_branch():
    """Production path: the loaded ``world.Region`` (extra='allow') exposes the
    authored ``lit`` key as an ATTRIBUTE, not a dict key. The dict-subclass stub
    used elsewhere only covers ``_region_is_lit``'s dict branch; this test
    exercises the getattr branch with a real, non-dict pydantic Region so a
    regression in that branch (the live one) cannot slip through.
    """
    from sidequest.genre.models.world import Region

    lit_region = Region(
        name="The Dropmouth",
        summary="A daylit ledge.",
        description="Sun spills over the rim.",
        lit=True,  # lands in model_extra; exposed as an attribute (extra='allow')
    )
    # Sanity: this is the production shape — NOT a dict, ``lit`` is an attribute.
    assert not isinstance(lit_region, dict)
    assert lit_region.lit is True

    class _RealCarto:
        regions = {"the_dropmouth": lit_region}

    class _RealWorld:
        cartography = _RealCarto()

    class _RealPack:
        worlds = {"beneath_sunden": _RealWorld()}

    pkg = _pkg("movement")
    snap = _snap_with_light(region="the_dropmouth")
    inject_environment_clock(pkg, snap, _RealPack(), player_name=ACTING_PC)
    clock = next(
        d for pd in pkg.per_player for d in pd.dispatch if d.subsystem == "environment_clock"
    )
    assert clock.params["lit"] is True


def test_no_inject_on_pure_social_turn():
    pkg, snap = _pkg("npc_agency"), _snap_with_light()
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    kinds = [d.subsystem for pd in pkg.per_player for d in pd.dispatch]
    assert "environment_clock" not in kinds


def test_no_inject_without_light_pool():
    pkg = _pkg("movement")
    snap = GameSnapshot()
    snap.world_slug = "beneath_sunden"
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    kinds = [d.subsystem for pd in pkg.per_player for d in pd.dispatch]
    assert "environment_clock" not in kinds


def test_character_name_resolves_to_acting_pc_core_name():
    """character_name must be a value find_creature_core accepts (matches
    core.name). The pass passes player_name (the acting PC core name), NOT the
    seat player_id — passing the seat id would silently no-op the penalty."""
    pkg, snap = _pkg("movement"), _snap_with_light()
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    clock = next(
        d for pd in pkg.per_player for d in pd.dispatch if d.subsystem == "environment_clock"
    )
    name = clock.params["character_name"]
    assert snap.find_creature_core(name) is not None


def test_time_advancing_set_is_conservative():
    assert set(TIME_ADVANCING_SUBSYSTEMS) == {"movement", "confrontation"}


def test_idempotency_key_is_unique_and_survives_package_validation():
    """The minted key must not collide with sibling keys (DispatchPackage
    enforces uniqueness across per_player + cross_player)."""
    pkg, snap = _pkg("movement"), _snap_with_light()
    inject_environment_clock(pkg, snap, _Pack(), player_name=ACTING_PC)
    # Re-validate by round-tripping through the model validator.
    DispatchPackage.model_validate(pkg.model_dump())


# ---------------------------------------------------------------------------
# Wiring: the injector is actually CALLED inside the live pre-narrator pass.
# Drive execute_intent_router_pre_narrator_pass with a router that emits a
# time-advancing (confrontation) dispatch + a snapshot carrying a light pool,
# and assert the dispatch bank received and engaged an environment_clock tick
# (the light pool was burned). This proves the call site fires, not just that
# the function is defined.
# ---------------------------------------------------------------------------


async def _run_pass_with_confrontation_and_light():
    from unittest.mock import AsyncMock, MagicMock

    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        RulesConfig,
    )
    from sidequest.server.intent_router_pass import (
        execute_intent_router_pre_narrator_pass,
    )

    cdef = ConfrontationDef(
        type="negotiation",
        label="Negotiation",
        category="social",
        player_metric=MetricDef(name="leverage", starting=0, threshold=10),
        opponent_metric=MetricDef(name="leverage", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "press",
                    "label": "Press the Point",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "CHA",
                }
            )
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    pack.witnessed_acts = None
    # No cartography on this synthetic pack → the acting PC's region is absent,
    # so lit defaults to False (unlit → burns) — exactly the deep-region path.
    pack.worlds = {}

    snap = _snap_with_light()
    snap.genre_slug = "test_pack"

    package = DispatchPackage(
        turn_id="t-wire",
        confidence_global=1.0,
        per_player=[
            PlayerDispatch(
                player_id="seat-1",
                raw_action="I block his way and call the bluff.",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="confrontation",
                        params={"type": "negotiation"},
                        idempotency_key="k-conf-1",
                        confidence=1.0,
                        visibility=_tag_all(),
                    )
                ],
            )
        ],
    )

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, bank_result = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I block his way and call the bluff.",
        player_name=ACTING_PC,
    )
    return returned, bank_result, snap


def test_injector_fires_inside_live_pre_narrator_pass():
    import asyncio

    returned, bank_result, snap = asyncio.run(_run_pass_with_confrontation_and_light())

    # The injected dispatch rode into the package the bank consumed.
    kinds = [d.subsystem for pd in returned.per_player for d in pd.dispatch]
    assert "environment_clock" in kinds, (
        "inject_environment_clock must run inside the live pass between the "
        "precondition gate and run_dispatch_bank on a time-advancing turn"
    )

    # And it actually ENGAGED through the real bank: the light pool burned.
    clock_key = f"environment_clock_{snap.turn_manager.interaction}"
    assert clock_key in bank_result.outputs_by_key, (
        f"the bank must have executed the injected tick; outputs_by_key keys="
        f"{list(bank_result.outputs_by_key)}"
    )
    out = bank_result.outputs_by_key[clock_key]
    assert out.data["burned"] is True
    assert snap.resources["light"].current == 5.0  # burned one unit from 6.0
