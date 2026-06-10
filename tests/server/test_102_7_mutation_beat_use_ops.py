"""Story 102-7 RED — mutation-marked beats route through use_ops on the
narrator apply path (AWN Plan 2 spec §6.3; the cast_spell mirror).

THE GAP: mutant_wasteland's "Use Mutation" combat beat is a bare strike beat —
the narrator narrates "mutation instability" with zero mechanical backing (no
Strain cost, no usage limit, no ``awn.mutation.*`` span). For WN casting the
apply path already routes ``cast_spell`` beats through the cast spine
(``_resolve_wwn_cast_for_beat``, proven by
tests/integration/test_wwn_elemental_harmony_dispatch.py). AWN's marquee
mechanic needs the same spine: a beat carrying the spec §6.3 wiring marker
(``mutation_resolution: true``) plus a ``BeatSelection.mutation_id`` sidecar
(tests/agents/test_beat_selection_mutation_id_102_7.py) must reach
``sidequest.mutation.use_ops.use_mutation``.

CONTRACT PINNED HERE (TEA-defined; the spec hands Dev the wiring shape, these
tests pin the OBSERVABLE behavior):
  * ``BeatDef`` accepts ``mutation_resolution: bool`` (default False).
  * Applying a marked beat whose selection names an owned mutation fires
    ``awn.mutation.used``, pays the Strain cost through the PC's pool, and
    ticks the per-scene usage counter — engagement, not improv.
  * A marked beat with NO ``mutation_id`` is the pre-wiring bug shape: it must
    be LOUD — ``awn.mutation.refused`` with reason ``beat_no_mutation_id`` —
    and change no state (mirror of ``magic.cast_spell_no_spell_id``).
  * Exhausted usage refuses (``limit_exhausted``) and pays no Strain —
    refusal IS engagement (the 102-3 doctrine).
  * An UNMARKED beat never invokes the mutation engine, even if a stray
    ``mutation_id`` rides the selection (misuse guard).

Synthetic pack throughout (P2-4: engine tests never assert real pack content);
the real-pack proof lives in
tests/integration/test_102_7_mutant_wasteland_mutations_live.py.

CLAUDE.md rule coverage: wiring proven by OTEL spans + state deltas, never
source text; every path asserts something the GM panel can see.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.turn import TurnManager
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    SaveVs,
    StigmaTables,
)
from sidequest.mutation.state import (
    CharacterMutationState,
    MutationState,
    UsageCounter,
)

# ---------------------------------------------------------------------------
# Fixture builders (shape mirrors tests/server/test_awn_combat_dispatch.py)
# ---------------------------------------------------------------------------

_ATTRIBUTE_MAP = {
    "STRENGTH": "Brawn",
    "CONSTITUTION": "Toughness",
    "DEXTERITY": "Reflexes",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Instinct",
    "CHARISMA": "Presence",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_STATS = {name: 10 for name in _ABILITY_SCORE_NAMES}

_PC = "Rux"
_OPPONENT = "Raider Scav"
_MUTATION = "exotic/acid_spit"
_STRAIN_COST = 2


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(
            body_part=["a"] * 6,
            nature=["b"] * 6,
            flavor=["c"] * 12,
        ),
        negatives=[
            NegativeMutationDef(
                id="negative/frail", name="Frail", roll_range=(1, 100), effect="frail"
            )
        ],
        positives=[
            PositiveMutationDef(
                id=_MUTATION,
                name="Acid Spit",
                category="exotic",
                effect="spit acid",
                strain_cost=_STRAIN_COST,
                usage="per_scene",
                save=SaveVs(stat="evasion", effect="negates"),
            ),
        ],
    )


def _make_awn_pack() -> Any:
    """Synthetic awn pack: combat ConfrontationDef with one PLAIN strike beat
    and one MUTATION-MARKED beat (spec §6.3 marker), plus a mutation catalog.
    """
    from sidequest.genre.models.rules import (
        AwnConfig,
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        SystemStrainConfig,
    )

    plain_beat = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Open Fire",
            "kind": "strike",
            "base": 2,
            "stat_check": "Reflexes",
            "effect": "Target takes damage this round",
            "narrator_hint": "Scrap-gun roars across the wastes.",
        }
    )
    mutation_beat = BeatDef.model_validate(
        {
            "id": "unleash_mutation",
            "label": "Unleash Mutation",
            "kind": "strike",
            "base": 2,
            "stat_check": "Instinct",
            "effect": "The change surfaces — power with a price",
            "narrator_hint": "Flesh remembers what the wastes wrote into it.",
            # Spec §6.3: the wiring marker that routes this beat through
            # use_ops instead of bare narration.
            "mutation_resolution": True,
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="Wasteland Brawl (fixture)",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[plain_beat, mutation_beat],
        opponent_default_stats={name: 12 for name in _ABILITY_SCORE_NAMES},
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset="awn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        awn=AwnConfig(
            attribute_map=dict(_ATTRIBUTE_MAP),
            system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        ),
    )
    pack.mutations = _catalog()
    pack.inventory = None
    pack.worlds = {}
    pack.witnessed_acts = None
    return pack


def _make_snapshot(*, usage_used: int = 0) -> GameSnapshot:
    pc_core = CreatureCore(
        name=_PC,
        description="Wasteland mutant.",
        personality="watchful",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        armor_class=12,
        system_strain=SystemStrainPool(current=0, max=10),
    )
    pc = Character(
        core=pc_core,
        char_class="Mutant",
        race="Mutant Human",
        backstory="Born under the fallout sky.",
        stats=dict(_STATS),
    )
    opp_core = CreatureCore(
        name=_OPPONENT,
        description="Raider scav.",
        personality="cruel",
        inventory=Inventory(),
        hp={"current": 8, "max": 8, "base_max": 8},
        armor_class=12,
    )
    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
        player_seats={"player:Keith": _PC},
    )
    snap.characters.append(pc)
    snap.npcs.append(Npc(core=opp_core))

    usage = {_MUTATION: UsageCounter(period="per_scene", used=usage_used)} if usage_used else {}
    snap.mutation_state = MutationState(
        characters={
            _PC: CharacterMutationState(
                mp_remaining=0,
                positive_ids=[_MUTATION],
                usage=usage,
            )
        }
    )

    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=_PC, role="combatant", side="player"),
            EncounterActor(name=_OPPONENT, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    return snap


def _apply_beat(
    snap: GameSnapshot,
    pack: Any,
    *,
    beat_id: str,
    mutation_id: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    # Deterministic dice everywhere in the apply path (save rolls, damage).
    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: b)

    result = NarrationTurnResult(
        narration="Rux's jaw unhinges; the wastes answer.",
        beat_selections=[
            BeatSelection(
                actor=_PC,
                beat_id=beat_id,
                target=_OPPONENT,
                mutation_id=mutation_id,
            )
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name=_PC,
        pack=pack,
        from_explicit_action=True,
        room=room_for(snap),
        acting_character_name=_PC,
    )


def _spans_named(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _pc_strain(snap: GameSnapshot) -> int:
    core = snap.find_creature_core(_PC)
    assert core is not None and core.system_strain is not None
    return core.system_strain.current


def _pc_usage_used(snap: GameSnapshot) -> int:
    cs = snap.mutation_state.characters[_PC]
    counter = cs.usage.get(_MUTATION)
    return counter.used if counter is not None else 0


# ===========================================================================
# Happy path — the marked beat reaches use_ops
# ===========================================================================


def test_marked_beat_with_mutation_id_fires_used_span_and_pays_strain(
    otel_capture, monkeypatch
) -> None:
    """AC: applying the mutation-marked beat with a named, owned mutation
    engages ``use_mutation`` — ``awn.mutation.used`` fires, the Strain cost
    lands on the PC's pool, and the per-scene usage counter ticks. Today the
    beat applies as bare momentum narration and ALL THREE stay silent.
    """
    snap = _make_snapshot()
    pack = _make_awn_pack()

    _apply_beat(
        snap,
        pack,
        beat_id="unleash_mutation",
        mutation_id=_MUTATION,
        monkeypatch=monkeypatch,
    )

    used = _spans_named(otel_capture, "awn.mutation.used")
    assert len(used) == 1, (
        "a mutation_resolution beat naming an owned mutation must route "
        "through use_ops and emit exactly one awn.mutation.used span "
        f"(the GM-panel lie detector); got {len(used)} — the beat is still "
        "bare narration"
    )
    assert used[0].attributes["actor"] == _PC
    assert used[0].attributes["mutation_id"] == _MUTATION

    assert _pc_strain(snap) == _STRAIN_COST, (
        f"the mutation's Strain cost must flow through the PC's pool "
        f"(0 -> {_STRAIN_COST}); got {_pc_strain(snap)} — power without a "
        "price is exactly the crunchless improv Sebastien and Jade named"
    )
    assert _pc_usage_used(snap) == 1, "per_scene usage must tick 0 -> 1 through the beat path"


# ===========================================================================
# Loud failure shapes — no silent improv
# ===========================================================================


def test_marked_beat_without_mutation_id_is_loud_and_inert(otel_capture, monkeypatch) -> None:
    """The pre-wiring bug shape: a mutation beat with no mutation named must
    NOT silently apply as plain narration — it emits ``awn.mutation.refused``
    (reason ``beat_no_mutation_id``, the ``magic.cast_spell_no_spell_id``
    mirror) and changes no mutation state.
    """
    snap = _make_snapshot()
    pack = _make_awn_pack()

    _apply_beat(
        snap,
        pack,
        beat_id="unleash_mutation",
        mutation_id=None,
        monkeypatch=monkeypatch,
    )

    refused = _spans_named(otel_capture, "awn.mutation.refused")
    assert len(refused) == 1, (
        "a mutation_resolution beat with no mutation_id must refuse LOUDLY "
        f"(GM-panel evidence), got {len(refused)} refused spans"
    )
    assert refused[0].attributes["reason"] == "beat_no_mutation_id"
    assert refused[0].attributes["actor"] == _PC

    assert _spans_named(otel_capture, "awn.mutation.used") == []
    assert _pc_strain(snap) == 0, "no engagement -> no Strain"
    assert _pc_usage_used(snap) == 0, "no engagement -> no usage tick"


def test_marked_beat_exhausted_usage_refuses_without_strain(otel_capture, monkeypatch) -> None:
    """Refusal IS engagement (102-3 doctrine): a per-scene mutation already
    used this scene refuses with ``limit_exhausted`` and pays NO Strain —
    never a free second use, never silent improv.
    """
    snap = _make_snapshot(usage_used=1)
    pack = _make_awn_pack()

    _apply_beat(
        snap,
        pack,
        beat_id="unleash_mutation",
        mutation_id=_MUTATION,
        monkeypatch=monkeypatch,
    )

    refused = _spans_named(otel_capture, "awn.mutation.refused")
    assert len(refused) == 1, (
        f"exhausted usage must surface as awn.mutation.refused; got {len(refused)}"
    )
    assert refused[0].attributes["reason"].startswith("limit_exhausted")

    assert _spans_named(otel_capture, "awn.mutation.used") == []
    assert _pc_strain(snap) == 0, "a refused use must not pay Strain"
    assert _pc_usage_used(snap) == 1, "the counter must not tick past the limit"


# ===========================================================================
# Misuse guard — unmarked beats never touch the engine
# ===========================================================================


def test_unmarked_beat_ignores_stray_mutation_id(otel_capture, monkeypatch) -> None:
    """A plain strike beat must never invoke the mutation engine, even when a
    stray ``mutation_id`` rides the selection — the marker is the route, not
    the sidecar (regression guard for every non-mutation beat in every pack).
    """
    snap = _make_snapshot()
    pack = _make_awn_pack()

    _apply_beat(snap, pack, beat_id="shoot", mutation_id=_MUTATION, monkeypatch=monkeypatch)

    assert _spans_named(otel_capture, "awn.mutation.used") == []
    assert _pc_strain(snap) == 0
    assert _pc_usage_used(snap) == 0
