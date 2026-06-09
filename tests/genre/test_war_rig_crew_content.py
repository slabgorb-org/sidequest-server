"""Story 86-6 (RED): road_warrior declares a war_rig table_resolution confrontation.

The engine work is inert until a pack actually points a confrontation at it
("Verify Wiring, Not Just Existence"). Per the design spec §6, 86-6 ships a
MINIMAL playable ``war_rig`` confrontation in road_warrior/rules.yaml
(``resolution_mode: table_resolution``, ``table_game: war_rig_crew``, station
beats) — enough to prove the wiring. Full vessel stat blocks / mount_slot remap
/ lethality calibration are 86-5 (Plan 5), NOT this story.

Mirrors tests/genre/test_auction_table_content.py. Skips when sidequest-content
is not on disk alongside sidequest-server.

RED until Dev authors the war_rig confrontation def in road_warrior/rules.yaml.
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
    return load_genre_pack(CONTENT_ROOT / "road_warrior")


@pytest.fixture(scope="module")
def war_rig_conf(pack):
    matches = [c for c in pack.rules.confrontations if c.confrontation_type == "war_rig"]
    assert len(matches) == 1, (
        f"expected exactly one 'war_rig' confrontation in road_warrior, found {len(matches)}"
    )
    return matches[0]


def test_war_rig_is_table_resolution(war_rig_conf):
    assert war_rig_conf.resolution_mode == ResolutionMode.table_resolution, (
        f"war_rig resolution_mode should be table_resolution, got {war_rig_conf.resolution_mode!r}"
    )


def test_war_rig_table_game_is_war_rig_crew(war_rig_conf):
    assert war_rig_conf.table_game == "war_rig_crew", (
        f"war_rig table_game should be 'war_rig_crew', got {war_rig_conf.table_game!r}"
    )


def test_war_rig_win_condition_is_table_showdown(war_rig_conf):
    """table_resolution types read table_state for victory, not the dials."""
    assert war_rig_conf.win_condition == WinCondition.table_showdown, (
        f"war_rig win_condition should be table_showdown, got {war_rig_conf.win_condition!r}"
    )


def test_war_rig_declares_station_verbs(war_rig_conf):
    """The crew need concurrent station verbs — at minimum the four 86-6
    stations beyond the road_boss command layer (which is 86-7)."""
    beat_ids = {b.id for b in war_rig_conf.beats}
    required = {"steer", "shoot", "repair", "scan"}
    missing = required - beat_ids
    assert not missing, (
        f"war_rig confrontation missing station beats: {sorted(missing)}; have: {sorted(beat_ids)}"
    )


def test_war_rig_no_dial_metrics(war_rig_conf):
    """table_resolution reads table_state, not the dual dials — metrics absent."""
    assert war_rig_conf.player_metric is None, (
        f"war_rig player_metric should be None for table_resolution, "
        f"got {war_rig_conf.player_metric!r}"
    )
    assert war_rig_conf.opponent_metric is None, (
        f"war_rig opponent_metric should be None for table_resolution, "
        f"got {war_rig_conf.opponent_metric!r}"
    )
