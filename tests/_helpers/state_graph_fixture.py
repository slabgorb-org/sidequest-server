"""Fixture builder for the ADR-153 §3 dogfight state-graph tests (158-40).

Builds a *temporary copy* of ``swn_test_pack`` rewritten to the multi-state
shape the state graph introduces:

- the dogfight def declares an ``interaction_tables:`` list of ``_from:``
  pointers (merge + tail_chase) instead of the single ``interaction_table:``
- the merge table's ``[straight, loop]`` cell ("Blue reverses onto Red's six")
  carries ``next_state: tail_chase``
- the tail_chase table's ``[loop, loop]`` cell is rewritten to the
  extend-and-return trigger shape (no gun solution, ``closure: opening_fast``
  on both views) so a duel can walk tail_chase → merge via the reset

The copy lives in ``tmp_path`` so the shared ``swn_test_pack`` fixture stays
byte-identical for every other suite — per the 96-1 doctrine (tests test
fixtures) and because in the RED phase these shapes are *rejected* by the
pydantic models (``extra="forbid"``): mutating the shared fixture would turn
every unrelated dogfight test red for the wrong reason.

No silent fallback: every rewrite asserts its anchor text/cell exists so a
fixture drift fails loudly here, not as a confusing downstream miss.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from tests._helpers.fixture_packs import SWN_TEST_PACK, fixture_pack_path

_SINGLE_TABLE_BLOCK = "interaction_table:\n      _from: dogfight/interactions_mvp.yaml"
_REGISTRY_BLOCK = (
    "interaction_tables:\n"
    "      - _from: dogfight/interactions_mvp.yaml\n"
    "      - _from: dogfight/interactions_tail_chase.yaml"
)

# The merge cell that transitions into tail_chase — "Blue reverses onto
# Red's six" is the canonical Plan 4 example transition.
TRANSITION_PAIR: list[str] = ["straight", "loop"]
# The tail_chase cell rewritten into the extend-and-return trigger.
EXTEND_RETURN_PAIR: list[str] = ["loop", "loop"]


def _rewrite_cell(table_path: Path, pair: list[str], mutate) -> None:
    """Load a table YAML, apply ``mutate(cell)`` to the cell matching ``pair``,
    and write it back. Raises if the pair is absent (fixture drift)."""
    doc = yaml.safe_load(table_path.read_text(encoding="utf-8"))
    matches = [c for c in doc["cells"] if c["pair"] == pair]
    if len(matches) != 1:
        raise AssertionError(
            f"fixture drift: expected exactly one {pair!r} cell in {table_path.name}, "
            f"found {len(matches)}"
        )
    mutate(matches[0])
    table_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def make_state_graph_pack(tmp_path: Path, *, transition_target: str = "tail_chase") -> Path:
    """Copy swn_test_pack into ``tmp_path`` and rewrite it to the state-graph
    shape. ``transition_target`` overrides the merge ``[straight, loop]``
    cell's ``next_state`` — pass an unregistered state id (e.g.
    ``"ghost_state"``) to build the dangling-transition pack for the
    fail-loud tests.

    Returns the pack directory (pass to ``load_genre_pack``).
    """
    pack_dir = tmp_path / "swn_state_graph_pack"
    shutil.copytree(fixture_pack_path(SWN_TEST_PACK), pack_dir)

    # 1. rules.yaml: single table pointer -> registry list
    rules_path = pack_dir / "rules.yaml"
    text = rules_path.read_text(encoding="utf-8")
    if _SINGLE_TABLE_BLOCK not in text:
        raise AssertionError(
            "fixture drift: swn_test_pack rules.yaml no longer carries the "
            "single dogfight interaction_table _from: pointer this builder rewrites"
        )
    rules_path.write_text(text.replace(_SINGLE_TABLE_BLOCK, _REGISTRY_BLOCK), encoding="utf-8")

    dogfight_dir = pack_dir / "dogfight"

    # 2. merge table: [straight, loop] transitions into the target state
    def _set_next_state(cell: dict) -> None:
        cell["next_state"] = transition_target

    _rewrite_cell(dogfight_dir / "interactions_mvp.yaml", TRANSITION_PAIR, _set_next_state)

    # 3. tail_chase table: [loop, loop] becomes the extend-and-return trigger
    #    (no gun solution anywhere + at least one opening_fast closure)
    def _make_extend_return_trigger(cell: dict) -> None:
        for view_key in ("red_view", "blue_view"):
            view = cell[view_key]
            view["gun_solution"] = False
            view["closure"] = "opening_fast"

    _rewrite_cell(
        dogfight_dir / "interactions_tail_chase.yaml",
        EXTEND_RETURN_PAIR,
        _make_extend_return_trigger,
    )

    return pack_dir
