"""Content conformance test: spaghetti_western poker → table_resolution (Task 16).

Asserts the poker confrontation in spaghetti_western/rules.yaml has been
converted from the legacy dial/opposed shape to the free-for-all N-seat
table_resolution mode introduced in Phase A (Tasks 1–3).

Skips when sidequest-content is not on disk alongside sidequest-server
(matches the pattern in test_dogfight_content_loading.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import ResolutionMode, WinCondition

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _has_real_content() -> bool:
    return CONTENT_ROOT.is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_content(),
    reason="sidequest-content not on disk alongside sidequest-server",
)


@pytest.fixture(scope="module")
def pack():
    return load_genre_pack(CONTENT_ROOT / "spaghetti_western")


@pytest.fixture(scope="module")
def poker_conf(pack):
    matches = [c for c in pack.rules.confrontations if c.confrontation_type == "poker"]
    assert len(matches) == 1, (
        f"expected exactly one 'poker' confrontation in spaghetti_western, found {len(matches)}"
    )
    return matches[0]


def test_spaghetti_western_poker_is_table_resolution(poker_conf):
    assert poker_conf.resolution_mode == ResolutionMode.table_resolution, (
        f"poker resolution_mode should be table_resolution, got {poker_conf.resolution_mode!r}"
    )


def test_spaghetti_western_poker_win_condition_is_table_showdown(poker_conf):
    assert poker_conf.win_condition == WinCondition.table_showdown, (
        f"poker win_condition should be table_showdown, got {poker_conf.win_condition!r}"
    )


def test_spaghetti_western_poker_table_game_is_poker(poker_conf):
    assert poker_conf.table_game == "poker", (
        f"poker table_game should be 'poker', got {poker_conf.table_game!r}"
    )


def test_spaghetti_western_poker_max_decision_points_at_least_one(poker_conf):
    assert poker_conf.max_decision_points >= 1, (
        f"poker max_decision_points should be >= 1, got {poker_conf.max_decision_points!r}"
    )


def test_spaghetti_western_poker_has_required_beat_ids(poker_conf):
    beat_ids = {b.id for b in poker_conf.beats}
    required = {"fold", "call", "cheat", "accuse", "read_table"}
    missing = required - beat_ids
    assert not missing, (
        f"poker confrontation missing required beats: {sorted(missing)}; have: {sorted(beat_ids)}"
    )


def test_spaghetti_western_poker_no_dial_metrics(poker_conf):
    """table_resolution types read table_state, not the dials — metrics should be absent."""
    assert poker_conf.player_metric is None, (
        f"poker player_metric should be None for table_resolution, got {poker_conf.player_metric!r}"
    )
    assert poker_conf.opponent_metric is None, (
        f"poker opponent_metric should be None for table_resolution, "
        f"got {poker_conf.opponent_metric!r}"
    )
