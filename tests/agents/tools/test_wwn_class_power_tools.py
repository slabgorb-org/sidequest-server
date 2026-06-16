"""Tests for WWN narrator WRITE tools: commit_effort, veterans_luck, long_rest.

TDD: this file is written BEFORE the tools exist. RED pass first, then GREEN.

These three tools are thin wrappers over WwnRulesetModule methods, exactly
mirroring the CWN adjust_system_strain pattern. The tests verify:
- Happy-path: the engine effect fires and the correct ToolResult payload is returned.
- Refusal: over-commit / second Veteran's Luck returns applied=False, pool unchanged.
- WWN-only guard: non-wwn pack → ValueError (fail loud).
- Actor not found → NOT_FOUND.
- No active session → ERROR_FATAL.
- Registry: all three tool names are registered in default_registry.
- Long rest: day/scene Effort reclaimed, casts refreshed, comfortable=False leaves day
  committed when day_reclaim_requires_comfort is True.
- Long rest (reprepare): new prepared list set and validated against catalog.
- Long rest (reprepare): unknown spell id → ValueError (fail loud, no silent fallback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)

# Import tool modules so they self-register at import time.
from sidequest.agents.tools import (  # noqa: F401
    commit_effort as _commit_effort_module,
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.turn import TurnManager
from sidequest.game.wwn_magic import EffortPool, SpellcastingState
from sidequest.genre.models.rules import WwnConfig
from sidequest.genre.models.wwn_spell import WwnSpell, WwnSpellCatalog

# ---------------------------------------------------------------------------
# WWN attribute map (shared by all tests)
# ---------------------------------------------------------------------------

_AMAP = {
    "STRENGTH": "Might",
    "CONSTITUTION": "Vigor",
    "DEXTERITY": "Grace",
    "INTELLIGENCE": "Lore",
    "WISDOM": "Wit",
    "CHARISMA": "Bearing",
}

# ---------------------------------------------------------------------------
# Fake pack fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeRules:
    """Minimal rules object acting like RulesConfig for a wwn pack."""

    ruleset: str = "wwn"
    _wwn_cfg: WwnConfig = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._wwn_cfg is None:
            self._wwn_cfg = WwnConfig(attribute_map=_AMAP)

    def ruleset_config(self) -> WwnConfig:
        return self._wwn_cfg


@dataclass
class _FakePack:
    """Minimal duck-typed wwn pack."""

    rules: _FakeRules = None  # type: ignore[assignment]
    wwn_spell_catalog: WwnSpellCatalog | None = None
    # Epic 94 world-first catalog resolution: resolve_wwn_spell_catalog reads
    # pack.worlds first. This fixture exercises the genre-tier path, so the
    # bound world ships no catalog → empty worlds map falls through to the
    # genre-tier wwn_spell_catalog above.
    worlds: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _FakeRules()


@dataclass
class _NonWwnRules:
    ruleset: str = "native"

    def ruleset_config(self) -> None:
        return None


@dataclass
class _NonWwnPack:
    rules: _NonWwnRules = None  # type: ignore[assignment]
    wwn_spell_catalog: WwnSpellCatalog | None = None

    def __post_init__(self) -> None:
        if self.rules is None:
            self.rules = _NonWwnRules()


# ---------------------------------------------------------------------------
# Character / snapshot builders
# ---------------------------------------------------------------------------


def _wwn_character(
    name: str,
    *,
    effort_source: str | None = "channeler",
    effort_max: int = 3,
    spellcasting: SpellcastingState | None = None,
    strain_current: int = 0,
    strain_max: int = 12,
) -> Character:
    effort: dict[str, EffortPool] = {}
    if effort_source is not None:
        effort[effort_source] = EffortPool(source=effort_source, max=effort_max)
    core = CreatureCore(
        name=name,
        description="An elemental channeler.",
        personality="calm",
        inventory=Inventory(),
        hp=HpPool(current=8, max=8, base_max=8),
        system_strain=SystemStrainPool(
            current=strain_current,
            max=strain_max,
            permanent=0,
        ),
        effort=effort,
        spellcasting=spellcasting,
    )
    return Character(
        core=core,
        backstory="Touched by the elements.",
        char_class="Channeler",
        race="Human",
    )


def _build_snapshot(
    characters: list[Character] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="verdant_expanse",
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
        perspective_pc="Kael",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
    )


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


async def _call(tool_name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools[tool_name]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


# ===========================================================================
# REGISTRATION TESTS
# ===========================================================================


def test_commit_effort_is_registered() -> None:
    assert "commit_effort" in default_registry.list_names()


def test_veterans_luck_is_registered() -> None:
    assert "veterans_luck" in default_registry.list_names()


def test_long_rest_is_registered() -> None:
    assert "long_rest" in default_registry.list_names()


# ===========================================================================
# commit_effort TESTS
# ===========================================================================


async def test_commit_effort_valid_commit_decrements_available() -> None:
    char = _wwn_character("Kael", effort_source="channeler", effort_max=3)
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "commit_effort",
        {
            "actor": "Kael",
            "source": "channeler",
            "points": 2,
            "duration": "scene",
            "label": "wind_wall",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["applied"] is True
    assert p["available"] == 1  # 3 - 2 committed
    assert p["source"] == "channeler"

    # Mutation persisted
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Kael")
    assert core is not None
    pool = core.effort["channeler"]
    assert pool.available == 1


async def test_commit_effort_over_commit_is_refused() -> None:
    char = _wwn_character("Kael", effort_source="channeler", effort_max=2)
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "commit_effort",
        {"actor": "Kael", "source": "channeler", "points": 5, "duration": "scene", "label": ""},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["applied"] is False
    assert "available" in p["reason"] or "Effort" in p["reason"]

    # Pool unchanged
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Kael")
    assert core is not None
    assert core.effort["channeler"].available == 2


async def test_commit_effort_non_wwn_pack_raises() -> None:
    char = _wwn_character("Kael")
    snap = _build_snapshot(characters=[char])
    pack = _NonWwnPack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="wwn"):
        await _call(
            "commit_effort",
            {"actor": "Kael", "source": "channeler", "points": 1, "duration": "scene", "label": ""},
            ctx,
        )


async def test_commit_effort_unknown_actor_returns_not_found() -> None:
    snap = _build_snapshot(characters=[])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "commit_effort",
        {"actor": "Ghost", "source": "channeler", "points": 1, "duration": "scene", "label": ""},
        ctx,
    )
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Ghost" in r.message


async def test_commit_effort_unknown_source_raises() -> None:
    """Missing Effort pool → ValueError (fail loud per module contract)."""
    char = _wwn_character("Kael", effort_source="channeler")
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="vowed"):
        await _call(
            "commit_effort",
            {"actor": "Kael", "source": "vowed", "points": 1, "duration": "scene", "label": ""},
            ctx,
        )


async def test_commit_effort_no_active_session_returns_fatal() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    pack = _FakePack()
    store = pg_empty_store(slug="ce-empty")
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "commit_effort",
        {"actor": "Kael", "source": "channeler", "points": 1, "duration": "scene", "label": ""},
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert "no active session" in (r.message or "")


# ===========================================================================
# veterans_luck TESTS
# ===========================================================================


async def test_veterans_luck_first_call_this_scene_applies() -> None:
    char = _wwn_character("Ruk", effort_source=None)
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "veterans_luck",
        {"actor": "Ruk", "mode": "force_hit"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["applied"] is True
    assert p["mode"] == "force_hit"


async def test_veterans_luck_second_call_same_scene_is_refused() -> None:
    from sidequest.game.ruleset.wwn import VETERANS_LUCK_USED_MARKER
    from sidequest.game.status import Status, StatusSeverity

    char = _wwn_character("Ruk", effort_source=None)
    # Pre-mark as used (simulates it already having fired this scene)
    char.core.statuses.append(
        Status(text=VETERANS_LUCK_USED_MARKER, severity=StatusSeverity.Scratch)
    )
    snap = _build_snapshot(characters=[char])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "veterans_luck",
        {"actor": "Ruk", "mode": "force_miss"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["applied"] is False
    assert "scene" in p["reason"].lower() or "luck" in p["reason"].lower()


async def test_veterans_luck_non_wwn_pack_raises() -> None:
    char = _wwn_character("Ruk", effort_source=None)
    snap = _build_snapshot(characters=[char])
    pack = _NonWwnPack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="wwn"):
        await _call(
            "veterans_luck",
            {"actor": "Ruk", "mode": "force_hit"},
            ctx,
        )


async def test_veterans_luck_unknown_actor_returns_not_found() -> None:
    snap = _build_snapshot(characters=[])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "veterans_luck",
        {"actor": "Nobody", "mode": "force_hit"},
        ctx,
    )
    assert r.status is ToolResultStatus.NOT_FOUND


async def test_veterans_luck_no_active_session_returns_fatal() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    pack = _FakePack()
    store = pg_empty_store(slug="vl-empty")
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "veterans_luck",
        {"actor": "Ruk", "mode": "force_hit"},
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_FATAL


# ===========================================================================
# long_rest TESTS
# ===========================================================================


def _wwn_caster(
    name: str,
    *,
    effort_source: str = "channeler",
    effort_max: int = 3,
    day_committed: int = 0,
    scene_committed: int = 0,
    casts_remaining: int = 1,
    casts_per_day: int = 4,
    prepared: list[str] | None = None,
) -> Character:
    """Build a WWN caster with committed Effort and spellcasting state."""
    from sidequest.game.wwn_magic import EffortCommitment

    pool = EffortPool(source=effort_source, max=effort_max)
    if day_committed > 0:
        pool.commitments.append(EffortCommitment(points=day_committed, duration="day", label=""))
    if scene_committed > 0:
        pool.commitments.append(
            EffortCommitment(points=scene_committed, duration="scene", label="")
        )

    sc = SpellcastingState(
        prepared=prepared or ["fireball"],
        casts_remaining=casts_remaining,
        casts_per_day=casts_per_day,
        max_spell_level=3,
    )
    core = CreatureCore(
        name=name,
        description="Elemental wielder.",
        personality="focused",
        inventory=Inventory(),
        hp=HpPool(current=6, max=6, base_max=6),
        system_strain=SystemStrainPool(current=0, max=12, permanent=0),
        effort={effort_source: pool},
        spellcasting=sc,
    )
    return Character(
        core=core,
        backstory="Studies the old ways.",
        char_class="Channeler",
        race="Elf",
    )


async def test_long_rest_comfortable_reclaims_day_and_refreshes_casts() -> None:
    caster = _wwn_caster(
        "Lyra",
        day_committed=1,
        scene_committed=1,
        casts_remaining=0,
        casts_per_day=4,
    )
    snap = _build_snapshot(characters=[caster])
    pack = _FakePack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call("long_rest", {"comfortable": True}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["rested"] is True

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Lyra")
    assert core is not None
    pool = core.effort["channeler"]
    # Both scene and day commitments dropped on comfortable rest
    assert pool.available == pool.max
    # Casts refreshed
    assert core.spellcasting is not None
    assert core.spellcasting.casts_remaining == 4


async def test_long_rest_uncomfortable_leaves_day_committed_when_required() -> None:
    """comfortable=False + day_reclaim_requires_comfort=True → day Effort stays."""
    caster = _wwn_caster(
        "Lyra",
        day_committed=2,
        scene_committed=1,
        casts_remaining=0,
        casts_per_day=4,
    )
    snap = _build_snapshot(characters=[caster])
    pack = _FakePack()  # WwnConfig default: day_reclaim_requires_comfort=True
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call("long_rest", {"comfortable": False}, ctx)
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Lyra")
    assert core is not None
    pool = core.effort["channeler"]
    # Scene dropped (1), day NOT dropped (2 still committed)
    assert pool.available == pool.max - 2
    # Casts still refreshed
    assert core.spellcasting is not None
    assert core.spellcasting.casts_remaining == 4


async def test_long_rest_non_wwn_pack_raises() -> None:
    caster = _wwn_caster("Lyra")
    snap = _build_snapshot(characters=[caster])
    pack = _NonWwnPack()
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="wwn"):
        await _call("long_rest", {"comfortable": True}, ctx)


async def test_long_rest_no_active_session_returns_fatal() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    pack = _FakePack()
    store = pg_empty_store(slug="lr-empty")
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call("long_rest", {"comfortable": True}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL


async def test_long_rest_reprepare_updates_prepared_list() -> None:
    """reprepare dict sets new prepared spells for named casters."""
    catalog = WwnSpellCatalog(
        spells=[
            WwnSpell(
                id="wind_blast",
                name="Wind Blast",
                level=1,
                genre_description="A burst of cutting wind.",
                mechanical_effect="1d6 damage",
            ),
            WwnSpell(
                id="stone_shield",
                name="Stone Shield",
                level=2,
                genre_description="Rock surrounds you.",
                mechanical_effect="AC +2 for scene",
            ),
        ]
    )
    pack = _FakePack(wwn_spell_catalog=catalog)

    caster = _wwn_caster("Lyra", prepared=["wind_blast"], casts_remaining=0, casts_per_day=3)
    snap = _build_snapshot(characters=[caster])
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    r = await _call(
        "long_rest",
        {"comfortable": True, "reprepare": {"Lyra": ["stone_shield"]}},
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core("Lyra")
    assert core is not None
    assert core.spellcasting is not None
    assert core.spellcasting.prepared == ["stone_shield"]


async def test_long_rest_reprepare_unknown_spell_id_raises() -> None:
    """No silent fallback: unknown spell id in reprepare → ValueError."""
    catalog = WwnSpellCatalog(
        spells=[
            WwnSpell(
                id="wind_blast",
                name="Wind Blast",
                level=1,
                genre_description="A burst of cutting wind.",
                mechanical_effect="1d6 damage",
            ),
        ]
    )
    pack = _FakePack(wwn_spell_catalog=catalog)

    caster = _wwn_caster("Lyra", prepared=["wind_blast"])
    snap = _build_snapshot(characters=[caster])
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="phantom_fire"):
        await _call(
            "long_rest",
            {"comfortable": True, "reprepare": {"Lyra": ["phantom_fire"]}},
            ctx,
        )


async def test_long_rest_reprepare_requires_catalog_when_reprepare_given() -> None:
    """reprepare requested but no wwn_spell_catalog on pack → ValueError."""
    pack = _FakePack(wwn_spell_catalog=None)
    caster = _wwn_caster("Lyra", prepared=["wind_blast"])
    snap = _build_snapshot(characters=[caster])
    store = _store_with(snap)
    ctx = _make_ctx(store, genre_pack=pack)

    with pytest.raises(ValueError, match="catalog"):
        await _call(
            "long_rest",
            {"comfortable": True, "reprepare": {"Lyra": ["wind_blast"]}},
            ctx,
        )
