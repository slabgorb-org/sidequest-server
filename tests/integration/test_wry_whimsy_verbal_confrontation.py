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
# wry_whimsy social confrontations are Fate Contests (story 153-3). The native
# single-beat ``resolution: true`` affordance (59-27 AC3) was SUPERSEDED by the
# Fate Contest port: a social confrontation now resolves via the 4dF first-to-N
# exchange and its beats are display-only stubs (no dial ``resolution`` flag).
# The well-formedness invariant is now "every social type is a contest with
# display-only beats" — pacing ("Cut the Dull Bits") is the contest's first-to-N
# target, not a native single-beat exit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctype", _SOCIAL_TYPES)
def test_social_confrontation_is_fate_contest(ctype: str) -> None:
    """story 153-3 content invariant: each social type is a Fate Contest with
    display-only beats (no native dial ``kind``/``stat_check``)."""
    cdef = find_confrontation_def(_pack().rules.confrontations, ctype)
    assert cdef is not None, f"wry_whimsy must define a {ctype!r} confrontation"
    mode = (
        cdef.resolution_mode.value
        if hasattr(cdef.resolution_mode, "value")
        else cdef.resolution_mode
    )
    assert mode == "contest", (
        f"{ctype!r} must be a Fate Contest under the Fate binding (got {mode!r})"
    )
    for beat in cdef.beats:
        assert beat.kind is None and beat.stat_check is None, (
            f"{ctype!r} beat {beat.id!r} must be a display-only Contest stub "
            f"(no dial kind/stat_check); got kind={beat.kind!r}, stat_check={beat.stat_check!r}"
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
