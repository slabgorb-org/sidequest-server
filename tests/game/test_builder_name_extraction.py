"""Playtest 2026-06-05 (RW-2): chargen freeform name extraction.

The road_warrior ``the_name`` scene asks a TWO-part freeform question ("What
do they call you? And what do they call the rig?"). Three measured failures:

1. **Verbatim Name** — ``character_name()`` returned the entire freeform
   sentence ("They call me Zeppo. The rig is Duck Soup — ...") as the
   character name, with zero extraction (ADR-016 freeform mode's whole job).
2. **Dead re-prompt** — the scene's ``hook_prompt`` pushes the builder into
   AwaitingFollowup; ``answer_followup`` inserted the player's crisp
   correction ("Road name: Zeppo. Rig name: Duck Soup.") as a WOUND hook and
   advanced — the correction never re-parsed as a name. The re-prompt was a
   dead input.
3. **Rig name never honored** — the rig-name half of the question had no
   landing site at all (the vessel inventory item keeps its template name).

This suite pins the builder-level contract: ``extract_freeform_names`` parses
the observed phrasings, ``character_name()`` returns the extracted name,
``vessel_name()`` exposes the rig half, and a name-scene followup REPLACES
the prior parse (per-field) instead of becoming a wound hook.

The server-level wiring (vessel inventory item rename at confirmation) is
covered by tests/server/test_chargen_name_rig_extraction.py.
"""

from __future__ import annotations

import pytest

from sidequest.game.builder import (
    CharacterBuilder,
    HookType,
    extract_freeform_names,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Fixture helpers (mirrors test_builder_walk.py)
# ---------------------------------------------------------------------------


def make_choice(label: str, **effect_fields: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description="A description.",
        mechanical_effects=MechanicalEffects(**effect_fields),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str,
    *,
    choices: list[CharCreationChoice] | None = None,
    allows_freeform: bool | None = None,
    hook_prompt: str | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title="Scene title",
        narration="Scene narration.",
        choices=choices or [],
        allows_freeform=allows_freeform,
        hook_prompt=hook_prompt,
    )


def simple_rules() -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class="Wheelman",
        default_race="Human",
    )


def name_scene_builder(*, hook_prompt: str | None = None) -> CharacterBuilder:
    """Class scene + terminal name scene (the canonical name-entry shape)."""
    scenes = [
        make_scene("class", choices=[make_choice("Wheelman", class_hint="Wheelman")]),
        make_scene(
            "the_name",
            allows_freeform=True,
            hook_prompt=hook_prompt,
        ),
    ]
    b = CharacterBuilder(scenes=scenes, rules=simple_rules())
    b.apply_choice(0)
    return b


# The exact phrasings from the playtest repro (Zeppo / the_circuit).
_REPRO_SENTENCE = (
    "They call me Zeppo. The rig is Duck Soup — because when she's "
    "running right, everything looks easy."
)
_REPRO_LABELED = "Road name: Zeppo. Rig name: Duck Soup."
_REPRO_TERSE = "Zeppo. The rig: Duck Soup."
_REPRO_COMMA = "Zeppo, Duck Soup"


# ===========================================================================
# extract_freeform_names — pure parsing
# ===========================================================================


class TestExtractFreeformNames:
    @pytest.mark.parametrize(
        ("text", "want_name", "want_vessel"),
        [
            # Observed repro phrasing 1: conversational, both halves.
            (_REPRO_SENTENCE, "Zeppo", "Duck Soup"),
            # Observed repro phrasing 2: labeled, both halves.
            (_REPRO_LABELED, "Zeppo", "Duck Soup"),
            # Observed repro phrasing 3 (2nd reproduction): terse.
            (_REPRO_TERSE, "Zeppo", "Duck Soup"),
            # Observed repro phrasing 4 (followup answer): comma pair.
            (_REPRO_COMMA, "Zeppo", "Duck Soup"),
            # Plain name — the common single-part case must keep working.
            ("Kara", "Kara", None),
            ("Mad Max", "Mad Max", None),
            # Conversational name only.
            ("They call me Blackbird.", "Blackbird", None),
            ("I go by Snake Plissken.", "Snake Plissken", None),
        ],
    )
    def test_extracts_observed_phrasings(
        self, text: str, want_name: str | None, want_vessel: str | None
    ) -> None:
        name, vessel = extract_freeform_names(text)
        assert name == want_name
        assert vessel == want_vessel

    def test_unparseable_prose_returns_none_name(self) -> None:
        """Long prose with no recognizable name pattern → (None, None) so the
        caller can fall back explicitly (no silent garbage extraction)."""
        name, vessel = extract_freeform_names(
            "the road took everything from us and gave back only dust"
        )
        assert name is None
        assert vessel is None

    def test_blank_returns_none(self) -> None:
        assert extract_freeform_names("   ") == (None, None)


