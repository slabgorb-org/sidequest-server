"""Content conformance test: tea_and_murder auction → table_resolution (Task 17).

Asserts the auction confrontation in tea_and_murder/rules.yaml has been
converted from the legacy dial/opposed shape to the free-for-all N-seat
table_resolution mode introduced in Phase A (Tasks 1–3).

Skips when sidequest-content is not on disk alongside sidequest-server
(matches the pattern in test_poker_table_content.py).
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
    return load_genre_pack(CONTENT_ROOT / "tea_and_murder")


@pytest.fixture(scope="module")
def auction_conf(pack):
    matches = [c for c in pack.rules.confrontations if c.confrontation_type == "auction"]
    assert len(matches) == 1, (
        f"expected exactly one 'auction' confrontation in tea_and_murder, "
        f"found {len(matches)}"
    )
    return matches[0]


def test_tea_and_murder_auction_is_table_resolution(auction_conf):
    assert auction_conf.resolution_mode == ResolutionMode.table_resolution, (
        f"auction resolution_mode should be table_resolution, got {auction_conf.resolution_mode!r}"
    )


def test_tea_and_murder_auction_win_condition_is_table_showdown(auction_conf):
    assert auction_conf.win_condition == WinCondition.table_showdown, (
        f"auction win_condition should be table_showdown, got {auction_conf.win_condition!r}"
    )


def test_tea_and_murder_auction_table_game_is_auction(auction_conf):
    assert auction_conf.table_game == "auction", (
        f"auction table_game should be 'auction', got {auction_conf.table_game!r}"
    )


def test_tea_and_murder_auction_max_decision_points_at_least_one(auction_conf):
    assert auction_conf.max_decision_points >= 1, (
        f"auction max_decision_points should be >= 1, got {auction_conf.max_decision_points!r}"
    )


def test_tea_and_murder_auction_has_required_beat_ids(auction_conf):
    beat_ids = {b.id for b in auction_conf.beats}
    required = {"raise_bid", "withdraw", "read_room"}
    missing = required - beat_ids
    assert not missing, (
        f"auction confrontation missing required beats: {sorted(missing)}; "
        f"have: {sorted(beat_ids)}"
    )


def test_tea_and_murder_auction_no_dial_metrics(auction_conf):
    """table_resolution types read table_state, not the dials — metrics should be absent."""
    assert auction_conf.player_metric is None, (
        f"auction player_metric should be None for table_resolution, "
        f"got {auction_conf.player_metric!r}"
    )
    assert auction_conf.opponent_metric is None, (
        f"auction opponent_metric should be None for table_resolution, "
        f"got {auction_conf.opponent_metric!r}"
    )
