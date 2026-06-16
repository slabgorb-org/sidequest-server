"""Story 59-27 (RED/guard): wry_whimsy verbal/social confrontation engagement
— single-beat resolvability (AC3) and the unrouted-confrontation lie-detector
(AC4).

AC3 design decision (Operator, 2026-06-03): a world-initiated menace
AUTO-ENTERS its confrontation type on the turn it appears in fiction (parity
with the combat side's "a weapon drawn fires combat THIS turn") AND a soft
menace stays resolvable in a SINGLE beat via the existing ``resolution: true``
push beat — no new weight-scaling mechanic. These tests pin the single-beat
substrate (the resolution beat exists for every social type) so the auto-enter
rule cannot strand the player in a multi-round grind.

AC4: the 59-1 no-emission lie-detector (``confrontation.unengaged_turn``) must
flag a wry_whimsy turn that reads as a verbal confrontation but routed nothing
— the turn-10 evidence ("convince me he's worth the walk" → confrontation=None,
zero mechanics). This guards that the no-emission watcher is genre-agnostic and
covers a social standoff, not just a combat one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

_CONTENT_PACKS = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
_WRY_WHIMSY = _CONTENT_PACKS / "wry_whimsy"

pytestmark = pytest.mark.skipif(
    not _WRY_WHIMSY.exists(),
    reason="wry_whimsy content pack not available",
)

# wry_whimsy's verbal/social confrontation types (category: social).
_SOCIAL_TYPES = ("audience", "wit_duel", "wonder_shock", "persuasion")


def _pack():
    return load_genre_pack(_WRY_WHIMSY)


# ---------------------------------------------------------------------------
# AC3 — single-beat resolvability: every social confrontation a world menace can
# auto-enter must carry a ``resolution: true`` beat, so a soft menace ends in one
# beat instead of grinding the dials (SOUL "Cut the Dull Bits").
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctype", _SOCIAL_TYPES)
def test_social_confrontation_is_single_beat_resolvable(ctype: str) -> None:
    """AC3 content invariant: each social type the auto-enter rule can instantiate
    offers a single-beat exit (a beat with ``resolution: true``)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None, f"wry_whimsy must define a {ctype!r} confrontation"
    resolution_beats = [b.id for b in cdef.beats if getattr(b, "resolution", False)]
    assert resolution_beats, (
        f"{ctype!r} must carry a resolution:true beat so an auto-entered soft "
        f"menace can resolve in a single beat; found none among "
        f"{[b.id for b in cdef.beats]}"
    )


def test_wonder_shock_look_away_resolves_in_one_beat() -> None:
    """AC3 (poppy-field turn-5 shape): the world-pushed Wonder-Shock the engine
    auto-enters must be escapable in one beat via ``look_away`` — the single-beat
    out the Operator chose over a new weight mechanic."""
    cdef = find_confrontation_def(_pack().rules.confrontations, "wonder_shock")
    assert cdef is not None
    look_away = next((b for b in cdef.beats if b.id == "look_away"), None)
    assert look_away is not None, "wonder_shock must define a 'look_away' beat"
    assert getattr(look_away, "resolution", False) is True, (
        "look_away must carry resolution:true so a soft wonder-shock ends in one beat"
    )


# ---------------------------------------------------------------------------
# AC4 — the no-emission lie-detector flags an unrouted verbal confrontation.
# ---------------------------------------------------------------------------


def test_unrouted_verbal_confrontation_fires_unengaged_watcher(otel_capture) -> None:
    """AC4: a wry_whimsy turn that reads as a verbal confrontation (the Guardian
    of the Gates standing as the contended Other) but routes NO confrontation and
    emits NO intent must trip ``confrontation.unengaged_turn`` — so a winged
    (prose-only) social standoff cannot regress silently. The turn-10 evidence:
    'convince me he's worth the walk' resolved as prose with confrontation=None.
    """
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = None

    result = NarrationTurnResult(
        narration=(
            "The Guardian of the Gates folds his arms. 'No one passes who cannot "
            "say why,' he says, and waits."
        ),
        confrontation=None,
        beat_selections=[],
        npcs_present=[
            NpcMention(
                name="Guardian of the Gates",
                role="gatekeeper",
                pronouns="he/him",
                side="opponent",
            )
        ],
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Dorothy",
        pack=_pack(),
        room=room_for(snap),
    )

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "confrontation.unengaged_turn"
    ]
    assert spans, (
        "an unrouted verbal confrontation (opponent present, no confrontation, no "
        "intent) must trip the confrontation.unengaged_turn lie-detector"
    )
    assert spans[-1].attributes["genre_slug"] == "wry_whimsy"
