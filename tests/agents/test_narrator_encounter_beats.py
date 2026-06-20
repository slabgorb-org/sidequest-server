from __future__ import annotations

from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.game.encounter import (
    ContestState,
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.encounter_tag import EncounterTag
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


def _contest_cdef() -> ConfrontationDef:
    """A Fate Contest (ADR-144). Its beats are DISPLAY-ONLY stubs — id + label,
    kind=None, no dial fields — surfaced on the UI class-Abilities tab; the 4dF
    exchange engine resolves the contest, not native beat_selections
    (spec 2026-06-17 §2, enforced by ConfrontationDef._validate)."""
    return ConfrontationDef(
        type="social_duel",
        label="Contest of Wills",
        category="social",
        resolution_mode="contest",
        player_metric=MetricDef(name="victories", threshold=3),
        beats=[
            BeatDef(id="overcome", label="Overcome"),
            BeatDef(id="create_advantage", label="Create an Advantage"),
        ],
    )


def _contest_enc() -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="social_duel",
        category="social",
        player_metric=EncounterMetric(name="victories", current=0, starting=0, threshold=3),
        opponent_metric=EncounterMetric(name="victories", current=0, starting=0, threshold=3),
        actors=[
            EncounterActor(name="Hamish", role="lead", side="player"),
            EncounterActor(name="Mrs. Thornfield", role="rival", side="opponent"),
        ],
    )
    enc.contest = ContestState(target=3)
    return enc


def test_build_encounter_context_fate_contest_does_not_render_native_beats() -> None:
    """Regression for the #985 follow-on Fate-Contest narrate-crash (playtest
    glenross 150-6, 2026-06-20).

    A Fate Contest's ConfrontationDef carries display-only stub beats with
    kind=None. The native beat menu did ``b.kind.value`` over every beat, raising
    ``AttributeError: 'NoneType' object has no attribute 'value'`` and bricking
    every Contest exchange in play. Per SOUL "Bind the Ruleset, Don't Balance It"
    (ADR-144 REPLACE), the native beat/dial machinery is REMOVED from the Fate
    path — build_encounter_context must NOT offer native beats for a Contest, and
    must NOT crash on the stub beats.
    """
    narrator = NarratorAgent()
    reg = PromptRegistry()
    # Was: AttributeError on the kind=None stub beat.
    narrator.build_encounter_context(
        reg,
        encounter=_contest_enc(),
        cdef=_contest_cdef(),
        encounter_summary="Mrs. Thornfield's composure cracks.",
    )
    composed = reg.compose(narrator.name())
    # The resolved-exchange context still reaches the narrator (valley zone).
    assert "Mrs. Thornfield's composure cracks." in composed
    # Participants are still named so the narrator knows who is in the contest.
    assert "Hamish" in composed
    assert "Mrs. Thornfield" in composed
    # The contest is framed as Fate-resolved, not native-beat-resolved.
    assert "Fate Contest" in composed
    assert "Do NOT emit beat_selections" in composed
    # The native beat-selection menu must NOT be rendered for a Contest (it both
    # crashed and re-armed the parallel dial engine ADR-144 forbids).
    assert "beat_selections.beat_id MUST be one of" not in composed


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


def test_encounter_context_emits_tag_gate_when_tags_present() -> None:
    """Playtest 2026-06-10: scene tags are engine-tracked persistent state the
    engine does not yet spend (EncounterTag v1). The narrator must be told NOT
    to narrate a tag being burned/transferred/consumed, or it desyncs prose
    from the stored tag the player can still spend."""
    narrator = NarratorAgent()
    reg = PromptRegistry()
    enc = _enc()
    enc.tags = [
        EncounterTag(
            text="Positional Advantage",
            created_by="Groucho",
            target="unknown_dark_contact",
            leverage=2,
            fleeting=False,
            created_turn=2,
        )
    ]
    narrator.build_encounter_context(
        reg, encounter=enc, cdef=_cdef(), encounter_summary="stub summary"
    )
    composed = reg.compose(narrator.name())
    # The tag itself is surfaced...
    assert "Positional Advantage" in composed
    # ...and the gate constrains the narrator from fabricating its consumption.
    assert "TAGS_ARE_ENGINE_STATE" in composed
    for forbidden in ("spent", "consumed", "transferred", "burned"):
        assert forbidden in composed


def test_encounter_context_omits_tag_gate_when_no_tags() -> None:
    """No tags → no gate text (keep the prompt lean; nothing to constrain)."""
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(
        reg, encounter=_enc(), cdef=_cdef(), encounter_summary="stub summary"
    )
    composed = reg.compose(narrator.name())
    assert "TAGS_ARE_ENGINE_STATE" not in composed


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
