"""Story 162-5 — V4 gate (GATED): does flickering_reach's runtime resolve
creatures.yaml stats?

Verification item V4 (spec §7): "Whether flickering_reach's runtime resolves
creatures.yaml stats anywhere (native-path encountergen
``_collect_creatures_from_yaml``, ``encountergen.py:231``) — if yes, its
divergent stat block is live ammunition, not just dead content."

Answer, from the code: **YES.** ``pregen._generate_encounter`` invokes
``encountergen.main`` in-process with ``--world`` (pregen.py:214), and
``main`` samples creatures.yaml and RETURNS an EnemyBlock roster built via
``creature_to_enemy_block`` *before* it ever reaches the ``effective_bestiary``
path (encountergen.py:788-805) whenever a non-empty creatures.yaml exists —
which flickering_reach has. So the divergent creatures.yaml stat block is a
live runtime source for this world.

Two tests encode that finding:
  * a GREEN characterization pin — the native path reads creatures.yaml (records
    V4=YES so it cannot silently regress);
  * a RED behavioral tie — a creature spawned from creatures.yaml carries stats
    that DIVERGE from the bestiary (ADR-155 single-source-of-truth violation).
    This is the test that forces AC2's fix to RECONCILE the numbers (stripping
    the stats would leave the spawned husk at hp-default, still != bestiary).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from sidequest.cli.encountergen.encountergen import (
    _collect_creatures_from_yaml,
    creature_to_enemy_block,
)
from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

PACK = "mutant_wasteland"
WORLD = "flickering_reach"


def _load_pack(slug: str) -> Any:
    try:
        return load_genre_pack(find_pack_path(slug))
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))


def _creatures_path() -> Path:
    return find_pack_path(PACK) / "worlds" / WORLD / "creatures.yaml"


def test_v4_native_path_reads_creatures_yaml() -> None:
    """V4 characterization pin (GREEN): the native encountergen loader returns a
    non-empty roster from flickering_reach/creatures.yaml, and that roster shares
    ids with the bestiary. This is the runtime read that makes the divergent
    stat block live ammunition — pinned so a future refactor that stops reading
    it (the correct end-state) trips this test loudly instead of silently."""
    creatures = _collect_creatures_from_yaml(_creatures_path())
    assert creatures, (
        "V4 premise broken: native encountergen path reads no creatures from "
        f"{PACK}/{WORLD}/creatures.yaml — re-verify the gate if this changed"
    )
    creature_ids = {c.get("id") for c in creatures if isinstance(c.get("id"), str)}

    pack = _load_pack(PACK)
    bestiary, _source = pack.effective_bestiary(WORLD)
    assert bestiary is not None
    bestiary_ids = {e.id for e in bestiary.entries}
    shared = creature_ids & bestiary_ids
    assert shared, (
        "creatures.yaml shares no ids with the bestiary — expected the parallel "
        "stat block the spec flagged (the source of the divergence)"
    )


def test_v4_spawned_creature_stats_match_bestiary() -> None:
    """Behavioral tie (RED): a creature SPAWNED from creatures.yaml via the native
    path must carry the bestiary's combat stats (ADR-155: bestiary is the single
    source of truth). RED today — silo_eye spawns at hp30 vs bestiary hp36,
    glass_touched_mount at hp14 vs hp18. GREEN once creatures.yaml is reconciled."""
    creatures = {
        c["id"]: c
        for c in _collect_creatures_from_yaml(_creatures_path())
        if isinstance(c.get("id"), str)
    }
    pack = _load_pack(PACK)
    bestiary, _source = pack.effective_bestiary(WORLD)
    assert bestiary is not None
    by_id = {e.id: e for e in bestiary.entries}

    shared = sorted(set(creatures) & set(by_id))
    assert shared, "no shared ids — behavioral tie would be vacuous"

    rng = random.Random(1620)  # deterministic: creature_to_enemy_block rolls OCEAN
    mismatches: list[str] = []
    for cid in shared:
        enemy = creature_to_enemy_block(creatures[cid], rng)
        expected_hp = by_id[cid].hp
        if enemy.hp != expected_hp:
            mismatches.append(
                f"{cid}: spawned EnemyBlock hp={enemy.hp} but bestiary hp={expected_hp}"
            )

    assert not mismatches, (
        f"{PACK}/{WORLD} spawns creatures with stats that diverge from the bestiary "
        f"(live ammunition — V4=YES):\n  " + "\n  ".join(mismatches)
    )
