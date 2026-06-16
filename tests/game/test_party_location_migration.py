"""Failing tests for ``_migrate_s3_party_location`` (story 45-48 / Wave 2B).

S3 retires the party-level ``snapshot.location`` field. Migration on load
must promote any legacy ``location`` value into ``character_locations`` for
every seated character before pydantic drops the unknown field (the model
config is ``extra: ignore``, so without this migration legacy saves silently
lose location data).

Spec source: docs/superpowers/specs/2026-05-04-snapshot-split-brain-cleanup-design.md
§"Migration on load" (lines 226-228):

    if character_locations is empty and snapshot.location is non-empty,
    populate character_locations[name] = snapshot.location for every
    seated character. Then drop location.

Pattern: matches Wave 2A's ``_migrate_s2_npc_registry_split`` — see
``tests/game/test_npc_pool_migration.py``.
"""

from __future__ import annotations

import copy
from typing import Any

from sidequest.game.migrations import migrate_legacy_snapshot

# ---------------------------------------------------------------------------
# Migration is wired into the orchestrator
# ---------------------------------------------------------------------------


def test_s3_sub_function_is_registered_in_orchestrator() -> None:
    """Wire test: ``migrate_legacy_snapshot`` must call the S3 sub-function.
    Otherwise the migration is dead code and legacy saves drop ``location``
    silently when pydantic ignores the unknown field."""
    from sidequest.game import migrations

    assert hasattr(migrations, "_migrate_s3_party_location"), (
        "S3 migration sub-function missing — Wave 2B AC5 not wired"
    )


# ---------------------------------------------------------------------------
# Happy path: legacy location backfilled into character_locations
# ---------------------------------------------------------------------------


def _seated_legacy(location: str = "Galley", **extras: Any) -> dict[str, Any]:
    """Build a legacy-shape snapshot dict with one seated PC."""
    base: dict[str, Any] = {
        "genre_slug": "g",
        "world_slug": "w",
        "location": location,
        "player_seats": {"p:1": "Shirley"},
        "characters": [],
        "npcs": [],
    }
    base.update(extras)
    return base


def test_legacy_location_backfills_character_locations_for_seated_pc() -> None:
    legacy = _seated_legacy(location="Galley")
    out = migrate_legacy_snapshot(legacy)

    assert out["character_locations"] == {"Shirley": "Galley"}
    # Legacy field is dropped post-migration.
    assert "location" not in out


def test_legacy_location_backfills_for_every_seated_pc() -> None:
    legacy = _seated_legacy(
        location="Bridge",
        player_seats={"p:1": "Shirley", "p:2": "Laverne", "p:3": "Squiggy"},
    )
    out = migrate_legacy_snapshot(legacy)

    assert out["character_locations"] == {
        "Shirley": "Bridge",
        "Laverne": "Bridge",
        "Squiggy": "Bridge",
    }
    assert "location" not in out


def test_legacy_location_preserves_existing_character_location_entries() -> None:
    """When ``character_locations`` already has entries, the migration must
    NOT overwrite them — those entries are newer truth than the legacy
    party-level field. Spec wording: 'if character_locations is empty…'."""
    legacy = _seated_legacy(
        location="Bridge",
        player_seats={"p:1": "Shirley", "p:2": "Laverne"},
        character_locations={"Shirley": "Cockpit"},  # existing, newer
    )
    out = migrate_legacy_snapshot(legacy)

    # Existing entry kept; only the absent peer gets seeded from legacy.
    assert out["character_locations"]["Shirley"] == "Cockpit"
    assert out["character_locations"]["Laverne"] == "Bridge"
    assert "location" not in out


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


def test_canonical_snapshot_without_legacy_location_unchanged() -> None:
    """Already-canonical snapshot (no ``location`` key) is left alone."""
    canonical = {
        "genre_slug": "g",
        "world_slug": "w",
        "player_seats": {"p:1": "Shirley"},
        "character_locations": {"Shirley": "Cockpit"},
        "characters": [],
        "npcs": [],
    }
    before = copy.deepcopy(canonical)
    out = migrate_legacy_snapshot(canonical)

    assert out["character_locations"] == before["character_locations"]
    assert "location" not in out


def test_empty_legacy_location_does_not_seed_empty_strings() -> None:
    """A legacy save with ``location=''`` (fresh / pre-narration) must NOT
    seed empty strings into ``character_locations`` — that would poison
    every per-character resolver and the field is dropped anyway. No silent
    fallback (CLAUDE.md)."""
    legacy = _seated_legacy(location="")
    out = migrate_legacy_snapshot(legacy)

    assert out.get("character_locations", {}) == {}
    assert "location" not in out


