"""Playtest 2026-06-07 (blackthorn_moor) — legacy fallback honours the world tier.

``legacy_axis_fallback`` (the no-constraints path) used to read genre-tier
``pack.archetypes`` raw, while every other archetype consumer resolves via
``pack.effective_archetypes(world)`` (world-over-genre replacement, ADR-121).
A world that declared its own archetype pool was invisible to the fallback.
"""

from __future__ import annotations

import argparse
import random
from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.cli.namegen.namegen import legacy_axis_fallback


def _args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "genre": "tea_and_murder",
        "world": "blackthorn_moor",
        "archetype": None,
        "jungian": None,
        "rpg_role": None,
        "npc_role": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _pack(genre_tier: list[Any], world_tier: list[Any]) -> Any:
    return SimpleNamespace(
        archetypes=genre_tier,
        effective_archetypes=lambda world: (
            (world_tier, "world") if world else (genre_tier, "genre")
        ),
    )


def test_fallback_spawns_from_world_tier_not_genre_tier() -> None:
    """World pool replaces the genre pool — the genre tier must be invisible."""
    genre_tier = [SimpleNamespace(name="Genre Ghost", named_individual=False)]
    world_tier = [SimpleNamespace(name="Constable", named_individual=False)]
    pack = _pack(genre_tier, world_tier)

    _j, _r, _n, name, _src = legacy_axis_fallback(pack, _args(), random.Random(0))
    assert name == "Constable"


def test_fallback_exits_when_world_pool_all_named_individual() -> None:
    genre_tier = [SimpleNamespace(name="Genre Ghost", named_individual=False)]
    world_tier = [SimpleNamespace(name="Lady Blackthorn", named_individual=True)]
    pack = _pack(genre_tier, world_tier)

    with pytest.raises(SystemExit) as exc:
        legacy_axis_fallback(pack, _args(), random.Random(0))
    assert exc.value.code == 1


def test_explicit_archetype_lookup_uses_world_tier() -> None:
    """``--archetype`` may target a named_individual, but on the WORLD tier."""
    genre_tier: list[Any] = []
    world_tier = [SimpleNamespace(name="Lady Blackthorn", named_individual=True)]
    pack = _pack(genre_tier, world_tier)

    _j, _r, _n, name, _src = legacy_axis_fallback(
        pack, _args(archetype="lady blackthorn"), random.Random(0)
    )
    assert name == "Lady Blackthorn"
