"""Story 126-5: the_story 'Appearance' input routes to Character.appearance,
never polluting background/backstory."""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.builder import (
    CharacterBuilder,
    StoryInput,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    IdentityCapture,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

ABILITY_NAMES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]


def _fresh_otel() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Fresh, isolated OTEL provider + exporter pair (mirrors
    tests/game/test_chargen_otel_class_events.py). Span context is
    thread-local, so tracer.start_as_current_span() makes the build()
    span the current span without registering a global provider."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _events_by_name(exporter: InMemorySpanExporter) -> dict[str, list]:
    """Collect all finished-span events grouped by name."""
    result: dict[str, list] = {}
    for span in exporter.get_finished_spans():
        for event in span.events:
            result.setdefault(event.name, []).append(event)
    return result


def _base_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=list(ABILITY_NAMES),
        default_class="Fighter",
        default_race="Human",
    )


def _make_scene(scene_id: str, **kwargs: object) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=scene_id.replace("_", " ").title(),
        narration="...",
        **kwargs,  # type: ignore[arg-type]
    )


def _story_scene() -> CharCreationScene:
    """Synthetic the_story scene with identity_capture (pronouns required)."""
    return _make_scene(
        "the_story",
        choices=[],
        allows_freeform=True,
        mechanical_effects=MechanicalEffects(
            identity_capture=IdentityCapture(pronouns_required=True),
        ),
    )


def _builder_parked_at_story() -> CharacterBuilder:
    """Return a CharacterBuilder parked on the_story, ready for StoryInput."""
    scenes = [
        _make_scene(
            "the_calling",
            choices=[
                CharCreationChoice(
                    label="Fighter",
                    description="Strong of arm.",
                    mechanical_effects=MechanicalEffects(class_hint="Fighter"),
                ),
            ],
        ),
        _story_scene(),
        # the_kit is an auto-advance scene (no choices, no freeform).
        _make_scene("the_kit", choices=[], allows_freeform=False),
    ]
    b = CharacterBuilder(scenes=scenes, rules=_base_rules())
    # Advance past the_calling.
    b.apply_choice(0)
    assert b.current_scene().id == "the_story"
    return b


def test_apply_story_routes_description_to_appearance_not_background():
    """Character.appearance holds the appearance text; background/backstory are clean."""
    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="they/them",
            background="Former ratcatcher, owes the apothecary money.",
            description="Tall, soot-stained, missing a tooth.",
        )
    )
    # Auto-advance the_kit to reach Confirmation.
    b.apply_auto_advance()
    assert b.is_confirmation()

    char = b.build("Mara")

    # The 126-5 fix: description routes to appearance.
    assert char.appearance == "Tall, soot-stained, missing a tooth."

    # The 126-5 bug: appearance must NOT appear in background or backstory.
    assert "soot-stained" not in (char.background or "")
    assert "soot-stained" not in char.backstory

    # The typed background still flows to the background channel unchanged.
    assert "ratcatcher" in (b.accumulated().background or "")


def test_apply_story_empty_description_gives_empty_appearance():
    """When description is blank, Character.appearance defaults to empty string."""
    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="she/her",
            background="Merchant's daughter.",
            description="   ",
        )
    )
    b.apply_auto_advance()
    assert b.is_confirmation()

    char = b.build("Lyra")

    assert char.appearance == ""
    # Background channel should only have the typed background.
    assert "Merchant" in (b.accumulated().background or "")


def test_build_emits_chargen_appearance_captured_otel_event():
    """build() emits chargen.appearance_captured with present=True / length>0
    when a non-empty description was supplied (CLAUDE.md OTEL lie-detector)."""
    provider, exporter = _fresh_otel()
    tracer = provider.get_tracer("test_appearance_otel")

    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="they/them",
            background="Former ratcatcher.",
            description="Tall, soot-stained, missing a tooth.",
        )
    )
    b.apply_auto_advance()
    assert b.is_confirmation()

    with tracer.start_as_current_span("build_span"):
        char = b.build("Mara")
    assert char.appearance == "Tall, soot-stained, missing a tooth."

    events = _events_by_name(exporter)
    assert "chargen.appearance_captured" in events, (
        f"Missing chargen.appearance_captured. Got events: {list(events.keys())}"
    )
    captured = events["chargen.appearance_captured"]
    assert len(captured) == 1
    attrs = captured[0].attributes
    assert attrs["present"] is True
    assert attrs["length"] == len("Tall, soot-stained, missing a tooth.")
    assert attrs["length"] > 0


def test_go_back_drops_appearance_from_scene_result():
    """Appearance rides the SceneResult, so reverting the_story drops it.
    A re-answer with an empty description then rebuilds with empty appearance —
    the reverted text is gone (the whole reason appearance is on SceneResult)."""
    b = _builder_parked_at_story()
    b.apply_response(
        StoryInput(
            pronouns="they/them",
            background="Former ratcatcher.",
            description="Tall, soot-stained, missing a tooth.",
        )
    )
    # Now parked on the_kit. go_back() pops the_kit's auto-advance result, then
    # go_back() again pops the_story result — dropping the appearance carrier.
    assert b.current_scene().id == "the_kit"
    b.go_back()
    assert b.current_scene().id == "the_story"

    # Re-answer the_story with a BLANK description; the reverted appearance is gone.
    b.apply_response(
        StoryInput(
            pronouns="they/them",
            background="Former ratcatcher.",
            description="   ",
        )
    )
    b.apply_auto_advance()
    assert b.is_confirmation()

    char = b.build("Mara")

    # The reverted appearance must NOT survive into the rebuilt Character.
    assert char.appearance == ""
    assert "soot-stained" not in char.appearance
    # And appearance never leaked into background/backstory either.
    assert "soot-stained" not in (char.background or "")
    assert "soot-stained" not in char.backstory
