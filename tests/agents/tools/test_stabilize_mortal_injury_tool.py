"""Tests for the stabilize_mortal_injury tool — Task 12 (CWN combat-lethality).

WRITE tool. The narrator calls stabilize_mortal_injury(actor, skill, attribute,
rounds_elapsed, roll) to resolve a CWN Heal check against difficulty
``8 + rounds_elapsed``. On success it clears the Mortal Injury Status (a Scar
attached by CwnRulesetModule.resolve_downed) and downgrades to a "Frail" Wound;
on failure it leaves the Mortal Injury in place.

Mirrors test_adjust_system_strain_tool.py: same duck-typed cwn pack fixtures,
same PgSaveRepository round-trip, same direct-handler invocation, plus a
registration check and a wiring dispatch round-trip.

OTEL note: the tool emits via ctx.otel_span.set_attribute (the dispatch span),
mirroring adjust_system_strain. We assert the Status mutation and the returned
payload; the d20 Heal-check face is passed in (``roll``), not rolled here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tools import (
    stabilize_mortal_injury as _stabilize_mortal_injury_module,  # noqa: F401
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import CwnConfig

# ---------------------------------------------------------------------------
# Attribute map required by CwnConfig
# ---------------------------------------------------------------------------

_AMAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}

_MORTAL_TEXT = "Mortal Injury — dies in 6 rounds unless stabilized"


# ---------------------------------------------------------------------------
# Minimal duck-typed pack with cwn rules
# ---------------------------------------------------------------------------


@dataclass
class _FakeRules:
    """Minimal rules object that acts like RulesConfig for a cwn pack."""

    ruleset: str = "cwn"
    _cwn_cfg: CwnConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._cwn_cfg is None:
            self._cwn_cfg = CwnConfig(attribute_map=_AMAP)

    def ruleset_config(self) -> CwnConfig:
        return self._cwn_cfg


@dataclass
class _FakePack:
    """Minimal duck-typed pack that exposes rules.ruleset and rules.ruleset_config()."""

    rules: _FakeRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _FakeRules()


@dataclass
class _NonCwnRules:
    ruleset: str = "native"

    def ruleset_config(self) -> None:
        return None


@dataclass
class _NonCwnPack:
    rules: _NonCwnRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _NonCwnRules()


@dataclass
class _FakeAwnRules:
    """Minimal rules object that acts like RulesConfig for an awn pack.

    AWN has the stabilize-at-0 Mortal Injury rule (SRD p.52) inherited from CWN,
    so stabilize_mortal_injury must accept it. The guard is capability-based
    (`isinstance(module, CwnRulesetModule)`), so `awn` is accepted.
    """

    ruleset: str = "awn"
    _awn_cfg: Any = None

    def __post_init__(self) -> None:
        if self._awn_cfg is None:
            from sidequest.genre.models.rules import AwnConfig

            self._awn_cfg = AwnConfig(attribute_map=_AMAP)

    def ruleset_config(self) -> Any:
        return self._awn_cfg


@dataclass
class _FakeAwnPack:
    rules: _FakeAwnRules = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _FakeAwnRules()


# ---------------------------------------------------------------------------
# Snapshot / character builders
# ---------------------------------------------------------------------------


def _cwn_character(name: str, *, mortal: bool = True) -> Character:
    core = CreatureCore(
        name=name,
        description="A neon-lit runner.",
        personality="cool",
        inventory=Inventory(),
        hp=HpPool(current=0, max=8, base_max=8),
        system_strain=SystemStrainPool(current=0, max=12, permanent=0),
    )
    if mortal:
        core.statuses.append(Status(text=_MORTAL_TEXT, severity=StatusSeverity.Scar))
    return Character(
        core=core,
        backstory="Street samurai for hire.",
        char_class="Warrior",
        race="Human",
    )


def _build_snapshot(characters: list[Character] | None = None) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="neon_dystopia",
        world_slug="franchise_nations",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=[],
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(
    store,
    *,
    session_id: str = "s",
    genre_pack: Any | None = None,
) -> ToolContext:
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Jax",
        turn_number=3,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["stabilize_mortal_injury"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_stabilize_mortal_injury_is_registered() -> None:
    assert "stabilize_mortal_injury" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy-path: passing Heal check clears Mortal Injury, adds Frail Wound
# ---------------------------------------------------------------------------


async def test_stabilize_success_clears_mortal_and_adds_frail() -> None:
    char = _cwn_character("Jax")
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    # force a passing Heal check (high d20 face vs difficulty 8+1=9)
    r = await _call(
        {
            "actor": "Jax",
            "skill": "Heal",
            "attribute": "Reflex",
            "rounds_elapsed": 1,
            "roll": 18,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["actor"] == "Jax"
    assert p["success"] is True
    assert p["difficulty"] == 9

    # Mutation persisted to the store
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Jax")
    assert core is not None
    assert not any("Mortal Injury" in s.text for s in core.statuses)
    assert any("Frail" in s.text for s in core.statuses)
    # the Frail downgrade is a (non-permanent) Wound, not a Scar
    frail = next(s for s in core.statuses if "Frail" in s.text)
    assert frail.severity is StatusSeverity.Wound


# ---------------------------------------------------------------------------
# Failure: failing Heal check leaves the Mortal Injury in place
# ---------------------------------------------------------------------------


async def test_stabilize_failure_keeps_mortal() -> None:
    char = _cwn_character("Jax")
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {
            "actor": "Jax",
            "skill": "Heal",
            "attribute": "Reflex",
            "rounds_elapsed": 1,
            "roll": 2,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["success"] is False

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Jax")
    assert core is not None
    assert any("Mortal Injury" in s.text for s in core.statuses)
    assert not any("Frail" in s.text for s in core.statuses)


# ---------------------------------------------------------------------------
# Story 88-1 Item 6: an AWN pack must be able to stabilize a Mortal Injury
# ---------------------------------------------------------------------------


async def test_awn_pack_can_stabilize() -> None:
    """AWN inherits the CWN stabilize-at-0 rule; the cwn-only guard must accept "awn"."""
    char = _cwn_character("Vane")
    snap = _build_snapshot(characters=[char])
    pack = _FakeAwnPack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {
            "actor": "Vane",
            "skill": "Heal",
            "attribute": "Reflex",
            "rounds_elapsed": 1,
            "roll": 18,  # passes difficulty 8+1=9
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK, (
        "stabilize_mortal_injury must accept an awn pack, not raise the cwn-only guard"
    )
    p = _payload(r)
    assert p["success"] is True
    assert p["difficulty"] == 9

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Vane")
    assert core is not None
    assert not any("Mortal Injury" in s.text for s in core.statuses)
    assert any("Frail" in s.text for s in core.statuses)


# ---------------------------------------------------------------------------
# Difficulty scales with rounds_elapsed (8 + rounds_elapsed)
# ---------------------------------------------------------------------------


async def test_difficulty_scales_with_rounds_elapsed() -> None:
    char = _cwn_character("Jax")
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    # roll 12 vs difficulty 8+5=13 → fails
    r = await _call(
        {
            "actor": "Jax",
            "skill": "Heal",
            "attribute": "Reflex",
            "rounds_elapsed": 5,
            "roll": 12,
        },
        ctx,
    )
    p = _payload(r)
    assert p["difficulty"] == 13
    assert p["success"] is False


# ---------------------------------------------------------------------------
# Guard: non-CWN pack raises ValueError (fail loud)
# ---------------------------------------------------------------------------


async def test_non_cwn_pack_raises() -> None:
    import pytest

    char = _cwn_character("Jax")
    snap = _build_snapshot(characters=[char])
    pack = _NonCwnPack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="cwn"):
        await _call(
            {
                "actor": "Jax",
                "skill": "Heal",
                "attribute": "Reflex",
                "rounds_elapsed": 1,
                "roll": 18,
            },
            ctx,
        )


# ---------------------------------------------------------------------------
# Guard: unknown actor returns NOT_FOUND
# ---------------------------------------------------------------------------


async def test_unknown_actor_returns_not_found() -> None:
    snap = _build_snapshot(characters=[])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {
            "actor": "Ghost",
            "skill": "Heal",
            "attribute": "Reflex",
            "rounds_elapsed": 1,
            "roll": 18,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Ghost" in r.message


# ---------------------------------------------------------------------------
# Guard: no active session returns fatal error
# ---------------------------------------------------------------------------


async def test_no_active_session_returns_fatal_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    pack = _FakePack()
    store = pg_empty_store(slug="stabilize-empty")
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {
            "actor": "Jax",
            "skill": "Heal",
            "attribute": "Reflex",
            "rounds_elapsed": 1,
            "roll": 18,
        },
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


# ---------------------------------------------------------------------------
# Wiring test: tool reachable through registry dispatch
# ---------------------------------------------------------------------------


async def test_wiring_dispatch_round_trip() -> None:
    """Verify the tool is reachable through the registry — not just registered."""
    import json

    from sidequest.agents.tooling_protocol import ToolUseBlock

    char = _cwn_character("Jax")
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="wiring-test", genre_pack=pack)

    block = ToolUseBlock(
        id="t-wiring",
        name="stabilize_mortal_injury",
        arguments={
            "actor": "Jax",
            "skill": "Heal",
            "attribute": "Reflex",
            "rounds_elapsed": 1,
            "roll": 18,
        },
    )
    out = await default_registry.dispatch(block, ctx)
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["success"] is True
    assert payload["difficulty"] == 9
