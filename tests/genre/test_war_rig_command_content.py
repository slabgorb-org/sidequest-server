"""Story 86-7 (RED): road_warrior's war_rig gains the road_boss Command layer.

86-6 shipped the minimal war_rig confrontation with the four station verbs
(steer/shoot/repair/scan) and explicitly deferred "the road_boss Command Points
layer is 86-7" (see road_warrior/rules.yaml). This story adds that layer to the
CONTENT so the crew can actually reach it in play ("Verify Wiring, Not Just
Existence" — the engine CP/crisis code is inert until a pack points a verb at it).

Per spec §6 this stays MINIMAL: declare the new command-economy verbs and the
Deal-With-a-Crisis verb on the existing war_rig confrontation. Full vessel stat
blocks / mount_slot remap / lethality calibration remain 86-5.

**TEA contract (open to Dev refinement):** the war_rig confrontation's beats grow
to include the two spendable CP actions (``above_and_beyond``, ``support_department``)
and ``deal_with_crisis``, without regressing the four 86-6 station verbs. The free
``do_your_duty`` action is the implicit default (standard resolution) and is pinned
at the unit level, not required as a distinct content beat.

Mirrors tests/genre/test_war_rig_crew_content.py. Skips when sidequest-content is
not on disk. RED until Dev authors the command/crisis beats in road_warrior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.loader import load_genre_pack

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _has_real_content() -> bool:
    return CONTENT_ROOT.is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_content(),
    reason="sidequest-content not on disk alongside sidequest-server",
)


@pytest.fixture(scope="module")
def war_rig_conf():
    pack = load_genre_pack(CONTENT_ROOT / "road_warrior")
    matches = [c for c in pack.rules.confrontations if c.confrontation_type == "war_rig"]
    assert len(matches) == 1, (
        f"expected exactly one 'war_rig' confrontation in road_warrior, found {len(matches)}"
    )
    return matches[0]


def test_war_rig_keeps_the_86_6_station_verbs(war_rig_conf):
    """The command layer must ADD to, not replace, the four station verbs — a
    regression here would break 86-6's cooperative round."""
    beat_ids = {b.id for b in war_rig_conf.beats}
    required = {"steer", "shoot", "repair", "scan"}
    missing = required - beat_ids
    assert not missing, (
        f"war_rig must keep its 86-6 station verbs; missing {sorted(missing)} (have {sorted(beat_ids)})"
    )


def test_war_rig_declares_the_spendable_cp_actions(war_rig_conf):
    """AC1: the two CP-spending actions must be reachable as authored verbs so a
    player can actually call Above and Beyond / Support Department in play."""
    beat_ids = {b.id for b in war_rig_conf.beats}
    required = {"above_and_beyond", "support_department"}
    missing = required - beat_ids
    assert not missing, (
        f"war_rig missing the spendable CP verbs {sorted(missing)} (have {sorted(beat_ids)})"
    )


def test_war_rig_declares_deal_with_crisis(war_rig_conf):
    """AC2: Deal With a Crisis is the player's verb for answering the d10 crisis
    table; it must be an authored beat on the confrontation."""
    beat_ids = {b.id for b in war_rig_conf.beats}
    assert "deal_with_crisis" in beat_ids, (
        f"war_rig must declare the 'deal_with_crisis' beat (have {sorted(beat_ids)})"
    )