# ===========================================================================
# character_name / vessel_name — builder accessors parse the name scene
# ===========================================================================


class TestCharacterNameExtraction:
    def test_full_sentence_yields_extracted_name(self) -> None:
        """The headline bug: the whole sentence must NOT become the Name."""
        b = name_scene_builder()
        b.apply_freeform(_REPRO_SENTENCE)
        assert b.character_name() == "Zeppo"

    def test_plain_name_still_works(self) -> None:
        b = name_scene_builder()
        b.apply_freeform("Kara")
        assert b.character_name() == "Kara"

    def test_unparseable_prose_falls_back_to_verbatim(self) -> None:
        """Extraction failure keeps the legacy verbatim behavior — a wrong
        name beats a blank one, and the followup correction path exists."""
        b = name_scene_builder()
        text = "the road took everything from us and gave back only dust"
        b.apply_freeform(text)
        assert b.character_name() == text

    def test_vessel_name_extracted_from_name_scene(self) -> None:
        b = name_scene_builder()
        b.apply_freeform(_REPRO_SENTENCE)
        assert b.vessel_name() == "Duck Soup"

    def test_vessel_name_none_when_not_given(self) -> None:
        b = name_scene_builder()
        b.apply_freeform("Kara")
        assert b.vessel_name() is None


# ===========================================================================
# answer_followup on the name scene — correction replaces, no wound hook
# ===========================================================================


