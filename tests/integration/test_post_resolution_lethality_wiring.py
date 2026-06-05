"""Wiring test — the post-resolution lethality seam is reached from PRODUCTION
(EH-2 burning_peace playtest 2026-06-05).

The unit tests in ``tests/server/test_post_resolution_lethality.py`` prove the
seam's behavior in isolation. This proves it is actually WIRED into the live
combat path: drive a real space_opera reprisal that drops a 1-HP player to 0,
resolving ``opponent_victory`` — the encounter must then carry a post-resolution
lethality consequence on the player (a downed/recovering status) AND emit the
``encounter.post_resolution_lethality`` span. Without the wiring the player is
parked at 0 HP with no status and no span (the measured bug).

space_opera ships pc=`dying` (a LETHAL verdict), so the wired consequence here is
the "Downed" flag + the decision span — the lie-detector firing on the real path.

Reuses the space_opera reprisal harness from ``test_opponent_reprisal_e2e``.
Skips gracefully when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from sidequest.server.post_resolution_lethality import SPAN_POST_RESOLUTION_LETHALITY
from tests._helpers.genre_paths import PackNotFound, find_pack_path
from tests.integration.test_opponent_reprisal_e2e import (
    PLAYER,
    _drive_player_shoot,
    _make_encounter,
    _make_snapshot,
)


def _load_space_opera_pack():
    from sidequest.genre.loader import load_genre_pack

    try:
        path = find_pack_path("space_opera")
    except PackNotFound:
        return None
    return load_genre_pack(path)


def test_player_kill_fires_post_resolution_lethality(otel_capture):
    """A 1-HP player dropped by the reprisal must trigger the post-resolution
    lethality seam on the real dispatch path: the encounter resolves against the
    player AND the seam emits its decision span + flags the player."""
    pack = _load_space_opera_pack()
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    snap = _make_snapshot(player_ac=2, player_hp=1)  # guaranteed-hit reprisal drops to 0
    enc = _make_encounter()

    _drive_player_shoot(snap, enc, pack, broadcasts=[])

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert enc.resolved and enc.outcome == "opponent_victory", (
        f"the reprisal must resolve opponent_victory against the 1-HP player; "
        f"resolved={enc.resolved} outcome={enc.outcome!r}"
    )

    # The lie-detector span must fire on the production path.
    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == SPAN_POST_RESOLUTION_LETHALITY
    ]
    assert len(spans) == 1, (
        f"the post-resolution lethality seam must fire exactly once on a real "
        f"player defeat; got {len(spans)} {SPAN_POST_RESOLUTION_LETHALITY} spans "
        f"(all spans: {[s.name for s in otel_capture.get_finished_spans()]})"
    )

    # space_opera pc=`dying` → lethal branch: PC stays down + Downed flag.
    assert (spans[0].attributes or {}).get("decision") == "lethal_down"
    assert any("Downed" in s.text for s in player_core.statuses), (
        f"the defeated PC must carry a Downed status (not parked silently at 0/10); "
        f"got {[s.text for s in player_core.statuses]}"
    )
