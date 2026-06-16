"""Unit tests for ``_migrate_s2_npc_registry_split`` (Wave 2A — story 45-47).

S2 splits the legacy ``npc_registry`` (which fused identity + last-seen +
hp/max_hp) into:
- ``npc_pool`` — identity-only ``NpcPoolMember`` entries
- ``Npc.last_seen_*`` — last-seen tracking moved onto encountered NPCs

For each legacy registry entry the migration:
1. If a matching ``Npc`` exists in ``npcs`` (by case-folded name): merge
   ``last_seen_*`` onto the ``Npc`` and drop the registry entry. Legacy
   ``hp/max_hp`` are NOT migrated to ``Npc.core.edge`` (canonical edge is
   already authoritative; legacy hp is redundant when the Npc exists).
2. Otherwise, if ``hp`` or ``max_hp`` is set: orphan stat block — drop
   with span attribute ``s2_orphans_dropped`` (legacy bug state, not
   canonicalized).
3. Otherwise: emit ``NpcPoolMember`` with ``drawn_from="legacy_registry"``.

Drops the ``npc_registry`` field from output. Returns OTEL attributes when
any rewrite happens.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from sidequest.game.migrations import migrate_legacy_snapshot

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "legacy_snapshots"


def _minimal_npc_dict(name: str, location: str = "Inn") -> dict[str, Any]:
    """Construct a minimal valid Npc-shaped dict for migration fixtures."""
    return {
        "core": {
            "name": name,
            "description": "A weathered figure.",
            "personality": "Stoic.",
            "level": 1,
            "xp": 0,
            "inventory": {"items": [], "max_slots": 10},
            "statuses": [],
            "edge": {
                "current": 10,
                "max": 10,
                "base_max": 10,
                "recovery_triggers": [{"kind": "OnResolution"}],
                "thresholds": [],
            },
            "acquired_advancements": [],
        },
        "voice_id": None,
        "disposition": 0,
        "location": location,
        "current_room": None,
        "pronouns": "they/them",
        "appearance": "weathered hands",
        "age": "40s",
        "build": "stocky",
        "height": "tall",
        "distinguishing_features": [],
        "ocean": None,
        "belief_state": {},
        "resolution_tier": "spawn",
        "non_transactional_interactions": 0,
        "jungian_id": None,
        "rpg_role_id": None,
        "npc_role_id": None,
        "resolved_archetype": None,
    }


def _registry_entry(
    name: str,
    *,
    role: str | None = None,
    pronouns: str | None = None,
    appearance: str | None = None,
    last_seen_location: str | None = None,
    last_seen_turn: int = 0,
    hp: int | None = None,
    max_hp: int | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "pronouns": pronouns,
        "appearance": appearance,
        "last_seen_location": last_seen_location,
        "last_seen_turn": last_seen_turn,
        "hp": hp,
        "max_hp": max_hp,
    }


# ---------------------------------------------------------------------------
# Branch 1: name-only registry entry → NpcPoolMember
# ---------------------------------------------------------------------------


def test_name_only_registry_entry_becomes_pool_member() -> None:
    legacy = {
        "npcs": [],
        "npc_registry": [
            _registry_entry(
                "Marya",
                role="barkeep",
                pronouns="she/her",
                appearance="weathered hands",
            )
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    assert "npc_registry" not in out
    pool = out["npc_pool"]
    assert len(pool) == 1
    member = pool[0]
    assert member["name"] == "Marya"
    assert member["role"] == "barkeep"
    assert member["pronouns"] == "she/her"
    assert member["appearance"] == "weathered hands"
    assert member["drawn_from"] == "legacy_registry"
    assert member["archetype_id"] is None


def test_multiple_name_only_entries_all_become_pool_members() -> None:
    legacy = {
        "npcs": [],
        "npc_registry": [
            _registry_entry("A"),
            _registry_entry("B", role="merchant"),
            _registry_entry("C", pronouns="he/him"),
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    assert len(out["npc_pool"]) == 3
    names = [m["name"] for m in out["npc_pool"]]
    assert names == ["A", "B", "C"]
    assert all(m["drawn_from"] == "legacy_registry" for m in out["npc_pool"])


# ---------------------------------------------------------------------------
# Branch 2: stats-published with matching Npc → merge last_seen onto Npc
# ---------------------------------------------------------------------------


def test_stats_published_with_matching_npc_merges_last_seen() -> None:
    legacy = {
        "npcs": [_minimal_npc_dict("Boris")],
        "npc_registry": [
            _registry_entry(
                "Boris",
                last_seen_location="TavernRow",
                last_seen_turn=7,
                hp=12,
                max_hp=12,
            )
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    assert "npc_registry" not in out
    assert out["npc_pool"] == []
    npc = out["npcs"][0]
    assert npc["last_seen_location"] == "TavernRow"
    assert npc["last_seen_turn"] == 7
    # pool_origin is preserved as None — legacy provenance is lost.
    assert npc.get("pool_origin") is None


def test_stats_published_match_is_case_insensitive() -> None:
    legacy = {
        "npcs": [_minimal_npc_dict("Boris")],
        "npc_registry": [
            _registry_entry(
                "BORIS",
                last_seen_location="Bridge",
                last_seen_turn=2,
                hp=5,
                max_hp=10,
            )
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    npc = out["npcs"][0]
    assert npc["last_seen_location"] == "Bridge"
    assert npc["last_seen_turn"] == 2


def test_name_only_entry_with_matching_npc_merges_last_seen_too() -> None:
    """A registry entry with no hp but matching Npc still merges last_seen.
    The migration prefers npc-update over pool-emit when both apply."""
    legacy = {
        "npcs": [_minimal_npc_dict("Wren")],
        "npc_registry": [
            _registry_entry(
                "Wren",
                last_seen_location="Crossroads",
                last_seen_turn=5,
            )
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    assert out["npc_pool"] == []
    npc = out["npcs"][0]
    assert npc["last_seen_location"] == "Crossroads"
    assert npc["last_seen_turn"] == 5


# ---------------------------------------------------------------------------
# Branch 3: stats-published, no matching Npc → orphan dropped
# ---------------------------------------------------------------------------


def test_stats_published_orphan_is_dropped() -> None:
    """Edge case: registry entry has hp set but no matching Npc.
    Legacy bug state — drop the orphan, do NOT synthesize an Npc."""
    legacy = {
        "npcs": [],
        "npc_registry": [
            _registry_entry("GhostStat", hp=5, max_hp=10),
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    assert out["npc_pool"] == []
    assert out["npcs"] == []


# ---------------------------------------------------------------------------
# Mixed branches and edge cases
# ---------------------------------------------------------------------------


def test_mixed_registry_branches_in_one_migration() -> None:
    legacy = {
        "npcs": [_minimal_npc_dict("Boris")],
        "npc_registry": [
            _registry_entry("Marya"),  # branch 1: pool
            _registry_entry(
                "Boris",
                last_seen_location="Inn",
                last_seen_turn=3,
                hp=10,
                max_hp=10,
            ),  # branch 2: merge
            _registry_entry("GhostStat", hp=1, max_hp=10),  # branch 3: orphan
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    assert "npc_registry" not in out
    assert len(out["npc_pool"]) == 1
    assert out["npc_pool"][0]["name"] == "Marya"
    assert len(out["npcs"]) == 1
    assert out["npcs"][0]["last_seen_location"] == "Inn"
    assert out["npcs"][0]["last_seen_turn"] == 3


def test_empty_npc_registry_is_no_op_drop() -> None:
    legacy = {"npcs": [], "npc_registry": []}
    out = migrate_legacy_snapshot(legacy)
    assert "npc_registry" not in out
    assert out["npc_pool"] == []


def test_missing_npc_registry_does_not_create_one() -> None:
    """If the snapshot never had npc_registry, migration leaves it absent.
    No empty-key pollution."""
    canonical = {"npcs": [], "npc_pool": []}
    out = migrate_legacy_snapshot(copy.deepcopy(canonical))
    assert "npc_registry" not in out
    assert out["npc_pool"] == []


def test_canonical_already_migrated_unchanged() -> None:
    """If the snapshot already has npc_pool (canonical) and no npc_registry,
    no rewrite happens."""
    canonical = {
        "npcs": [],
        "npc_pool": [
            {
                "name": "X",
                "drawn_from": "world_authored",
                "role": None,
                "pronouns": None,
                "appearance": None,
                "archetype_id": None,
            }
        ],
    }
    before = copy.deepcopy(canonical)
    out = migrate_legacy_snapshot(canonical)
    # Canonical input — no field changes.
    assert out["npc_pool"] == before["npc_pool"]
    assert "npc_registry" not in out


def test_input_dict_is_not_mutated() -> None:
    legacy = {
        "npcs": [],
        "npc_registry": [_registry_entry("Marya")],
    }
    snapshot = copy.deepcopy(legacy)
    migrate_legacy_snapshot(legacy)
    # Migration must not mutate caller's dict.
    assert legacy == snapshot


def test_existing_pool_entries_preserved_when_legacy_registry_also_present() -> None:
    """If a snapshot has BOTH npc_pool (canonical) and npc_registry
    (legacy), migration appends legacy entries to the existing pool
    rather than clobbering it."""
    legacy = {
        "npcs": [],
        "npc_pool": [
            {
                "name": "World-X",
                "drawn_from": "world_authored",
                "role": None,
                "pronouns": None,
                "appearance": None,
                "archetype_id": None,
            }
        ],
        "npc_registry": [_registry_entry("Marya")],
    }
    out = migrate_legacy_snapshot(legacy)
    assert "npc_registry" not in out
    assert len(out["npc_pool"]) == 2
    names = {m["name"] for m in out["npc_pool"]}
    assert names == {"World-X", "Marya"}


# ---------------------------------------------------------------------------
# Coordination with Wave 1 (S1 + S2 in one migration call)
# ---------------------------------------------------------------------------


def test_s1_and_s2_can_run_in_same_migration() -> None:
    """A real legacy save can have BOTH world_confrontations (S1) and
    npc_registry (S2). Both sub-functions must fire; both attribute
    namespaces must surface in the canonicalize span."""
    legacy = {
        "npcs": [],
        "magic_state": {"confrontations": []},
        "world_confrontations": [{"id": "duel-1", "register": "intimate"}],
        "npc_registry": [_registry_entry("Marya")],
    }
    out = migrate_legacy_snapshot(legacy)
    assert "world_confrontations" not in out
    assert "npc_registry" not in out
    assert len(out["magic_state"]["confrontations"]) == 1
    assert len(out["npc_pool"]) == 1


# ---------------------------------------------------------------------------
# Round-trip on captured legacy fixture
# ---------------------------------------------------------------------------


def test_malformed_npc_entries_counted_in_otel(otel_capture) -> None:
    """Story 45-52 silent-failure detector.

    Non-dict entries in legacy ``npc_registry`` are silently skipped by the
    migration. The migration must publish a count so the GM panel can see
    them rather than the migration eating them on the floor.
    """
    legacy = {
        "npcs": [],
        "npc_registry": [
            "not-a-dict",
            42,
            _registry_entry("Real Entry"),
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    # Only the valid entry made it through.
    assert len(out["npc_pool"]) == 1
    assert out["npc_pool"][0]["name"] == "Real Entry"

    canonicalize_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "snapshot.canonicalize"
    ]
    assert len(canonicalize_spans) == 1
    attrs = dict(canonicalize_spans[0].attributes or {})
    assert attrs.get("s2_malformed_npcs_skipped") == 2, (
        "Migration must count non-dict registry entries via "
        f"s2_malformed_npcs_skipped; got {attrs!r}"
    )


def test_nameless_npc_entries_counted_in_otel(otel_capture) -> None:
    """Story 45-52 silent-failure detector.

    Entries with an empty/missing name are silently dropped. The migration
    must publish a count so the GM panel can see them rather than the
    migration eating them on the floor.
    """
    legacy = {
        "npcs": [],
        "npc_registry": [
            _registry_entry(""),  # blank name
            {"role": "merchant"},  # no name key at all
            _registry_entry("Has-A-Name"),
        ],
    }
    out = migrate_legacy_snapshot(legacy)
    # Only the named entry made it through.
    assert len(out["npc_pool"]) == 1
    assert out["npc_pool"][0]["name"] == "Has-A-Name"

    canonicalize_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "snapshot.canonicalize"
    ]
    assert len(canonicalize_spans) == 1
    attrs = dict(canonicalize_spans[0].attributes or {})
    assert attrs.get("s2_nameless_entries_dropped") == 2, (
        "Migration must count nameless registry entries via "
        f"s2_nameless_entries_dropped; got {attrs!r}"
    )


@pytest.fixture
def otel_capture():
    """Attach an in-memory exporter to the running TracerProvider."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def test_legacy_fixture_with_npc_registry_round_trips() -> None:
    """Real save (2026-05-03 coyote_star MP) round-trips through the
    migration. All registry entries are name-only in the captured fixture
    so they all become pool members."""
    fixture_path = _FIXTURE_DIR / "with_npc_registry.json"
    if not fixture_path.exists():
        pytest.skip("fixture not captured")

    legacy = json.loads(fixture_path.read_text())
    legacy_registry_count = len(legacy.get("npc_registry", []))

    out = migrate_legacy_snapshot(legacy)

    assert "npc_registry" not in out
    pool = out.get("npc_pool", [])
    # Every entry in the fixture had hp=None — all become pool members.
    assert len(pool) == legacy_registry_count
    for member in pool:
        assert member["drawn_from"] == "legacy_registry"
        assert "name" in member
        assert member.get("archetype_id") is None
