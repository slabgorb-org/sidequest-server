"""ADR-153 §3 state graph — loader resolution of per-state tables (158-40, AC-2).

The dogfight def declares ``interaction_tables:`` as a LIST of ``_from:``
pointers; the loader resolves each side-file and keys the registry by each
table's ``starting_state``. Fail-loud contract (CLAUDE.md No Silent
Fallbacks): duplicate or missing ``starting_state`` and path traversal are
load errors, never silent skips.

RED: the loader does not resolve ``interaction_tables`` and the pydantic
models reject the key (``extra="forbid"``), so every load below fails with
the extra-key rejection — the failure message itself is the signpost to
Task 1/2 of the plan.

All packs here are tmp_path copies of ``swn_test_pack`` (96-1 doctrine:
tests test fixtures; the shared fixture stays untouched).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sidequest.genre.loader import GenreLoadError, load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef, InteractionTable
from tests._helpers.fixture_packs import SWN_TEST_PACK, fixture_pack_path
from tests._helpers.state_graph_fixture import (
    _REGISTRY_BLOCK,
    _SINGLE_TABLE_BLOCK,
    make_state_graph_pack,
)


def _dogfight(pack: GenrePack) -> ConfrontationDef:
    assert pack.rules is not None
    return next(c for c in pack.rules.confrontations if c.confrontation_type == "dogfight")


def _copy_pack_with_registry_block(tmp_path: Path, registry_block: str) -> Path:
    """Copy swn_test_pack and swap the dogfight's single-table pointer for an
    arbitrary ``interaction_tables:`` block (for the fail-loud shapes the
    graph builder itself refuses to produce)."""
    pack_dir = tmp_path / "swn_registry_pack"
    shutil.copytree(fixture_pack_path(SWN_TEST_PACK), pack_dir)
    rules_path = pack_dir / "rules.yaml"
    text = rules_path.read_text(encoding="utf-8")
    assert _SINGLE_TABLE_BLOCK in text, "fixture drift: single-table anchor missing"
    rules_path.write_text(text.replace(_SINGLE_TABLE_BLOCK, registry_block), encoding="utf-8")
    return pack_dir


def test_from_list_resolves_registry_keyed_by_starting_state(tmp_path: Path) -> None:
    """AC-2 happy path: two ``_from:`` pointers resolve into a registry keyed
    by each side-file's ``starting_state``, each a fully-loaded 16-cell
    InteractionTable (not a leftover pointer dict)."""
    pack = load_genre_pack(make_state_graph_pack(tmp_path))
    d = _dogfight(pack)

    assert set(d.interaction_tables) == {"merge", "tail_chase"}
    for state, table in d.interaction_tables.items():
        assert isinstance(table, InteractionTable), (
            f"registry[{state!r}] is {type(table).__name__}, not a resolved InteractionTable"
        )
        assert table.starting_state == state, (
            f"registry key {state!r} must equal the table's starting_state {table.starting_state!r}"
        )
        assert len(table.cells) == 16, f"{state} table should keep its full 4x4 grid"
    assert d.interaction_tables["tail_chase"].maneuvers_consumed == [
        "straight",
        "bank",
        "loop",
        "kill_rotation",
    ]


def test_duplicate_starting_state_fails_loud(tmp_path: Path) -> None:
    """AC-2: two tables claiming the same ``starting_state`` is a load error
    naming the duplicate — never a silent last-one-wins."""
    pack_dir = _copy_pack_with_registry_block(
        tmp_path,
        "interaction_tables:\n"
        "      - _from: dogfight/interactions_mvp.yaml\n"
        "      - _from: dogfight/interactions_mvp.yaml",
    )
    # NOTE the space-containing pattern: GenreLoadError embeds the pack PATH
    # in its message, and pytest's tmp_path contains this TEST'S NAME — a
    # bare match="duplicate" is satisfied by the path itself and passes
    # spuriously in RED. Spaces cannot appear in the tmp_path.
    with pytest.raises(GenreLoadError, match="duplicate interaction_tables starting_state"):
        load_genre_pack(pack_dir)


def test_missing_starting_state_fails_loud(tmp_path: Path) -> None:
    """AC-2: a registry side-file without a ``starting_state`` cannot be keyed
    — load error naming the field, not a silent drop."""
    pack_dir = _copy_pack_with_registry_block(
        tmp_path,
        "interaction_tables:\n      - _from: dogfight/interactions_nokey.yaml",
    )
    bad = pack_dir / "dogfight" / "interactions_nokey.yaml"
    bad.write_text(
        "version: '1'\n"
        "maneuvers_consumed: [straight]\n"
        "cells:\n"
        "  - pair: [straight, straight]\n"
        "    red_view: {}\n"
        "    blue_view: {}\n",
        encoding="utf-8",
    )
    # Space-containing pattern for the same reason as the duplicate test:
    # the tmp_path embeds this test's name ("...missing_starting_state0..."),
    # so a bare match="starting_state" matches the path, not the error.
    with pytest.raises(GenreLoadError, match="missing starting_state"):
        load_genre_pack(pack_dir)


def test_from_list_rejects_parent_traversal(tmp_path: Path) -> None:
    """AC-2 / lang-review #11: registry ``_from:`` entries inherit the same
    pack-relative path safety as the single-table pointer."""
    pack_dir = _copy_pack_with_registry_block(
        tmp_path,
        "interaction_tables:\n      - _from: ../../secrets.yaml",
    )
    with pytest.raises(GenreLoadError, match="parent-directory traversal"):
        load_genre_pack(pack_dir)


def test_single_table_def_autoregisters_backcompat() -> None:
    """AC-2 back-compat: a def with only the legacy single
    ``interaction_table`` (the untouched swn_test_pack) auto-registers
    ``{starting_state: table}`` so the state-machine apply seam has ONE
    lookup shape for both old and new content."""
    from tests._helpers.fixture_packs import load_fixture_pack

    d = _dogfight(load_fixture_pack(SWN_TEST_PACK))
    assert d.interaction_table is not None
    assert set(d.interaction_tables) == {"merge"}
    assert d.interaction_tables["merge"] is d.interaction_table


def test_registry_block_constant_matches_fixture_shape() -> None:
    """Guard for the test infrastructure itself: the builder's registry block
    must keep declaring both fixture tables (a drift here would silently
    weaken every state-graph test)."""
    assert "interactions_mvp.yaml" in _REGISTRY_BLOCK
    assert "interactions_tail_chase.yaml" in _REGISTRY_BLOCK
