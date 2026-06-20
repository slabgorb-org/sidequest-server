"""Tests for sidequest.game.builder — Slice B (Story 2.2): protocol rendering.

Covers:
- ``find_unrecognized_tokens`` module helper — exhaustive scan, unclosed `{`
- ``CharacterBuilder.interpolate_scene_narration`` — placeholder substitution,
  lobby-name fallback, OTEL warn events for empty resolution and unrecognized
  tokens
- ``CharacterBuilder.to_scene_message`` — InProgress scene rendering across
  choice / name-entry / display-only disambiguation, rolled-stats gating on
  ``mechanical_effects.stat_generation``, AwaitingFollowup rendering, and
  Confirmation-phase programmer-error guard
"""

from __future__ import annotations

import random

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import ValidationError

from sidequest.game.builder import (
    CharacterBuilder,
    find_unrecognized_tokens,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_choice(
    label: str,
    description: str = "A description.",
    **effect_fields: object,
) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=description,
        mechanical_effects=MechanicalEffects(**effect_fields),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str,
    *,
    choices: list[CharCreationChoice] | None = None,
    allows_freeform: bool | None = None,
    hook_prompt: str | None = None,
    mechanical_effects: MechanicalEffects | None = None,
    narration: str = "Scene narration.",
    title: str = "Scene title",
    loading_text: str | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=title,
        narration=narration,
        choices=choices or [],
        allows_freeform=allows_freeform,
        hook_prompt=hook_prompt,
        mechanical_effects=mechanical_effects,
        loading_text=loading_text,
    )


def simple_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Fighter",
        default_race="Human",
    )


def _fresh_otel() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _capture_events(provider: TracerProvider, fn) -> list:  # type: ignore[no-untyped-def]
    """Run ``fn`` under a fresh span from ``provider`` and return that span's events."""
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("test_harness"):
        fn()
    spans = provider.get_tracer("test")  # no-op to satisfy typechecker
    _ = spans
    # SimpleSpanProcessor exports the span as soon as it ends; reach through
    # the provider's span processors to pull the exporter.
    for processor in provider._active_span_processor._span_processors:  # type: ignore[attr-defined]
        if isinstance(processor, SimpleSpanProcessor):
            inner = processor.span_exporter  # type: ignore[attr-defined]
            if isinstance(inner, InMemorySpanExporter):
                finished = inner.get_finished_spans()
                assert finished, "no span was exported"
                return list(finished[-1].events)
    raise AssertionError("no InMemorySpanExporter found on provider")


# ---------------------------------------------------------------------------
# find_unrecognized_tokens
# ---------------------------------------------------------------------------


class TestFindUnrecognizedTokens:
    def test_empty_string_returns_empty(self) -> None:
        assert find_unrecognized_tokens("") == []

    def test_no_braces_returns_empty(self) -> None:
        assert find_unrecognized_tokens("plain prose, no templating") == []

    def test_all_known_tokens_return_empty(self) -> None:
        assert find_unrecognized_tokens("{name} the {class}, a {race}") == []

    def test_single_typo_returns_token(self) -> None:
        assert find_unrecognized_tokens("Hello {nmae}") == ["{nmae}"]

    def test_unsupported_key_returns_token(self) -> None:
        assert find_unrecognized_tokens("from {origin}") == ["{origin}"]

    def test_multiple_typos_exhaustive(self) -> None:
        out = find_unrecognized_tokens("{nmae} and {clss} and {race}")
        assert out == ["{nmae}", "{clss}"]

    def test_mixed_known_and_unknown(self) -> None:
        out = find_unrecognized_tokens("{name} seeks {mcguffin} for {faction}")
        assert out == ["{mcguffin}", "{faction}"]

    def test_unclosed_brace_returns_rest_of_string(self) -> None:
        out = find_unrecognized_tokens("Hello {name} and {unclosed")
        assert out == ["{unclosed"]

    def test_adjacent_braces_are_separate_tokens(self) -> None:
        out = find_unrecognized_tokens("{a}{b}")
        assert out == ["{a}", "{b}"]


# ---------------------------------------------------------------------------
# interpolate_scene_narration
# ---------------------------------------------------------------------------


