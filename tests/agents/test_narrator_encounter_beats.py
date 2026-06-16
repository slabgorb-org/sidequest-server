from __future__ import annotations

from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
)


def _cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", threshold=10),
        opponent_metric=MetricDef(name="momentum", threshold=10),
        beats=[
            BeatDef(id="attack", label="Attack", kind="strike", base=2, stat_check="STR"),
            BeatDef(id="defend", label="Defend", kind="brace", base=1, stat_check="CON"),
        ],
    )


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="Goblin", role="combatant", side="opponent"),
        ],
    )


def test_build_encounter_context_lists_beats_and_actors() -> None:
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(
        reg, encounter=_enc(), cdef=_cdef(), encounter_summary="stub summary"
    )
    composed = reg.compose(narrator.name())
    assert "stub summary" in composed
    # Available beats must appear so the narrator can pick valid ids
    assert "attack" in composed
    assert "defend" in composed
    # Actors must be listed
    assert "Rux" in composed
    assert "Goblin" in composed


def test_build_encounter_context_without_cdef_falls_back_to_generic() -> None:
    """Without encounter+cdef, still injects the generic rules text.

    Covers the first-turn case where encounter just created and def lookup
    will reach the next turn.
    """
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(
        reg,
        encounter=None,
        cdef=None,
        encounter_summary=None,
    )
    composed = reg.compose(narrator.name())
    assert "encounter-rules" in composed


def test_build_encounter_context_backward_compatible_no_kwargs() -> None:
    """The original positional-only call signature still works.

    Three existing narrator tests at tests/agents/test_narrator.py call
    build_encounter_context(registry) with no keyword args.
    """
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(reg)  # must not raise
    composed = reg.compose(narrator.name())
    assert "encounter-rules" in composed


def test_turn_context_has_encounter_field() -> None:
    from sidequest.agents.orchestrator import TurnContext

    ctx = TurnContext()
    assert ctx.encounter is None
    sentinel = object()
    ctx2 = TurnContext(encounter=sentinel)
    assert ctx2.encounter is sentinel
