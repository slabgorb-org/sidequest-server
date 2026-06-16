"""RED tests for Story 77-2 — widen ``quest_log`` to ``dict[str, QuestEntry]``.

ADR-137 §Decision 2 asks ``record_quest`` to mint a *structured* quest
(id + title + objective + status + optional anchor). Operator ruling
(2026-06-03): store it structured by widening ``GameSnapshot.quest_log``
(and ``WorldStatePatch.quest_log``) from ``dict[str, str]`` to
``dict[str, QuestEntry]``, rather than serializing into the string value.

These tests pin the model contract and the load-time migration that keeps
the widening backward-compatible. Saves written before this story carry
``quest_log: dict[str, str]`` (id -> status-string, e.g. the 77-1 seed's
``{"seed_drive": "Active: ..."}`` and the trope handshake's
``{"trope_x": "Resolved at turn 3"}``); they MUST still load — coerced into
``QuestEntry`` — never fail loud on a legacy save (this is the one place a
silent-load would be wrong: existing playtest saves predate the type).

The blast radius (WorldStatePatch, the legacy ``quest_updates`` apply path,
and the 77-1 ``seed_quest_spine`` writer) is a direct consequence of the
type widening and is exercised here so the widened type stays coherent —
NOT the 77-4 retirement of those lanes, which remains out of scope.
"""

from __future__ import annotations

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot, QuestEntry, TurnManager, WorldStatePatch


def _snapshot(**kw) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        turn_manager=TurnManager(interaction=1),
        **kw,
    )


# --------------------------------------------------------------------------- #
# QuestEntry model contract
# --------------------------------------------------------------------------- #


def test_quest_entry_has_structured_fields() -> None:
    entry = QuestEntry(
        title="Defeat the Witch",
        objective="Reach the Emerald City and confront her",
        status="active",
        anchor_id="emerald_city",
    )
    assert entry.title == "Defeat the Witch"
    assert entry.objective == "Reach the Emerald City and confront her"
    assert entry.status == "active"
    assert entry.anchor_id == "emerald_city"


def test_quest_entry_status_defaults_active_and_anchor_optional() -> None:
    entry = QuestEntry(title="Go home", objective="Find a way back to Kansas")
    assert entry.status == "active"
    assert entry.anchor_id is None


def test_quest_entry_instances_do_not_share_anchor_state() -> None:
    """Guard against a mutable-default-argument bug (lang-review #2): two
    bare QuestEntry instances must be independent, not alias one default."""
    a = QuestEntry(title="A", objective="oa")
    b = QuestEntry(title="B", objective="ob")
    a.anchor_id = "anchor_a"
    assert b.anchor_id is None


# --------------------------------------------------------------------------- #
# GameSnapshot.quest_log is now dict[str, QuestEntry]
# --------------------------------------------------------------------------- #


def test_snapshot_quest_log_accepts_structured_entries_and_round_trips() -> None:
    snap = _snapshot(
        quest_log={"q_witch": QuestEntry(title="Defeat the Witch", objective="reach Oz")}
    )
    entry = snap.quest_log["q_witch"]
    assert isinstance(entry, QuestEntry)
    assert entry.title == "Defeat the Witch"

    # Survives a model_dump / model_validate round-trip (save/load).
    reloaded = GameSnapshot.model_validate(snap.model_dump(mode="python"))
    assert isinstance(reloaded.quest_log["q_witch"], QuestEntry)
    assert reloaded.quest_log["q_witch"].objective == "reach Oz"


def test_legacy_string_quest_log_migrates_on_load() -> None:
    """A pre-77-2 save carries quest_log as dict[str, str] (id -> status).
    Loading it MUST coerce each string into a QuestEntry carrying that
    status — never raise (existing playtest saves predate the widened type).
    """
    snap = GameSnapshot.model_validate(
        {
            "genre_slug": "wry_whimsy",
            "world_slug": "oz",
            "quest_log": {
                "seed_drive": "Active: go home",
                "trope_storm": "Resolved at turn 3",
            },
        }
    )
    seed = snap.quest_log["seed_drive"]
    assert isinstance(seed, QuestEntry)
    # The legacy status string is preserved as the QuestEntry status.
    assert seed.status == "Active: go home"

    storm = snap.quest_log["trope_storm"]
    assert isinstance(storm, QuestEntry)
    assert storm.status == "Resolved at turn 3"


# --------------------------------------------------------------------------- #
# WorldStatePatch + legacy apply path stay coherent under the widened type
# --------------------------------------------------------------------------- #


def test_world_state_patch_quest_log_is_structured() -> None:
    patch = WorldStatePatch(
        quest_log={"q1": QuestEntry(title="t", objective="o", status="active")}
    )
    assert patch.quest_log is not None
    assert isinstance(patch.quest_log["q1"], QuestEntry)


# Story 77-4 (ADR-137 AC-3): the legacy ``quest_updates`` apply lane was retired
# (the field is gone from WorldStatePatch). The status-only type-coherence it
# guarded — a bare status string lands as a ``QuestEntry``, never a raw str —
# now lives on ``upsert_quest_status`` and is asserted by
# tests/game/test_quest_updates_retirement.py::test_upsert_quest_status_status_only_path_intact.
# The old ``test_legacy_quest_updates_apply_coerces_to_quest_entry`` was removed
# here as it exercised the now-deleted field.


# --------------------------------------------------------------------------- #
# 77-1 seed writer stays coherent (it must write a QuestEntry now)
# --------------------------------------------------------------------------- #


def _character_with_drive(drive: str) -> Character:
    core = CreatureCore(
        name="Dorothy",
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(core=core, backstory="b", char_class="Traveler", race="Human", drive=drive)


def test_seed_quest_spine_writes_a_quest_entry() -> None:
    """Story 77-1's seed-at-creation writer must produce a QuestEntry under
    the widened type (it previously wrote ``f"Active: {source}"`` — a str)."""
    from sidequest.game.quest_seed import seed_quest_spine

    snap = _snapshot()
    seed_quest_spine(snap, _character_with_drive("recover the stolen ruby"))

    assert snap.quest_log, "seed should have written a quest with a drive present"
    entry = next(iter(snap.quest_log.values()))
    assert isinstance(entry, QuestEntry)
    assert "ruby" in (entry.objective + entry.title + entry.status).lower()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
