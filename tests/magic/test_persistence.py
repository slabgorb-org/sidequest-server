"""SQLite save/load roundtrip for MagicState.

Task 2.5 — Phase 2 cut-point verification.

The persistence layer serializes GameSnapshot via ``model_dump_json()`` and
deserializes via ``model_validate_json()``, so ``magic_state`` roundtrips for
free as a declared Pydantic field.  These tests prove that claim end-to-end
against a real in-memory SQLite store (same code path as production).
"""

from __future__ import annotations

import pytest

from sidequest.game.delta import compute_delta, snapshot
from sidequest.game.persistence import SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.magic.models import (
    HardLimit,
    LedgerBarSpec,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.state import BarKey, MagicState

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def world_config() -> WorldMagicConfig:
    """Minimal Coyote Star config — same bars used across Tasks 2.x."""
    return WorldMagicConfig(
        world_slug="coyote_star",
        genre_slug="space_opera",
        allowed_sources=["innate"],
        active_plugins=["innate_v1"],
        intensity=0.25,
        world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
        visibility={"primary": "feared"},
        hard_limits=[HardLimit(id="psionics_never_decisive", description="x")],
        cost_types=["sanity"],
        ledger_bars=[
            LedgerBarSpec(
                id="sanity",
                scope="character",
                direction="down",
                range=(0.0, 1.0),
                threshold_low=0.40,
                consequence_on_low_cross="auto-fire The Bleeding-Through",
                starts_at_chargen=1.0,
            ),
        ],
        narrator_register="x",
    )


@pytest.fixture()
def store() -> SqliteStore:
    """Fresh in-memory SQLite store per test."""
    s = SqliteStore.open_in_memory()
    s.init_session("space_opera", "coyote_star")
    return s


# ---------------------------------------------------------------------------
# Test 1: ledger bar value survives save/load
# ---------------------------------------------------------------------------


def test_persist_roundtrip_preserves_ledger(world_config, store) -> None:
    """Save a snapshot carrying MagicState; load it; assert sanity bar value preserved."""
    magic = MagicState.from_config(world_config)
    magic.add_character("sira_mendes")

    sanity_key = BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity")
    magic.set_bar_value(sanity_key, 0.72)

    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        magic_state=magic,
    )
    store.save(snap)

    saved = store.load()
    assert saved is not None, "load() must return a SavedSession after save()"
    assert saved.snapshot.magic_state is not None, (
        "magic_state must survive SQLite roundtrip — "
        "check that GameSnapshot.magic_state is a declared Pydantic field "
        "so model_dump_json includes it"
    )
    restored_bar = saved.snapshot.magic_state.get_bar(sanity_key)
    assert restored_bar.value == pytest.approx(0.72), (
        f"Expected sanity=0.72 after roundtrip; got {restored_bar.value}"
    )


# ---------------------------------------------------------------------------
# Test 2: compute_delta after load detects no change
# ---------------------------------------------------------------------------


def test_compute_delta_after_load_detects_no_change(world_config, store) -> None:
    """Round-trip a snapshot, then compute_delta(before, after) must be is_empty()."""
    magic = MagicState.from_config(world_config)
    magic.add_character("sira_mendes")
    magic.set_bar_value(BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity"), 0.55)

    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        magic_state=magic,
    )

    before = snapshot(snap)
    store.save(snap)

    saved = store.load()
    assert saved is not None

    after = snapshot(saved.snapshot)
    delta = compute_delta(before, after)

    assert delta.is_empty(), (
        f"compute_delta must report no change after a pure save/load cycle; "
        f"flags: magic={delta.magic}, characters={delta.characters}, "
        f"location={delta.location}"
    )
    # Narrow assertion: magic flag must be False specifically.
    assert delta.magic is False, (
        "StateDelta.magic must be False — MagicState serialization changed across "
        "the roundtrip, indicating non-deterministic dump or field exclusion"
    )


# ---------------------------------------------------------------------------
# Test 3: legacy save (no magic_state) loads with magic_state=None
# ---------------------------------------------------------------------------


def test_legacy_save_loads_with_none_magic_state(store) -> None:
    """A snapshot saved without magic_state loads successfully with magic_state=None.

    Writes a raw JSON blob that omits the magic_state key entirely, then
    verifies that model_validate_json treats the missing field as None
    (the declared default) rather than raising a ValidationError.
    """
    import json

    # Build a minimal snapshot dict, deliberately omitting magic_state.
    snap = GameSnapshot(genre_slug="space_opera", world_slug="coyote_star")
    raw = json.loads(snap.model_dump_json())
    raw.pop("magic_state", None)  # simulate a pre-magic save file

    raw_json = json.dumps(raw)

    # Write directly into the game_state table to bypass save() which would
    # re-inject magic_state via model_dump_json.
    from datetime import UTC, datetime

    store._conn.execute(
        "INSERT OR REPLACE INTO game_state (id, snapshot_json, saved_at) VALUES (1, ?, ?)",
        (raw_json, datetime.now(tz=UTC).isoformat()),
    )
    store._conn.commit()

    saved = store.load()
    assert saved is not None
    assert saved.snapshot.magic_state is None, (
        "legacy save missing magic_state key must deserialize to magic_state=None"
    )