class TestInterpolateSceneNarration:
    def test_no_braces_passthrough(self) -> None:
        b = CharacterBuilder(
            scenes=[make_scene("s", narration="Nothing templated.")],
            rules=simple_rules(),
        )
        assert b.interpolate_scene_narration("Nothing templated.") == "Nothing templated."

    def test_name_class_race_substituted_from_accumulated(self) -> None:
        scenes = [
            make_scene(
                "origin",
                choices=[make_choice("Mutant", race_hint="Mutant")],
            ),
            make_scene(
                "class",
                choices=[make_choice("Ranger", class_hint="Ranger")],
            ),
            make_scene("name", allows_freeform=True),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        b.apply_choice(0)
        b.apply_freeform("Thessa")

        rendered = b.interpolate_scene_narration("{name} the {class}, a {race}")
        assert rendered == "Thessa the Ranger, a Mutant"

    def test_indefinite_article_agrees_with_vowel_initial_class(self) -> None:
        # sq-playtest 150-6: a vowel-initial vocation behind a hardcoded "a {class}"
        # rendered "a Episcopal Rector"; the article must agree with the value, incl.
        # across the possessive ("a {class}'s …").
        scenes = [
            make_scene(
                "class",
                choices=[make_choice("Episcopal Rector", class_hint="Episcopal Rector")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        rendered = b.interpolate_scene_narration("the working contents of a {class}'s working life")
        assert rendered == "the working contents of an Episcopal Rector's working life"

    def test_indefinite_article_preserves_case_and_consonant(self) -> None:
        # A consonant value keeps "a"; a sentence-leading article stays capitalized.
        b = CharacterBuilder(
            scenes=[make_scene("class", choices=[make_choice("Ranger", class_hint="Ranger")])],
            rules=simple_rules(),
        )
        b.apply_choice(0)
        assert b.interpolate_scene_narration("A {class} appears.") == "A Ranger appears."
        # A vowel value at sentence start capitalizes the CORRECTED article.
        b2 = CharacterBuilder(
            scenes=[
                make_scene("class", choices=[make_choice("Aristocrat", class_hint="Aristocrat")]),
            ],
            rules=simple_rules(),
        )
        b2.apply_choice(0)
        assert b2.interpolate_scene_narration("A {class} appears.") == "An Aristocrat appears."

    def test_name_falls_back_to_lobby_name(self) -> None:
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules()).with_lobby_name("Rux")
        # No name-entry scene, no freeform input; lobby name must serve.
        assert b.interpolate_scene_narration("Welcome, {name}.") == "Welcome, Rux."

    def test_unset_hint_resolves_empty_without_raising(self) -> None:
        # class and race unset — tokens resolve to "" rather than blowing up.
        b = CharacterBuilder(scenes=[make_scene("s")], rules=simple_rules())
        assert b.interpolate_scene_narration("A {class} of {race}") == "A  of "

    def test_unrecognized_token_preserved_in_output(self) -> None:
        b = CharacterBuilder(scenes=[make_scene("s")], rules=simple_rules())
        rendered = b.interpolate_scene_narration("Hello {mcguffin}.")
        # No recognized token → no substitution pass → {mcguffin} leaks literally.
        assert rendered == "Hello {mcguffin}."

    def test_known_plus_unknown_mix(self) -> None:
        b = CharacterBuilder(
            scenes=[make_scene("origin", choices=[make_choice("Human", race_hint="Human")])],
            rules=simple_rules(),
        ).with_lobby_name("Rux")
        b.apply_choice(0)
        assert (
            b.interpolate_scene_narration("{name} of {race} seeks {mcguffin}")
            == "Rux of Human seeks {mcguffin}"
        )

    def test_emits_interpolated_event_with_info_severity_when_all_resolved(self) -> None:
        scenes = [
            make_scene("origin", choices=[make_choice("Mutant", race_hint="Mutant")]),
            make_scene("class", choices=[make_choice("Ranger", class_hint="Ranger")]),
            make_scene("name", allows_freeform=True),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        b.apply_choice(0)
        b.apply_freeform("Thessa")

        provider, _ = _fresh_otel()
        events = _capture_events(
            provider, lambda: b.interpolate_scene_narration("{name} the {class}, a {race}")
        )

        interpolated = [e for e in events if e.name == "chargen.scene_narration_interpolated"]
        assert len(interpolated) == 1
        attrs = dict(interpolated[0].attributes or {})
        assert attrs["action"] == "scene_narration_interpolated"
        assert attrs["severity"] == "info"
        assert attrs["name_resolved"] is True
        assert attrs["class_resolved"] is True
        assert attrs["race_resolved"] is True

    def test_emits_warn_severity_when_known_token_resolves_empty(self) -> None:
        # class unset — {class} resolves to "" → severity must be warn.
        b = CharacterBuilder(scenes=[make_scene("s")], rules=simple_rules()).with_lobby_name("Rux")
        provider, _ = _fresh_otel()
        events = _capture_events(
            provider, lambda: b.interpolate_scene_narration("{name} the {class}")
        )
        interpolated = [e for e in events if e.name == "chargen.scene_narration_interpolated"]
        assert len(interpolated) == 1
        attrs = dict(interpolated[0].attributes or {})
        assert attrs["severity"] == "warn"
        assert attrs["class_resolved"] is False
        assert attrs["name_resolved"] is True

    def test_emits_one_unrecognized_event_per_token(self) -> None:
        b = CharacterBuilder(
            scenes=[make_scene("s", choices=[make_choice("Human", race_hint="Human")])],
            rules=simple_rules(),
        ).with_lobby_name("Rux")
        b.apply_choice(0)

        provider, _ = _fresh_otel()
        events = _capture_events(
            provider,
            lambda: b.interpolate_scene_narration(
                "{name} of {race} seeks {mcguffin} and {faction}"
            ),
        )
        unrecognized = [
            e for e in events if e.name == "chargen.scene_narration_unrecognized_placeholder"
        ]
        tokens = sorted(dict(e.attributes or {}).get("token", "") for e in unrecognized)
        assert tokens == ["{faction}", "{mcguffin}"]
        for e in unrecognized:
            attrs = dict(e.attributes or {})
            assert attrs["severity"] == "warn"

    def test_no_events_on_plain_text(self) -> None:
        b = CharacterBuilder(scenes=[make_scene("s")], rules=simple_rules())
        provider, _ = _fresh_otel()
        events = _capture_events(provider, lambda: b.interpolate_scene_narration("Nothing to see."))
        chargen_events = [e for e in events if e.name.startswith("chargen.")]
        assert chargen_events == []


# ---------------------------------------------------------------------------
# to_scene_message — InProgress dispatch
# ---------------------------------------------------------------------------


class TestToSceneMessageInProgress:
    def test_choice_scene_emits_full_payload(self) -> None:
        scenes = [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "Mutant", description="A wanderer born in the ash.", race_hint="Mutant"
                    ),
                    make_choice(
                        "Human", description="A survivor from before the fall.", race_hint="Human"
                    ),
                ],
                narration="Choose your origin.",
                loading_text="Weaving your past...",
            ),
            make_scene("name", allows_freeform=True),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        msg = b.to_scene_message("player-xyz")

        assert msg.type == "CHARACTER_CREATION"
        assert msg.player_id == "player-xyz"
        p = msg.payload
        assert p.phase == "scene"
        assert p.scene_index == 0
        assert p.total_scenes == 2
        assert p.prompt == "Choose your origin."
        assert p.input_type == "choice"
        assert p.allows_freeform is None  # scene.allows_freeform default (None) passes through
        assert p.loading_text == "Weaving your past..."
        assert p.choices is not None and len(p.choices) == 2
        assert str(p.choices[0].label) == "Mutant"
        assert str(p.choices[0].description) == "A wanderer born in the ash."
        assert p.rolled_stats is None
        assert p.summary is None
        assert p.message is None
        assert p.character is None
        assert p.character_preview is None
        assert p.action is None

    def test_name_entry_scene_sets_name_input_type(self) -> None:
        scenes = [make_scene("name", allows_freeform=True, narration="What is your name?")]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        p = b.to_scene_message("player-1").payload
        assert p.input_type == "name"
        assert p.allows_freeform is True
        assert p.choices == []
        assert p.prompt == "What is your name?"

    def test_display_only_scene_sets_continue_input_type(self) -> None:
        scenes = [make_scene("intro", narration="The caverns yawn before you.")]
        # No choices, allows_freeform not set → display-only "continue" scene.
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        p = b.to_scene_message("player-1").payload
        assert p.input_type == "continue"
        assert p.allows_freeform is False
        assert p.choices == []

    def test_choice_scene_preserves_scene_allows_freeform(self) -> None:
        # A choice scene can also allow freeform alternative input.
        scenes = [
            make_scene(
                "origin",
                choices=[make_choice("Human", race_hint="Human")],
                allows_freeform=True,
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        p = b.to_scene_message("player-1").payload
        assert p.input_type == "choice"
        assert p.allows_freeform is True

    def test_narration_is_interpolated(self) -> None:
        scenes = [
            make_scene(
                "origin",
                choices=[make_choice("Mutant", race_hint="Mutant")],
            ),
            make_scene(
                "greeting",
                choices=[make_choice("continue_label", description="Continue.")],
                narration="Welcome, {race} wanderer.",
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules()).with_lobby_name("Rux")
        b.apply_choice(0)
        p = b.to_scene_message("player-1").payload
        assert p.prompt == "Welcome, Mutant wanderer."

    def test_rolled_stats_populated_when_scene_declares_stat_generation(self) -> None:
        stat_effects = MechanicalEffects(stat_generation="roll_3d6_strict")
        scenes = [make_scene("roll", mechanical_effects=stat_effects, allows_freeform=True)]
        b = CharacterBuilder(
            scenes=scenes,
            rules=simple_rules(),
            rng=random.Random(42),
        )
        p = b.to_scene_message("player-1").payload
        assert p.rolled_stats is not None
        names = [rs.name for rs in p.rolled_stats]
        assert names == ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
        for rs in p.rolled_stats:
            assert 3 <= rs.value <= 18

    def test_rolled_stats_none_when_scene_lacks_stat_generation(self) -> None:
        # Builder has rolled stats from scene 0, but scene 1 does NOT declare
        # stat_generation in its effects — rolled stats stay off the wire until
        # the scene that owns the roll is shown.
        stat_effects = MechanicalEffects(stat_generation="roll_3d6_strict")
        scenes = [
            make_scene("roll", mechanical_effects=stat_effects, allows_freeform=True),
            make_scene(
                "origin",
                choices=[make_choice("Human", race_hint="Human")],
                narration="Pick an origin.",
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules(), rng=random.Random(7))
        b.apply_freeform("noise")  # any freeform; scene 0 wasn't a name scene
        p = b.to_scene_message("player-1").payload
        assert p.rolled_stats is None

    def test_blank_choice_label_fails_loud(self) -> None:
        # Blank label on a CharCreationChoice must surface via NonBlankString at
        # render time rather than silently fall back — pack-YAML validation gate.
        # Genre models accept blank strings (permissive); the protocol layer
        # is the strict gate.
        scenes = [make_scene("bad", choices=[make_choice(" ")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        with pytest.raises(ValidationError):
            b.to_scene_message("player-1")


# ---------------------------------------------------------------------------
# to_scene_message — AwaitingFollowup dispatch
# ---------------------------------------------------------------------------


class TestToSceneMessageAwaitingFollowup:
    def test_followup_emits_text_input_with_hook_prompt(self) -> None:
        scenes = [
            make_scene(
                "origin",
                choices=[make_choice("Mutant", race_hint="Mutant")],
                hook_prompt="Tell us about the wound that shaped you.",
            ),
            make_scene("name", allows_freeform=True),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)  # Pushes the builder into AwaitingFollowup

        assert b.is_awaiting_followup()
        p = b.to_scene_message("player-xyz").payload
        assert p.phase == "scene"
        assert p.scene_index is None
        assert p.total_scenes == 2
        assert p.prompt == "Tell us about the wound that shaped you."
        assert p.allows_freeform is True
        assert p.input_type == "text"
        assert p.choices is None
        assert p.rolled_stats is None


# ---------------------------------------------------------------------------
# to_scene_message — Confirmation phase guard
# ---------------------------------------------------------------------------


class TestToSceneMessageConfirmation:
    def test_confirmation_phase_raises_runtime_error(self) -> None:
        scenes = [
            make_scene("origin", choices=[make_choice("Human", race_hint="Human")]),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)

        assert b.is_confirmation()
        with pytest.raises(RuntimeError) as exc_info:
            b.to_scene_message("player-1")
        assert "render_confirmation_summary" in str(exc_info.value)
