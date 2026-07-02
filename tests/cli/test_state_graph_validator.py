"""ADR-153 §3 state graph — pack-validator guards (158-40, AC-6 + AC-7).

Per ``feedback_no_content_in_unit_tests`` (and the 96-1 doctrine), the live
pack's dogfight graph shape is a CONTENT invariant — it belongs in the pack
validator, never pytest-pinned against live space_opera. These synthetic-pack
tests prove the validator logic; live content is enforced by running
``python -m sidequest.cli.validate`` (the Dev green gate for Task 6/7).

New rules under test (all RED — ``validate_rules_in_pack`` has no state-graph
checks yet):

  - graph closure: every cell ``next_state`` names a registered table
    (a dangling transition is authoring error, fail loud — AC-7)
  - reachability: every registered table is reachable from the entry state
    (an unreachable table is dead content — No Stubbing)
  - view-key firewall: cell views carry descriptor geometry + gun_solution
    only — a damage/hull key inside a view is the ADR-153 §2 violation the
    pydantic model CANNOT catch (views are open dicts)
  - schema↔registry consistency: every ``status: mvp`` starting-state in
    ``dogfight/descriptor_schema.yaml`` has a registered table and vice versa
    (this is what forces the six-state space_opera graph once Task 6 promotes
    beam/overhead and adds scissors/overshoot)
  - ``_from:`` pointers are followed when checking the graph (live content
    uses side-files, not inline tables)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from sidequest.cli.validate.rules import validate_rules_in_pack


def _write_pack(pack_dir: Path, rules_yaml: str, schema_yaml: str | None = None) -> Path:
    (pack_dir / "rules.yaml").write_text(textwrap.dedent(rules_yaml), encoding="utf-8")
    if schema_yaml is not None:
        dogfight_dir = pack_dir / "dogfight"
        dogfight_dir.mkdir(exist_ok=True)
        (dogfight_dir / "descriptor_schema.yaml").write_text(
            textwrap.dedent(schema_yaml), encoding="utf-8"
        )
    return pack_dir


def _inline_table(state: str, *, next_state: str | None = None, view_extra: str = "") -> str:
    """One-cell inline table YAML fragment (indented for the confrontation
    list item). The single-cell shape keeps the closure/reachability logic
    under test without 16-cell noise — cross-product completeness is a
    separate rule."""
    ns = f"\n            next_state: {next_state}" if next_state else ""
    return f"""\
          - version: "1"
            starting_state: {state}
            maneuvers_consumed: [straight]
            cells:
              - pair: [straight, straight]
                name: {state} holding pattern
                narration_hint: The {state} engagement continues.
                red_view: {{gun_solution: false{view_extra}}}
                blue_view: {{gun_solution: false}}{ns}
"""


def _dogfight_def(tables_block: str) -> str:
    return (
        "confrontations:\n"
        "  - type: dogfight\n"
        "    label: Fighter Duel\n"
        "    category: combat\n"
        "    resolution_mode: sealed_letter_lookup\n"
        "    win_condition: hp_depletion\n"
        "    interaction_tables:\n" + tables_block
    )


# ---------------------------------------------------------------------------
# AC-7: graph closure
# ---------------------------------------------------------------------------


def test_rejects_dangling_next_state(tmp_path: Path) -> None:
    """A cell transitioning to a state with no registered table is an error
    naming the dangling state."""
    pack_dir = _write_pack(
        tmp_path,
        _dogfight_def(_inline_table("merge", next_state="scissors")),
    )
    result = validate_rules_in_pack(pack_dir)

    assert not result.success, (
        "a next_state with no registered table must be a validation error "
        "(dangling transition = the duel would fail loud mid-flight)"
    )
    messages = [i.message for i in result.errors]
    assert any("scissors" in m and "next_state" in m for m in messages), (
        f"expected an error naming the dangling state 'scissors', got {messages!r}"
    )


def test_accepts_closed_graph(tmp_path: Path) -> None:
    """Non-regression guard: a closed two-state graph (merge ⇄ tail_chase)
    produces no graph errors."""
    pack_dir = _write_pack(
        tmp_path,
        _dogfight_def(
            _inline_table("merge", next_state="tail_chase")
            + _inline_table("tail_chase", next_state="merge")
        ),
    )
    result = validate_rules_in_pack(pack_dir)

    graph_errors = [
        i.message for i in result.errors if "next_state" in i.message or "reachable" in i.message
    ]
    assert not graph_errors, f"a closed graph must not be flagged, got {graph_errors!r}"


# ---------------------------------------------------------------------------
# AC-6: reachability (dead content is No-Stubbing territory)
# ---------------------------------------------------------------------------


def test_rejects_unreachable_table(tmp_path: Path) -> None:
    """A registered table no transition can ever reach (and that is not the
    entry state) is dead content — exactly the orphaned
    ``interactions_tail_chase.yaml`` failure mode this story retires."""
    pack_dir = _write_pack(
        tmp_path,
        _dogfight_def(
            _inline_table("merge")  # entry, no outbound transitions
            + _inline_table("beam")  # nothing transitions into beam
        ),
    )
    result = validate_rules_in_pack(pack_dir)

    assert not result.success
    messages = [i.message for i in result.errors]
    assert any("beam" in m and "reachable" in m for m in messages), (
        f"expected an error naming unreachable state 'beam', got {messages!r}"
    )


# ---------------------------------------------------------------------------
# AC-6: view-key firewall (ADR-153 §2) — views are open dicts, so the
# validator is the ONLY enforcement point for damage keys inside them
# ---------------------------------------------------------------------------


def test_rejects_damage_key_inside_cell_view(tmp_path: Path) -> None:
    pack_dir = _write_pack(
        tmp_path,
        _dogfight_def(_inline_table("merge", view_extra=", damage: 2")),
    )
    result = validate_rules_in_pack(pack_dir)

    assert not result.success, (
        "a damage key inside a cell view violates the ADR-153 §2 firewall "
        "(positioning cells carry geometry + gun_solution only; SWN owns damage)"
    )
    messages = [i.message for i in result.errors]
    assert any("damage" in m for m in messages), (
        f"expected an error naming the 'damage' view key, got {messages!r}"
    )


# ---------------------------------------------------------------------------
# AC-6: descriptor-schema ↔ registry consistency (forces the six-state
# space_opera graph on live content once Task 6 lands)
# ---------------------------------------------------------------------------

_SCHEMA_TWO_MVP = """\
    version: "0.2.0"
    starting_states:
      - id: merge
        status: mvp
      - id: tail_chase
        status: mvp
      - id: beam
        status: future
