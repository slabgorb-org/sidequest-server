"""Story 102-6 RED — Psionic discipline activation wired into LIVE dispatch.

AC1 (activation live) + AC3 (strain ledger unity) + AC6 (wiring): a psychic PC
activates a discipline through the SAME dispatch entry points the WWN cast spine
uses — the free-text ``magic_working`` path (intent router → ``run_dispatch_bank``)
and the beat path (``BeatSelection`` sidecar) — with Effort instead of spell-slot
accounting. The story forbids a parallel "psionics dispatch": reuse the cast spine.

OBSERVABLE CONTRACT (pinned via OTEL spans + pool/strain state, per CLAUDE.md
"No Source-Text Wiring Tests"):

  * A named discipline activation emits a discipline-activation span (shape per
    ``wwn.spell.cast``: actor + the discipline id) AND ``{ruleset}.effort.commit``
    with the pool decremented.
  * 0 free Effort → LOUD: a refused activation span or a
    ``dispatch_engagement.magic_working.mismatch`` — never a silent success.
  * A strain-costing push routes its System Strain through the SAME
    ``core.system_strain`` counter the lethality seam uses (no forked strain
    field), emitting ``{ruleset}.system_strain.delta`` with psionic source
    attribution.
  * Native-ruleset genres emit ZERO ``swn.*`` / psionic spans (slug honesty).

ASSUMED NET-NEW CONTRACT (documented in the TEA assessment; reached at call time
so the module always collects and the RED failure is crisp):
  * Model ``sidequest/genre/models/psionics.py`` — ``PsionicDiscipline``
    (id, name, level, effort_cost, duration, save, genre_description,
    mechanical_effect) + ``PsionicDisciplineCatalog`` (version, disciplines),
    parallel to ``wwn_spell.py``.
  * ``GenrePack.psionic_discipline_catalog`` resolved per world/genre tier.
  * ``BeatSelection.discipline_id`` sidecar, parallel to ``spell_id`` /
    ``mutation_id``.

The router is never driven live here — the dispatch bank is fed a hand-built
``DispatchPackage`` (router test discipline, mirroring 102-3).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.turn import TurnManager
from sidequest.game.wwn_magic import EffortCommitment, EffortPool
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

_ATTRIBUTE_MAP = {
    "STRENGTH": "Might",
    "CONSTITUTION": "Vigor",
    "DEXTERITY": "Grace",
    "INTELLIGENCE": "Lore",
    "WISDOM": "Wit",
    "CHARISMA": "Bearing",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_STATS = {name: 10 for name in _ABILITY_SCORE_NAMES}

_PSYCHIC = "Sael"
_DISCIPLINE_ID = "telepathic_contact"
_DISCIPLINE_DISPLAY = "Telepathic Contact"
_PSIONIC_SOURCE = "psionic"


# ---------------------------------------------------------------------------
# Net-new model contract — reached at call time (ImportError == crisp RED)
# ---------------------------------------------------------------------------


def _discipline_catalog() -> Any:
    """Build a psionic discipline catalog from the assumed net-new model.

    One free-Effort discipline (telepathic_contact, cost 1) plus one
    strain-pushing discipline (psychic_assault, cost 1, strain 1) for the AC3
    push path.
    """
    from sidequest.genre.models.psionics import PsionicDiscipline, PsionicDisciplineCatalog

    return PsionicDisciplineCatalog(
        disciplines=[
            PsionicDiscipline(
                id=_DISCIPLINE_ID,
                name=_DISCIPLINE_DISPLAY,
                level=1,
                effort_cost=1,
                duration="scene",
                save=None,
                genre_description="The psychic brushes another mind and hears its surface song.",
                mechanical_effect="Reads the target's surface thoughts for the scene.",
            ),
            PsionicDiscipline(
                id="psychic_assault",
                name="Psychic Assault",
                level=1,
                effort_cost=1,
                duration="scene",
                save="mental",
                strain_cost=1,
                genre_description="A spike of raw will drives into the target's skull.",
                mechanical_effect="Mental save or take psychic damage; caster takes 1 System Strain.",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_swn_pack() -> Any:
    """A MagicMock pack whose ``.rules`` is a real swn-bound RulesConfig and
    whose genre-tier ``psionic_discipline_catalog`` ships the disciplines."""
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig, SwnConfig

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="swn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        swn=SwnConfig(attribute_map=dict(_ATTRIBUTE_MAP)),
    )
    pack.worlds = {}
    pack.witnessed_acts = None
    pack.psionic_discipline_catalog = _discipline_catalog()
    return pack


def _make_wwn_pack_with_strain() -> Any:
    """A wwn-bound pack (heavy_metal shape) carrying the psionic catalog — used
    for the AC3 strain-unity push (WWN has both psionics and a strain seam)."""
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig, SystemStrainConfig, WwnConfig

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        wwn=WwnConfig(
            attribute_map=dict(_ATTRIBUTE_MAP),
            system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        ),
    )
    pack.worlds = {}
    pack.witnessed_acts = None
    pack.wwn_spell_catalog = None
    pack.psionic_discipline_catalog = _discipline_catalog()
    return pack


def _make_native_pack() -> Any:
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig()
    pack.worlds = {}
    pack.witnessed_acts = None
    pack.psionic_discipline_catalog = None
    return pack


def _psychic_core(
    *,
    max_effort: int = 3,
    committed: int = 0,
    with_strain: bool = False,
) -> CreatureCore:
    commitments = [EffortCommitment(points=committed, duration="scene")] if committed else []
    core = CreatureCore(
        name=_PSYCHIC,
        description="A precognitive of the Aureate Span.",
        personality="watchful",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        effort={
            _PSIONIC_SOURCE: EffortPool(
                source=_PSIONIC_SOURCE, max=max_effort, commitments=commitments
            )
        },
    )
    if with_strain:
        core.system_strain = SystemStrainPool(current=0, max=10)
    return core


def _snapshot(core: CreatureCore, *, genre: str, world: str) -> GameSnapshot:
    char = Character(
        core=core,
        char_class="Psychic",
        race="Human",
        backstory="Trained on a quiet world.",
        stats=dict(_STATS),
    )
    snap = GameSnapshot(
        genre_slug=genre,
        world_slug=world,
        turn_manager=TurnManager(),
        player_seats={"player:Keith": _PSYCHIC},
    )
    snap.characters.append(char)
    return snap


def _activation_dispatch(
    *, discipline: str = _DISCIPLINE_ID, key: str = "k-psi-1"
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="magic_working",
        params={"actor": _PSYCHIC, "spell": discipline},
        idempotency_key=key,
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _package(*dispatches: SubsystemDispatch) -> DispatchPackage:
    return DispatchPackage(
        turn_id="turn-1",
        per_player=[
            PlayerDispatch(
                player_id="player:Keith",
                raw_action=f"I reach out with {_DISCIPLINE_DISPLAY}.",
                dispatch=list(dispatches),
                narrator_instructions=[],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )


async def _run_bank(package: DispatchPackage, *, snapshot: GameSnapshot, pack: Any):
    from sidequest.agents.subsystems import run_dispatch_bank

    return await run_dispatch_bank(
        package,
        context={"snapshot": snapshot, "pack": pack, "player_name": _PSYCHIC},
    )


def _spans_named(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _discipline_spans(otel_capture) -> list[Any]:
    """Discipline-activation spans, shape per wwn.spell.cast: name ends in
    '.discipline.activated' and carries the actor + discipline id."""
    return [
        s for s in otel_capture.get_finished_spans() if s.name.endswith(".discipline.activated")
    ]


# ===========================================================================
# AC1 + AC6 — free-text path: magic_working routes a discipline to the spine
# ===========================================================================


@pytest.mark.asyncio
async def test_freeplay_discipline_activation_fires_discipline_and_effort_spans(
    otel_capture,
) -> None:
    """A magic_working dispatch naming a known discipline, on a swn psychic with
    free Effort, engages the activation spine: a discipline-activation span fires
    AND ``swn.effort.commit`` fires, with the pool decremented by the cost."""
    core = _psychic_core(max_effort=3)
    snap = _snapshot(core, genre="space_opera", world="aureate_span")
    pack = _make_swn_pack()

    result = await _run_bank(_package(_activation_dispatch()), snapshot=snap, pack=pack)

    disc = _discipline_spans(otel_capture)
    assert len(disc) == 1, (
        f"a named discipline activation must reach the spine and emit one "
        f"discipline-activation span; got {[s.name for s in otel_capture.get_finished_spans()]} "
        f"(bank.errors={getattr(result, 'errors', None)})"
    )
    assert disc[0].attributes["actor"] == _PSYCHIC
    assert disc[0].attributes["discipline_id"] == _DISCIPLINE_ID

    commit = _spans_named(otel_capture, "swn.effort.commit")
    assert len(commit) == 1, "activation must commit Effort via swn.effort.commit"
    assert core.effort[_PSIONIC_SOURCE].available == 2, "1 Effort committed: 3 -> 2"


@pytest.mark.asyncio
async def test_discipline_activation_with_no_free_effort_is_loud(otel_capture) -> None:
    """0 free Effort → the activation must NOT silently succeed. Loud evidence:
    a refused discipline span OR a dispatch_engagement.magic_working.mismatch."""
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher

    core = _psychic_core(max_effort=2, committed=2)  # 0 available
    snap = _snapshot(core, genre="space_opera", world="aureate_span")
    pack = _make_swn_pack()
    package = _package(_activation_dispatch())

    await _run_bank(package, snapshot=snap, pack=pack)

    # No effort may have been silently committed beyond the cap.
    assert core.effort[_PSIONIC_SOURCE].available == 0
    successful = [
        s for s in _discipline_spans(otel_capture) if s.attributes.get("refused") is False
    ]
    assert successful == [], "no-free-Effort activation must not resolve as a success"

    run_dispatch_engagement_watcher(package=package, snapshot=snap)
    refusals = [s for s in _discipline_spans(otel_capture) if s.attributes.get("refused") is True]
    mismatches = _spans_named(otel_capture, "dispatch_engagement.magic_working.mismatch")
    assert refusals or mismatches, (
        "0-Effort activation must surface LOUD evidence — a refused discipline "
        "span or a magic_working mismatch; got neither (silent Illusionism)"
    )


@pytest.mark.asyncio
async def test_native_genre_discipline_dispatch_emits_no_swn_spans(otel_capture) -> None:
    """Slug honesty: the same dispatch in a native-ruleset genre does not crash
    and emits ZERO swn.* / discipline spans."""
    core = CreatureCore(
        name=_PSYCHIC,
        description="No psychic surface here.",
        personality="plain",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
    )
    snap = _snapshot(core, genre="tea_and_murder", world="glenross")
    pack = _make_native_pack()

    await _run_bank(_package(_activation_dispatch()), snapshot=snap, pack=pack)

    swn_spans = [s for s in otel_capture.get_finished_spans() if s.name.startswith("swn.")]
    assert swn_spans == [], (
        f"native genre must not borrow swn spans; got {[s.name for s in swn_spans]}"
    )
    assert _discipline_spans(otel_capture) == []


# ===========================================================================
# AC1 — beat path: BeatSelection carries a discipline_id sidecar
# ===========================================================================


def test_beat_selection_reads_discipline_id_sidecar() -> None:
    """The beat path must carry a ``discipline_id`` sidecar, parallel to the
    ``spell_id`` (cast) and ``mutation_id`` (mutation) sidecars — so a
    psionic_activation beat resolves the chosen discipline."""
    from sidequest.agents.orchestrator import BeatSelection

    sel = BeatSelection.from_dict(
        {
            "actor": _PSYCHIC,
            "beat_id": "psionic_activation",
            "discipline_id": _DISCIPLINE_ID,
        }
    )
    assert getattr(sel, "discipline_id", None) == _DISCIPLINE_ID, (
        "BeatSelection must expose a discipline_id sidecar for the beat-path discipline activation"
    )


# ===========================================================================
# AC3 — strain ledger unity: psionic push and lethality share ONE counter
# ===========================================================================


@pytest.mark.asyncio
async def test_wwn_psionic_push_routes_strain_through_shared_counter(otel_capture) -> None:
    """A strain-costing discipline (psychic_assault) activated by a wwn psychic
    raises ``core.system_strain.current`` and emits ``wwn.system_strain.delta``
    with psionic source attribution — the push routes through the SAME counter
    the lethality seam writes, not a forked field."""
    core = _psychic_core(max_effort=3, with_strain=True)
    snap = _snapshot(core, genre="heavy_metal", world="long_foundry")
    pack = _make_wwn_pack_with_strain()
    strain_before = core.system_strain.current

    await _run_bank(
        _package(_activation_dispatch(discipline="psychic_assault", key="k-push")),
        snapshot=snap,
        pack=pack,
    )

    assert core.system_strain.current > strain_before, (
        "a strain-costing discipline must raise the shared core.system_strain counter"
    )
    deltas = _spans_named(otel_capture, "wwn.system_strain.delta")
    assert deltas, "the strain push must emit wwn.system_strain.delta"
    sources = {d.attributes.get("source") for d in deltas}
    assert any(s and "psy" in str(s).lower() for s in sources), (
        f"the strain delta must attribute a psionic source for the GM panel; got {sources}"
    )


def test_psionic_strain_and_lethality_strain_are_one_field_not_forked() -> None:
    """No-fork guard (AC3): there is exactly ONE strain counter on the core.
    A psionic strain write and a lethality strain write land on the same
    ``core.system_strain`` object — a regression here would mean a forked field."""
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.genre.models.rules import SystemStrainConfig, WwnConfig

    module = get_ruleset_module("wwn")
    cfg = WwnConfig(
        attribute_map=dict(_ATTRIBUTE_MAP),
        system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
    )
    core = _psychic_core(max_effort=3, with_strain=True)
    pool_id = id(core.system_strain)

    module.apply_system_strain(
        core=core, kind="temporary", amount=1, source="psionic:psychic_assault", cfg=cfg
    )
    module.apply_system_strain(
        core=core, kind="temporary", amount=1, source="lethality:mortal_injury", cfg=cfg
    )

    assert core.system_strain.current == 2, "both sources accumulate on one counter"
    assert id(core.system_strain) == pool_id, "the strain pool object must not be replaced/forked"
