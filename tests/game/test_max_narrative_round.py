"""Unit coverage for the ``SqliteStore.max_narrative_round()`` helper.

Story 45-11 AC4. The invariant span at the end of ``_execute_narration_turn``
needs ``MAX(round_number) FROM narrative_log`` on every tick. The query is
trivial; getting it wrong (returning the last-inserted row, or crashing on
an empty table) is the kind of mistake that surfaces at 72-round playtest
sessions, not at 5.

The helper belongs alongside ``recent_narrative()`` in
``sidequest/game/persistence.py`` — both read narrative_log rows for the
caller's analysis. RED until the helper lands.
"""

from __future__ import annotations

from sidequest.game.persistence import SqliteStore
from sidequest.game.session import NarrativeEntry


def _entry(round_: int, content: str = "x") -> NarrativeEntry:
    return NarrativeEntry(
        timestamp=0,
        round=round_,
        author="narrator",
        content=content,
        tags=[],
    )


def test_max_narrative_round_empty_log_returns_zero() -> None:
    """Brand-new session: narrative_log is empty.

    The invariant span fires on turn 0 and reads the helper before any row
    has been written. The helper MUST return 0 (not None, not raise) so the
    span attribute ``max_narrative_round`` is plottable on a chart that
    starts with axis at 0.
    """
    store = SqliteStore.open_in_memory()
    try:
        assert store.max_narrative_round() == 0
    finally:
        store.close()


def test_max_narrative_round_single_row() -> None:
    """One row written: the helper returns that row's round_number."""
    store = SqliteStore.open_in_memory()
    try:
        store.append_narrative(_entry(42))
        assert store.max_narrative_round() == 42
    finally:
        store.close()


def test_max_narrative_round_returns_max_not_last_inserted() -> None:
    """Insertion order is not monotonic: the helper must return SQL MAX,
    not the last row's round_number.

    Synthetic regression: a Lane B fix that wrote the helper as
    ``ORDER BY id DESC LIMIT 1`` would silently regress here — the bug
    looks identical to the original Felix divergence (helper says 1,
    truth says 7). Locking in MAX semantics with this case prevents the
    regression class.
    """
    store = SqliteStore.open_in_memory()
    try:
        for r in (3, 7, 2, 5, 1):
            store.append_narrative(_entry(r))
        assert store.max_narrative_round() == 7
    finally:
        store.close()


def test_max_narrative_round_after_many_rows() -> None:
    """100 rows with monotonic round_numbers — the helper still returns
    the actual maximum, not row count."""
    store = SqliteStore.open_in_memory()
    try:
        for r in range(1, 101):
            store.append_narrative(_entry(r))
        assert store.max_narrative_round() == 100
    finally:
        store.close()