"""


def test_rejects_mvp_schema_state_without_table(tmp_path: Path) -> None:
    """Every ``status: mvp`` starting-state in the descriptor schema must have
    a registered table — a promoted state with no table is a stub."""
    pack_dir = _write_pack(
        tmp_path,
        _dogfight_def(_inline_table("merge")),
        schema_yaml=_SCHEMA_TWO_MVP,
    )
    result = validate_rules_in_pack(pack_dir)

    assert not result.success
    messages = [i.message for i in result.errors]
    assert any("tail_chase" in m for m in messages), (
        f"expected an error naming the table-less mvp state 'tail_chase', got {messages!r}"
    )


def test_rejects_table_without_mvp_schema_state(tmp_path: Path) -> None:
    """The inverse: a registered table whose state the schema does not declare
    as mvp (undeclared or still ``future``) — the schema is the states'
    source of truth and must be promoted in the same change."""
    pack_dir = _write_pack(
        tmp_path,
        _dogfight_def(
            _inline_table("merge", next_state="beam") + _inline_table("beam", next_state="merge")
        ),
        schema_yaml=_SCHEMA_TWO_MVP.replace("      - id: tail_chase\n        status: mvp\n", ""),
    )
    result = validate_rules_in_pack(pack_dir)

    assert not result.success
    messages = [i.message for i in result.errors]
    assert any("beam" in m and ("mvp" in m or "schema" in m) for m in messages), (
        f"expected an error flagging table 'beam' vs the schema, got {messages!r}"
    )


# ---------------------------------------------------------------------------
# Live-content shape: the registry arrives as _from: pointers — the checks
# must follow them, not skip pointer-shaped entries
# ---------------------------------------------------------------------------


def test_follows_from_pointers_when_checking_graph(tmp_path: Path) -> None:
    pack_dir = tmp_path
    dogfight_dir = pack_dir / "dogfight"
    dogfight_dir.mkdir()
    (dogfight_dir / "interactions_merge.yaml").write_text(
        textwrap.dedent(
            """\
            version: "1"
            starting_state: merge
            maneuvers_consumed: [straight]
            cells:
              - pair: [straight, straight]
                name: merge pass
                narration_hint: They pass.
                red_view: {gun_solution: false}
                blue_view: {gun_solution: false}
                next_state: scissors
            """
        ),
        encoding="utf-8",
    )
    (pack_dir / "rules.yaml").write_text(
        textwrap.dedent(
            """\
            confrontations:
              - type: dogfight
                label: Fighter Duel
                category: combat
                resolution_mode: sealed_letter_lookup
                win_condition: hp_depletion
                interaction_tables:
                  - _from: dogfight/interactions_merge.yaml
            """
        ),
        encoding="utf-8",
    )
    result = validate_rules_in_pack(pack_dir)

    assert not result.success, (
        "the dangling next_state lives in a _from: side-file — the validator "
        "must resolve pointers exactly like the loader, not skip them"
    )
    messages = [i.message for i in result.errors]
    assert any("scissors" in m for m in messages), (
        f"expected the side-file's dangling 'scissors' transition flagged, got {messages!r}"
    )