class TestNameSceneFollowupCorrection:
    def test_followup_correction_replaces_name_parse(self) -> None:
        """The dead-input bug: the re-prompt answer must be re-parsed and
        REPLACE the prior parse, not be dropped (or buried as a hook)."""
        b = name_scene_builder(hook_prompt="Give your rider a road name and your rig a name.")
        b.apply_freeform(_REPRO_SENTENCE)
        assert b.is_awaiting_followup()
        b.answer_followup("Road name: Ghost. Rig name: Pale Horse.")
        assert b.is_confirmation()
        assert b.character_name() == "Ghost"
        assert b.vessel_name() == "Pale Horse"

    def test_followup_correction_merges_per_field(self) -> None:
        """A name-only correction keeps the rig name from the first answer."""
        b = name_scene_builder(hook_prompt="Names?")
        b.apply_freeform(_REPRO_SENTENCE)  # Zeppo + Duck Soup
        b.answer_followup("They call me Ghost.")  # name only
        assert b.character_name() == "Ghost"
        assert b.vessel_name() == "Duck Soup"

    def test_name_scene_followup_does_not_insert_wound_hook(self) -> None:
        """'Zeppo, Duck Soup' is a name correction, not a trauma hook."""
        b = name_scene_builder(hook_prompt="Names?")
        b.apply_freeform(_REPRO_SENTENCE)
        b.answer_followup(_REPRO_COMMA)
        results = b.scene_results()
        wound_texts = [
            h.text for r in results for h in r.hooks_added if h.hook_type == HookType.WOUND
        ]
        assert _REPRO_COMMA not in wound_texts, (
            "name-scene followup answer must not become a WOUND hook"
        )

    def test_non_name_scene_followup_keeps_wound_hook(self) -> None:
        """Regression guard: hook_prompt scenes that are NOT the name scene
        keep the existing followup-as-wound-hook behavior."""
        scenes = [
            make_scene(
                "wound",
                choices=[make_choice("Scar")],
                hook_prompt="Describe it.",
            ),
            make_scene("the_name", allows_freeform=True),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        b.answer_followup("A thin white line across her palm.")
        results = b.scene_results()
        assert results[0].hooks_added[0].hook_type == HookType.WOUND
        assert results[0].hooks_added[0].text == "A thin white line across her palm."


# ===========================================================================
# Terminal DISPLAY scene must not be mistaken for a name-entry scene
# [BAR-1] chargen-confirm-prose — heavy_metal & 8 sibling packs end on a
# `confirmation`/display scene (no choices, allows_freeform: false), NOT a
# name-entry scene (no choices, allows_freeform: true — only road_warrior).
# The "last scene with no choices" heuristic mis-tagged the display scene as
# the name scene, so character_name() returned the PRIOR scene's freeform
# answer and the confirmation prose rendered "you can see your name —
# I walk toward Helium, to find the princess —".
# ===========================================================================


def _confirm_scene(narration: str) -> CharCreationScene:
    """A terminal display/confirmation scene: no choices, allows_freeform off."""
    return CharCreationScene(
        id="confirmation",
        title="The Book Is Closed",
        narration=narration,
        choices=[],
        allows_freeform=False,
        hook_prompt=None,
    )


def _heavy_metal_shape(confirm_narration: str) -> CharacterBuilder:
    """origins/crucible/the_road choice scenes (freeform-allowed) + a terminal
    display confirmation scene — the heavy_metal/barsoom all-freeform shape."""
    scenes = [
        make_scene("origins", choices=[make_choice("O", race_hint="Servant")], allows_freeform=True),
        make_scene("crucible", choices=[make_choice("C", class_hint="Warrior")], allows_freeform=True),
        make_scene("the_road", choices=[make_choice("R", goals="reach_helium")], allows_freeform=True),
        _confirm_scene(confirm_narration),
    ]
    return CharacterBuilder(scenes=scenes, rules=simple_rules())


class TestTerminalDisplaySceneNotNameScene:
    def test_display_scene_is_not_a_name_scene(self) -> None:
        b = _heavy_metal_shape("Confirmation narration.")
        b.apply_freeform("born in the dust, nobody")
        b.apply_freeform("void took my old life on Earth")
        b.apply_freeform("I walk toward Helium, to find the princess")
        # The terminal scene (index 3) is allows_freeform=False → display, not name.
        assert b._is_name_scene(3) is False

    def test_character_name_is_none_so_lobby_name_wins(self) -> None:
        """With no real name scene, character_name() must yield None so callers
        fall back to the lobby name — NOT leak the_road's freeform answer."""
        b = _heavy_metal_shape("Confirmation narration.")
        b.apply_freeform("born in the dust, nobody")
        b.apply_freeform("void took my old life on Earth")
        b.apply_freeform("I walk toward Helium, to find the princess")
        assert b.character_name() is None

    def test_confirmation_prose_fills_name_from_lobby(self) -> None:
        """The headline symptom: the {name} prose slot must render the lobby
        name, not the motivation freeform."""
        narration = "you can see your name — {name} — And so it is that {name} rises."
        b = _heavy_metal_shape(narration)
        b.with_lobby_name("Groucho")
        b.apply_freeform("born in the dust, nobody")
        b.apply_freeform("void took my old life on Earth")
        b.apply_freeform("I walk toward Helium, to find the princess")
        rendered = b.interpolate_scene_narration(narration)
        assert "I walk toward Helium" not in rendered
        assert "you can see your name — Groucho —" in rendered

    def test_real_terminal_name_scene_still_extracts(self) -> None:
        """Regression guard: road_warrior's genuine terminal name scene
        (allows_freeform=True) must still parse the name."""
        b = name_scene_builder()  # class scene + the_name (allows_freeform=True)
        b.apply_freeform(_REPRO_SENTENCE)
        assert b._is_name_scene(1) is True
        assert b.character_name() == "Zeppo"