def test_legacy_location_with_no_seated_pcs_just_drops_field() -> None:
    """Pre-chargen legacy save: no seats yet, but location may be set
    (fixture). Drop the field; do not invent character entries from nothing."""
    legacy = {
        "genre_slug": "g",
        "world_slug": "w",
        "location": "Lobby",
        "player_seats": {},
        "characters": [],
        "npcs": [],
    }
    out = migrate_legacy_snapshot(legacy)

    assert out.get("character_locations", {}) == {}
    assert "location" not in out


def test_input_dict_is_not_mutated_by_s3_migration() -> None:
    legacy = _seated_legacy(location="Galley")
    snapshot = copy.deepcopy(legacy)
    migrate_legacy_snapshot(legacy)
    assert legacy == snapshot


# ---------------------------------------------------------------------------
# OTEL — snapshot.canonicalize gains S3 attributes
# ---------------------------------------------------------------------------


def test_s3_attributes_routed_through_canonicalize_extractor() -> None:
    """The ``snapshot.canonicalize`` payload routed to the GM panel must
    include the S3 per-field counters when the migration ran. The honesty
    rule from the existing extractor (test_canonicalize_extract_only_…)
    requires the new keys to be added to the extractor's allow-list."""
    from types import SimpleNamespace

    from sidequest.telemetry.spans._core import SPAN_ROUTES

    route = SPAN_ROUTES["snapshot.canonicalize"]
    span = SimpleNamespace(
        name="snapshot.canonicalize",
        attributes={"s3_party_location_seeded": 2},
    )
    payload = route.extract(span)

    # New S3 key must surface in the payload.
    assert payload.get("s3_party_location_seeded") == 2


def test_canonicalize_span_fires_when_s3_migration_runs() -> None:
    """Wire test: a legacy save with ``location`` must trigger the
    ``snapshot.canonicalize`` span with the S3 attribute set. Capture via
    the in-memory OTEL exporter (same pattern as
    tests/integration/test_orbital_e2e.py)."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        legacy = _seated_legacy(location="Galley")
        migrate_legacy_snapshot(legacy)

        canonicalize = [
            s for s in exporter.get_finished_spans() if s.name == "snapshot.canonicalize"
        ]
        assert canonicalize, "canonicalize span did not fire for S3 migration"
        attrs = dict(canonicalize[-1].attributes or {})
        assert attrs.get("s3_party_location_seeded", 0) >= 1
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Coordination with prior S1 + S2 (sub-function ordering)
# ---------------------------------------------------------------------------


def test_s1_s2_s3_can_run_in_same_migration_call() -> None:
    """A real legacy save can carry world_confrontations (S1), npc_registry
    (S2), AND a party-level location (S3). All three must co-migrate; no
    later sub-function clobbers the earlier ones' work."""
    legacy = {
        "genre_slug": "g",
        "world_slug": "w",
        "magic_state": {"confrontations": []},
        "world_confrontations": [{"id": "duel-1", "register": "intimate"}],
        "npc_registry": [
            {"name": "Marya", "role": "barkeep"},
        ],
        "location": "Tavern",
        "player_seats": {"p:1": "Shirley"},
        "characters": [],
        "npcs": [],
    }
    out = migrate_legacy_snapshot(legacy)

    # S1
    assert "world_confrontations" not in out
    assert len(out["magic_state"]["confrontations"]) == 1
    # S2
    assert "npc_registry" not in out
    assert len(out["npc_pool"]) == 1
    # S3
    assert "location" not in out
    assert out["character_locations"]["Shirley"] == "Tavern"


# ---------------------------------------------------------------------------
# AC8: round-trip via SqliteStore.load
# ---------------------------------------------------------------------------


def test_legacy_save_round_trips_through_sqlite_store(tmp_path) -> None:
    """End-to-end: write a legacy snapshot dict to SQLite, load it via
    ``SqliteStore.load``, and verify ``character_locations`` is populated
    from the legacy ``location`` field. This is the wire-first integration
    test the epic requires (Lane B integration check)."""
    import json

    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot

    store = SqliteStore(tmp_path / "save.db")
    store.init_session(genre_slug="g", world_slug="w")

    # Save a canonical snapshot first to materialize the schema/row.
    canonical = GameSnapshot(genre_slug="g", world_slug="w")
    store.save(canonical)

    # Now overwrite the row's snapshot blob with a legacy-shape JSON that
    # uses ``location`` and seats one PC. The migration must promote the
    # value on load.
    legacy_blob = json.dumps(
        {
            "genre_slug": "g",
            "world_slug": "w",
            "location": "Galley",
            "player_seats": {"p:1": "Shirley"},
        }
    )
    with store._conn:  # type: ignore[attr-defined]
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE game_state SET snapshot_json = ? WHERE id = 1",
            (legacy_blob,),
        )

    loaded = store.load()
    assert loaded is not None
    snap = loaded.snapshot
    assert snap.character_locations == {"Shirley": "Galley"}
    # Field is gone from the validated model.
    assert not hasattr(snap, "location")
