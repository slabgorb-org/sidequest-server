"""Tests for the adjust_system_strain tool — Task 7 (CWN System Strain production caller).

WRITE tool. The narrator calls adjust_system_strain(actor, kind, amount, source)
to apply CWN System Strain rules to a character. This is the thin tool wrapper
that resolves the actor's CreatureCore from the snapshot and delegates to
CwnRulesetModule.apply_system_strain — all rules live in the engine method.

OTEL note: The tool does NOT accept a tracer kwarg (ToolContext has no tracer slot).
We rely on Task 5's tests (test_cwn_system_strain.py) for OTEL span coverage.
Instead we assert the engine effect on the pool and the StrainResult-derived
payload returned by the tool.
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
    adjust_system_strain as _adjust_system_strain_module,  # noqa: F401
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
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
class _FakeAwnRules:
    """Minimal rules object that acts like RulesConfig for an awn pack.

    AWN uses System Strain inherited from CWN (stims, mutations, first-aid), so
    adjust_system_strain must accept it. The current guard (`ruleset != "cwn"`)
    blocks it (Item 7); the fix loosens to the capability form.
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


# ---------------------------------------------------------------------------
# Snapshot / character builders
# ---------------------------------------------------------------------------


def _cwn_character(
    name: str,
    *,
    strain_current: int = 0,
    strain_max: int = 12,
    strain_permanent: int = 0,
) -> Character:
    core = CreatureCore(
        name=name,
        description="A neon-lit runner.",
        personality="cool",
        inventory=Inventory(),
        hp=HpPool(current=8, max=8, base_max=8),
        system_strain=SystemStrainPool(
            current=strain_current,
            max=strain_max,
            permanent=strain_permanent,
        ),
    )
    return Character(
        core=core,
        backstory="Street samurai for hire.",
        char_class="Warrior",
        race="Human",
    )


def _build_snapshot(
    characters: list[Character] | None = None,
) -> GameSnapshot:
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
    registered = default_registry._tools["adjust_system_strain"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_adjust_system_strain_is_registered() -> None:
    assert "adjust_system_strain" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy-path: temporary add within max
# ---------------------------------------------------------------------------


async def test_temporary_add_within_max_applies() -> None:
    char = _cwn_character("Jax", strain_current=0, strain_max=12)
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {"actor": "Jax", "kind": "temporary", "amount": 3, "source": "adrenal_boost"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["actor"] == "Jax"
    assert p["applied"] is True
    assert p["current"] == 3
    assert p["max"] == 12
    assert p["delta"] == 3

    # Mutation persisted to the store
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Jax")
    assert core is not None
    assert core.system_strain is not None
    assert core.system_strain.current == 3


# ---------------------------------------------------------------------------
# Refusal: add over max leaves pool unchanged
# ---------------------------------------------------------------------------


async def test_temporary_add_over_max_is_refused() -> None:
    char = _cwn_character("Jax", strain_current=10, strain_max=12)
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {"actor": "Jax", "kind": "temporary", "amount": 5, "source": "overclock"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["applied"] is False
    assert p["current"] == 10
    assert p["delta"] == 0
    # reason describes the refusal
    assert "max" in p["reason"]

    # Pool unchanged in store
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Jax")
    assert core is not None
    assert core.system_strain is not None
    assert core.system_strain.current == 10


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
            {"actor": "Jax", "kind": "temporary", "amount": 1, "source": "x"},
            ctx,
        )


# ---------------------------------------------------------------------------
# Guard: unknown actor returns NOT_FOUND
# ---------------------------------------------------------------------------


async def test_awn_pack_applies_strain() -> None:
    """Story 88-1 Item 7: an AWN pack must apply System Strain (it inherits the
    mechanic from CWN). The cwn-only guard must loosen to accept "awn".
    """
    char = _cwn_character("Vane", strain_current=0, strain_max=12)
    snap = _build_snapshot(characters=[char])
    pack = _FakeAwnPack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {"actor": "Vane", "kind": "temporary", "amount": 3, "source": "stimpack"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK, (
        "adjust_system_strain must accept an awn pack, not raise the cwn-only guard"
    )
    p = _payload(r)
    assert p["applied"] is True
    assert p["current"] == 3
    assert p["delta"] == 3

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Vane")
    assert core is not None
    assert core.system_strain is not None
    assert core.system_strain.current == 3


async def test_unknown_actor_returns_not_found() -> None:
    snap = _build_snapshot(characters=[])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {"actor": "Ghost", "kind": "temporary", "amount": 1, "source": "x"},
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
    store = pg_empty_store(slug="strain-empty")
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        {"actor": "Jax", "kind": "temporary", "amount": 1, "source": "x"},
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

    char = _cwn_character("Jax", strain_current=2, strain_max=12)
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="wiring-test", genre_pack=pack)

    block = ToolUseBlock(
        id="t-wiring",
        name="adjust_system_strain",
        arguments={"actor": "Jax", "kind": "rest", "amount": 1, "source": "night_rest"},
    )
    out = await default_registry.dispatch(block, ctx)
    assert out.is_error is False
    payload = json.loads(out.content)
    assert payload["applied"] is True
    # rest recovers: current 2 → 1 (down by rest_recovery_per_night=1, floor permanent=0)
    assert payload["current"] == 1
