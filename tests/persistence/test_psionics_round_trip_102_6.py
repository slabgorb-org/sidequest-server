"""Story 102-6 RED — Psionic Effort + System Strain are resume-safe (AC5).

Committed Effort and accumulated System Strain must survive a save/load round
trip. The production encoding is ``GameSnapshot.model_dump_json`` /
``model_validate_json`` (``game/pg/snapshot.py``). No existing test round-trips a
core with NON-empty ``EffortPool.commitments`` or a non-zero
``SystemStrainPool.current`` after a live mutation — this fills that gap and pins
resume-safety once psionics is live.

The Effort commit runs through the swn-resolved module (the AC1 surface), so a
reload that lost the commitment would fail here.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.turn import TurnManager
from sidequest.game.wwn_magic import EffortPool

_PSIONIC_SOURCE = "psionic"


def _psychic_snapshot(*, with_strain: bool = False) -> tuple[GameSnapshot, CreatureCore]:
    core = CreatureCore(
        name="Sael",
        description="A precog of the Aureate Span",
        personality="watchful",
        inventory=Inventory(),
        effort={_PSIONIC_SOURCE: EffortPool(source=_PSIONIC_SOURCE, max=3)},
    )
    if with_strain:
        core.system_strain = SystemStrainPool(current=0, max=10)
    char = Character(
        core=core, char_class="Psychic", race="Human", backstory="A quiet world."
    )
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="aureate_span",
        turn_manager=TurnManager(),
        characters=[char],
    )
    return snap, core


def _round_trip(snap: GameSnapshot) -> GameSnapshot:
    return GameSnapshot.model_validate_json(snap.model_dump_json())


def test_committed_psionic_effort_survives_round_trip():
    """Commit Effort via the swn module, then round-trip the snapshot through the
    production JSON encoding — the commitment (points, duration, label) survives."""
    snap, core = _psychic_snapshot()
    module = get_ruleset_module("swn")
    module.commit_effort(
        core=core,
        source=_PSIONIC_SOURCE,
        points=2,
        duration="scene",
        label="Telepathic Contact",
    )
    assert core.effort[_PSIONIC_SOURCE].available == 1

    reloaded = _round_trip(snap)
    pool = reloaded.characters[0].core.effort[_PSIONIC_SOURCE]
    assert pool.available == 1, "committed Effort must survive reload (resume-safe)"
    assert len(pool.commitments) == 1
    assert pool.commitments[0].points == 2
    assert pool.commitments[0].duration == "scene"
    assert pool.commitments[0].label == "Telepathic Contact"


def test_accumulated_system_strain_survives_round_trip():
    """Accumulate System Strain, then round-trip — current/max survive."""
    from sidequest.genre.models.rules import SystemStrainConfig, WwnConfig

    snap, core = _psychic_snapshot(with_strain=True)
    module = get_ruleset_module("wwn")
    cfg = WwnConfig(
        attribute_map={
            "STRENGTH": "STR",
            "CONSTITUTION": "CON",
            "DEXTERITY": "DEX",
            "INTELLIGENCE": "INT",
            "WISDOM": "WIS",
            "CHARISMA": "CHA",
        },
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
    )
    module.apply_system_strain(
        core=core, kind="temporary", amount=2, source="psionic:psychic_assault", cfg=cfg
    )
    assert core.system_strain.current == 2

    reloaded = _round_trip(snap)
    strain = reloaded.characters[0].core.system_strain
    assert strain is not None, "the strain pool must survive reload"
    assert strain.current == 2, "accumulated System Strain must survive reload"


def test_reloaded_psychic_can_still_activate():
    """Resume-safety end-to-end: after a round-trip the reloaded psychic can
    activate a discipline (the engine surface still resolves against the reloaded
    Effort pool)."""
    from sidequest.genre.models.psionics import PsionicDiscipline

    snap, _core = _psychic_snapshot()
    reloaded = _round_trip(snap)
    reloaded_core = reloaded.characters[0].core

    module = get_ruleset_module("swn")
    discipline = PsionicDiscipline(
        id="telepathic_contact",
        name="Telepathic Contact",
        level=1,
        effort_cost=1,
        duration="scene",
        genre_description="x",
        mechanical_effect="y",
    )
    module.activate_discipline(core=reloaded_core, discipline=discipline, source=_PSIONIC_SOURCE)
    assert reloaded_core.effort[_PSIONIC_SOURCE].available == 2
