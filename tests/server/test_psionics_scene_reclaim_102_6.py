"""Story 102-6 RED — Effort reclaim lifecycle for SWN psionic sessions (AC2).

Committed Effort reclaims on scene/day boundaries. The WWN reclaim is already
wired into ``Session.end_scene`` (story 102-3 era) but the gate is
``self._ruleset == "wwn"`` (``server/session.py``) — so a space_opera (swn)
psychic's scene-committed Effort is NEVER reclaimed at scene end today. AC2
broadens the boundary surface to the SWN family.

These tests drive the REAL ``Session.end_scene`` and the swn-resolved module's
day reclaim — never a reimplementation, never a source grep. The reclaim is
observable on the GM panel via ``swn.effort.reclaim`` (the ``{ruleset}``-namespaced
span).
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.wwn_magic import EffortCommitment, EffortPool
from sidequest.server.session import Session

_PSIONIC_SOURCE = "psionic"


def _psychic_pc() -> Character:
    """A swn psychic whose psionic Effort pool holds one scene + one day commit."""
    core = CreatureCore(
        name="Sael",
        description="A precog of the Aureate Span",
        personality="watchful",
        inventory=Inventory(),
    )
    core.effort[_PSIONIC_SOURCE] = EffortPool(
        source=_PSIONIC_SOURCE,
        max=3,
        commitments=[
            EffortCommitment(points=1, duration="scene", label="Telepathic Contact"),
            EffortCommitment(points=1, duration="day", label="Precognitive Ward"),
        ],
    )
    return Character(
        core=core,
        char_class="Psychic",
        race="Human",
        backstory="Trained on a quiet world.",
    )


def _swn_snapshot(pc: Character) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        turn_manager=TurnManager(),
        characters=[pc],
    )


# ===========================================================================
# Scene boundary — end_scene reclaims scene Effort for a swn session
# ===========================================================================


def test_end_scene_reclaims_scene_effort_for_swn_session():
    """A swn-bound session must reclaim the scene commitment, leave day intact."""
    pc = _psychic_pc()
    session = Session(_swn_snapshot(pc), ruleset="swn")

    pool = pc.core.effort[_PSIONIC_SOURCE]
    assert pool.available == 1  # 3 - (scene 1 + day 1)

    session.end_scene("scene_end", turn=1)

    pool = pc.core.effort[_PSIONIC_SOURCE]
    assert pool.available == 2, "scene Effort reclaimed: 1 -> 2"
    durations = [c.duration for c in pool.commitments]
    assert "scene" not in durations
    assert "day" in durations, "day commitment survives the scene boundary"


def test_end_scene_emits_swn_namespaced_reclaim_span(otel_capture):
    """The reclaim is observable on the GM panel as ``swn.effort.reclaim``."""
    pc = _psychic_pc()
    session = Session(_swn_snapshot(pc), ruleset="swn")

    session.end_scene("scene_end", turn=1)

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "swn.effort.reclaim"]
    assert len(spans) == 1, (
        "a swn psionic session must emit swn.effort.reclaim at scene end; got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert spans[0].attributes["actor"] == "Sael"
    assert spans[0].attributes["source"] == _PSIONIC_SOURCE
    assert spans[0].attributes["trigger"] == "scene"


def test_dial_session_does_not_reclaim_psionic_effort():
    """Regression guard: a dial session leaves the psionic pool untouched —
    broadening the gate to swn must not loosen it to every ruleset."""
    pc = _psychic_pc()
    session = Session(_swn_snapshot(pc), ruleset="dial")

    session.end_scene("scene_end", turn=1)

    pool = pc.core.effort[_PSIONIC_SOURCE]
    assert pool.available == 1, "dial session must not reclaim"
    assert len(pool.commitments) == 2


# ===========================================================================
# Day boundary — day reclaim restores day-committed Effort under SWN
# ===========================================================================


def test_swn_day_reclaim_drops_day_commitments_and_is_namespaced(otel_capture):
    """A day advance (long rest) on the swn-resolved module drops day + scene
    commitments and emits ``swn.effort.reclaim`` — Effort reclaim must accept the
    SWN config, not only WwnConfig."""
    from sidequest.genre.models.rules import SwnConfig

    pc = _psychic_pc()
    module = get_ruleset_module("swn")

    module.reclaim_day_and_refresh(
        core=pc.core,
        comfortable=True,
        cfg=SwnConfig(),
    )

    pool = pc.core.effort[_PSIONIC_SOURCE]
    assert pool.available == 3, "a comfortable day reclaim restores all Effort"
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "swn.effort.reclaim"]
    assert spans, "swn day reclaim must emit swn.effort.reclaim"
