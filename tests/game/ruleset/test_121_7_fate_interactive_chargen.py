"""RED (ADR-144 F4a2 / story 121-7): Interactive Fate chargen engine + validator.

F4a (121-1, done) seeds a DEFAULT FateSheet (the Menu path). F4a2 builds the
INTERACTIVE flow that turns EXPLICIT player choices — archetype -> aspects -> skill
pyramid -> stunts — into a legal sheet, attaches it through the production builder,
and emits the ``fate.chargen.*`` lie-detector spans.

Design: ``docs/superpowers/specs/2026-06-16-fate-interactive-chargen-design.md``
(decisions S/A/T/R: Pyramid skills, High-Concept + Trouble + N free aspects,
seed-then-edit archetypes, standard refresh). §9 makes
``validate_fate_sheet(sheet, cfg) -> list[str]`` the *single* legality authority — the
auto-generated three-validator AC decomposition (context-story-121-7 AC1/AC2/AC3) is
unified into that one function here; the pyramid / aspect / stunt checks are its named
sub-rules (see the TEA deviation log in the session file).

Patterns mirrored from ``tests/game/ruleset/test_121_1_fate_chargen_seed.py``:
- synthetic ``FateConfig`` fixtures driven through the REAL ``CharacterBuilder``
  (no live pulp_noir pack — the content pilot is 121-3/4/5);
- ``InMemorySpanExporter`` + injected ``_tracer`` for the GM-panel span assertions;
- a paired-negative routing/emission guard.

PINNED PUBLIC CONTRACT (Dev implements to these names — see the TEA Assessment):
- ``FateConfig.{chargen_pyramid, chargen_apex_rating, free_aspect_count, free_stunts}``
- ``sidequest.game.ruleset.fate_chargen.{FateChargenChoices, validate_fate_sheet}``
- ``FateRulesetModule.apply_fate_chargen(*, rules, choices, _tracer=None) -> ChargenResources``
- ``CharacterBuilder.record_fate_chargen(choices)`` (+ ``build()`` attaches the
  interactive sheet, falling back to the Menu seed when no choices were recorded)
- ``MechanicalEffects.fate_chargen_step`` (+ the builder emits ``input_type``
  ``fate_aspects`` / ``fate_skill_pyramid`` / ``fate_stunts``)
- spans ``fate.chargen.{archetype_selected, aspects_authored, pyramid_allocated,
  stunts_selected, validated, completed}`` (+ ``SPAN_ROUTES`` entries)
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from sidequest.game.builder import CharacterBuilder
from sidequest.game.fate_sheet import Aspect, FateSheet, Stunt
from sidequest.game.ruleset import get_ruleset_module

# fate_chargen is NEW in F4a2. The ImportError in RED is the first signal Dev must
# satisfy (create the module); once present, the behavioral assertions below take over.
from sidequest.game.ruleset.fate_chargen import FateChargenChoices, validate_fate_sheet
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.rules import FateConfig, FateStuntDef, RulesConfig
from sidequest.telemetry.spans import fate as fate_spans
from sidequest.telemetry.spans._core import SPAN_ROUTES

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A pulp_noir-shaped skill list. 14 skills so a full Fate-Core pyramid (1/2/3/4 =
# 10 placed skills) fits with room to spare; the values are the Menu-mode default
# ratings (the interactive pyramid OVERRIDES them, so they don't drive legality).
NOIR_SKILLS = {
    "Investigate": 3,
    "Contacts": 2,
    "Notice": 2,
    "Deceive": 1,
    "Shoot": 1,
    "Rapport": 1,
    "Will": 1,
    "Stealth": 0,
    "Athletics": 0,
    "Fight": 0,
    "Burglary": 0,
    "Drive": 0,
    "Empathy": 0,
    "Provoke": 0,
}
HIGH_CONCEPT = "Hard-Boiled Private Eye"
TROUBLE = "Can't Walk Away From a Dame in Trouble"
FREE_ASPECTS = ["A Card With No Name On It", "Owes the Wrong People", "Never Carries a Gun"]
STUNT_NAMES = [
    "The Right Word in the Right Ear",
    "Always a Way Out",
    "Gun Nut",
    "Quick on the Draw",
    "Streetwise",
    "Nerves of Steel",
]


def legal_pyramid() -> dict[str, int]:
    """A legal Fate-Core allocation for the default ``chargen_pyramid`` [1,2,3,4]
    at apex rating 4: 1 skill @Great(4), 2 @Good(3), 3 @Fair(2), 4 @Average(1)."""
    return {
        "Investigate": 4,
        "Contacts": 3,
        "Notice": 3,
        "Deceive": 2,
        "Shoot": 2,
        "Rapport": 2,
        "Will": 1,
        "Stealth": 1,
        "Fight": 1,
        "Provoke": 1,
    }


def fate_config(**overrides: object) -> FateConfig:
    kwargs: dict[str, object] = {
        "skills": dict(NOIR_SKILLS),
        "refresh": 3,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
        "stunts": [FateStuntDef(name=n) for n in STUNT_NAMES],
    }
    kwargs.update(overrides)
    return FateConfig(**kwargs)  # type: ignore[arg-type]


def fate_rules(*, with_d20_stats: bool = True) -> RulesConfig:
    """A minimal ``ruleset: fate`` RulesConfig whose ``fate`` block matches
    ``fate_config()``. ``with_d20_stats`` is the de-d20 risk pin: a fate pack
    authors no ability scores, yet the builder must still produce a PC."""
    fate_block: dict[str, object] = {
        "skills": dict(NOIR_SKILLS),
        "refresh": 3,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
        "stunts": [{"name": n} for n in STUNT_NAMES],
    }
    data: dict[str, object] = {"ruleset": "fate", "fate": fate_block}
    if with_d20_stats:
        data["stat_generation"] = "standard_array"
        data["standard_array"] = [15, 14, 13, 12, 10, 8]
        data["ability_score_names"] = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    return RulesConfig.model_validate(data)


def legal_choices(**overrides: object) -> FateChargenChoices:
    kwargs: dict[str, object] = {
        "archetype": "The Fixer",
        "high_concept": HIGH_CONCEPT,
        "trouble": TROUBLE,
        "free_aspects": list(FREE_ASPECTS),
        "pyramid": legal_pyramid(),
        "stunts": [],
    }
    kwargs.update(overrides)
    return FateChargenChoices(**kwargs)  # type: ignore[arg-type]


def legal_sheet(
    *,
    pyramid: dict[str, int] | None = None,
    stunts: list[str] | None = None,
    refresh: int | None = None,
    free_aspects: list[str] | None = None,
    hc: str | None = HIGH_CONCEPT,
    trouble: str | None = TROUBLE,
) -> FateSheet:
    """Construct a FateSheet directly for validator unit tests. By default it is a
    legal sheet; pass overrides to make one rule fail in isolation. ``refresh``
    defaults to the value the invariant requires for the given ``stunts``."""
    stunt_names = stunts if stunts is not None else []
    base, free_stunts = 3, 3  # mirrors fate_config() defaults
    inv_refresh = max(1, base - max(0, len(stunt_names) - free_stunts))
    aspects: list[Aspect] = []
    if hc is not None:
        aspects.append(Aspect(text=hc, kind="high_concept"))
    if trouble is not None:
        aspects.append(Aspect(text=trouble, kind="trouble"))
    fa = free_aspects if free_aspects is not None else list(FREE_ASPECTS)
    aspects += [Aspect(text=a, kind="character") for a in fa]
    r = refresh if refresh is not None else inv_refresh
    return FateSheet(
        skills=pyramid if pyramid is not None else legal_pyramid(),
        aspects=aspects,
        stunts=[Stunt(name=n) for n in stunt_names],
        refresh=r,
        fate_points=r,
    )


def make_choice(label: str, description: str = "desc", **fx: object) -> CharCreationChoice:
    return CharCreationChoice(
        label=label,
        description=description,
        mechanical_effects=MechanicalEffects(**fx),  # type: ignore[arg-type]
    )


def make_scene(
    scene_id: str, *, choices: list[CharCreationChoice] | None = None
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id, title="T", narration="N", choices=choices or [], mechanical_effects=None
    )


def fate_step_scene(step: str) -> CharCreationScene:
    """A scene that declares it is a Fate chargen step (aspects/pyramid/stunts)."""
    return CharCreationScene(
        id=f"fate_{step}",
        title="T",
        narration="N",
        choices=[],
        mechanical_effects=MechanicalEffects(fate_chargen_step=step),  # type: ignore[call-arg]
    )


def _exporter() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _span(exporter: InMemorySpanExporter, name: str) -> object:
    return next(s for s in exporter.get_finished_spans() if s.name == name)


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _build_fate_character(
    *, choices: FateChargenChoices | None = None, with_d20_stats: bool = False
) -> object:
    scenes = [
        make_scene("origins", choices=[make_choice("The City", description="Neon and rain.")])
    ]
    builder = CharacterBuilder(scenes=scenes, rules=fate_rules(with_d20_stats=with_d20_stats))
    if choices is not None:
        builder.record_fate_chargen(choices)  # pinned accumulator seam
    builder.apply_choice(0)
    return builder.build("Sam Quaid")


# ---------------------------------------------------------------------------
# AC1 — FateConfig chargen fields + config-validity
# ---------------------------------------------------------------------------


class TestAC1FateConfigChargenFields:
    def test_chargen_fields_default_to_srd_values(self) -> None:
        cfg = fate_config()
        assert cfg.chargen_pyramid == [1, 2, 3, 4]
        assert cfg.chargen_apex_rating == 4
        assert cfg.free_aspect_count == 3
        assert cfg.free_stunts == 3

    def test_chargen_fields_are_pack_tunable(self) -> None:
        cfg = fate_config(
            chargen_pyramid=[1, 1, 2], chargen_apex_rating=3, free_aspect_count=2, free_stunts=2
        )
        assert cfg.chargen_pyramid == [1, 1, 2]
        assert cfg.chargen_apex_rating == 3
        assert cfg.free_aspect_count == 2
        assert cfg.free_stunts == 2

    def test_chargen_pyramid_must_be_apex_narrowest(self) -> None:
        # §3 config-validity: chargen_pyramid[i] <= chargen_pyramid[i+1] (never
        # wider at the top). [2,1] and [4,3,2,1] are illegal pyramids.
        with pytest.raises(ValidationError):
            FateConfig(skills=dict(NOIR_SKILLS), chargen_pyramid=[2, 1])  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            FateConfig(skills=dict(NOIR_SKILLS), chargen_pyramid=[4, 3, 2, 1])  # type: ignore[arg-type]

    def test_fate_config_still_forbids_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            FateConfig(skills=dict(NOIR_SKILLS), bogus_chargen_key=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# AC2 — validate_fate_sheet accepts a legal sheet (the single authority, §9)
# ---------------------------------------------------------------------------


class TestAC2ValidatorAcceptsLegal:
    def test_legal_sheet_has_no_violations(self) -> None:
        assert validate_fate_sheet(legal_sheet(), fate_config()) == []

    def test_legal_sheet_with_free_stunts_has_no_violations(self) -> None:
        # 3 stunts == free_stunts, so refresh is undebited (3).
        assert validate_fate_sheet(legal_sheet(stunts=STUNT_NAMES[:3]), fate_config()) == []

    def test_refresh_floor_is_legal(self) -> None:
        # 6 stunts -> 3 - max(0, 6-3) = 0 -> floored to 1; the floored sheet is legal.
        sheet = legal_sheet(stunts=STUNT_NAMES[:6])
        assert sheet.refresh == 1  # the helper applies the floored invariant
        assert validate_fate_sheet(sheet, fate_config()) == []


# ---------------------------------------------------------------------------
# AC3 — validate_fate_sheet rejects each illegal shape (paranoid, de-tautologized)
# ---------------------------------------------------------------------------


class TestAC3ValidatorRejections:
    def test_widened_apex_is_rejected(self) -> None:
        bad = legal_pyramid()
        bad["Notice"] = 4  # now TWO skills at Great(4); rung counts no longer [1,2,3,4]
        violations = validate_fate_sheet(legal_sheet(pyramid=bad), fate_config())
        assert violations
        assert any(
            kw in v.lower() for v in violations for kw in ("pyramid", "rung", "rating", "great")
        )

    def test_rating_above_apex_is_rejected(self) -> None:
        bad = legal_pyramid()
        bad["Investigate"] = 5  # no rung holds rating 5 under apex 4
        assert validate_fate_sheet(legal_sheet(pyramid=bad), fate_config())

    def test_skill_not_in_pack_is_rejected(self) -> None:
        bad = legal_pyramid()
        bad.pop("Provoke")
        bad["Telepathy"] = 1  # not a pulp_noir skill; rung counts still [1,2,3,4]
        violations = validate_fate_sheet(legal_sheet(pyramid=bad), fate_config())
        assert any("telepathy" in v.lower() for v in violations)

    def test_missing_high_concept_is_rejected(self) -> None:
        violations = validate_fate_sheet(legal_sheet(hc=None), fate_config())
        assert any("high concept" in v.lower() or "high_concept" in v.lower() for v in violations)

    def test_empty_high_concept_text_is_rejected(self) -> None:
        violations = validate_fate_sheet(legal_sheet(hc="   "), fate_config())
        assert any("high concept" in v.lower() or "high_concept" in v.lower() for v in violations)

    def test_missing_trouble_is_rejected(self) -> None:
        violations = validate_fate_sheet(legal_sheet(trouble=None), fate_config())
        assert any("trouble" in v.lower() for v in violations)

    def test_too_many_free_aspects_is_rejected(self) -> None:
        # free_aspect_count == 3 is the UPPER bound; free aspects are optional at
        # chargen (story 121-8 AC1 / epic 121 "seeded + refined in play"), so FOUR
        # exceeds the cap and is illegal.
        violations = validate_fate_sheet(
            legal_sheet(free_aspects=["One", "Two", "Three", "Four"]), fate_config()
        )
        assert any("aspect" in v.lower() for v in violations)

    def test_fewer_free_aspects_is_legal(self) -> None:
        # Free aspects optional (≤ free_aspect_count): one of three is legal.
        assert validate_fate_sheet(legal_sheet(free_aspects=["Just One"]), fate_config()) == []

    def test_stunt_not_in_catalog_is_rejected(self) -> None:
        violations = validate_fate_sheet(legal_sheet(stunts=["Time Travel"]), fate_config())
        assert any("time travel" in v.lower() or "stunt" in v.lower() for v in violations)

    def test_refresh_invariant_violation_is_rejected(self) -> None:
        # 5 stunts -> correct refresh is 3 - (5-3) = 1; claiming 3 is illegal.
        sheet = legal_sheet(stunts=STUNT_NAMES[:5], refresh=3)
        violations = validate_fate_sheet(sheet, fate_config())
        assert any("refresh" in v.lower() for v in violations)

    def test_refresh_below_floor_is_rejected(self) -> None:
        # 6 stunts floors at 1; a sheet claiming refresh 0 is illegal.
        sheet = legal_sheet(stunts=STUNT_NAMES[:6], refresh=0)
        violations = validate_fate_sheet(sheet, fate_config())
        assert any("refresh" in v.lower() for v in violations)


# ---------------------------------------------------------------------------
# AC4/AC6 — FateRulesetModule.apply_fate_chargen (the interactive apply authority)
# ---------------------------------------------------------------------------


class TestAC4ApplyFateChargen:
    def test_apply_returns_legal_populated_sheet(self) -> None:
        res = get_ruleset_module("fate").apply_fate_chargen(
            rules=fate_rules(), choices=legal_choices()
        )
        sheet = res.fate_sheet
        assert isinstance(sheet, FateSheet)
        assert validate_fate_sheet(sheet, fate_config()) == []
        assert sheet.skills == legal_pyramid()
        assert any(a.kind == "high_concept" and a.text == HIGH_CONCEPT for a in sheet.aspects)
        assert any(a.kind == "trouble" and a.text == TROUBLE for a in sheet.aspects)
        assert sum(1 for a in sheet.aspects if a.kind == "character") == 3

    def test_apply_debits_refresh_per_stunt_over_free(self) -> None:
        # 5 stunts, free_stunts 3 -> refresh 3 - 2 = 1; fate_points start == refresh.
        res = get_ruleset_module("fate").apply_fate_chargen(
            rules=fate_rules(), choices=legal_choices(stunts=STUNT_NAMES[:5])
        )
        assert res.fate_sheet is not None
        assert res.fate_sheet.refresh == 1
        assert res.fate_sheet.fate_points == 1

    def test_apply_rejects_illegal_choices_loud(self) -> None:
        # No Silent Fallbacks: an illegal allocation must fail loud, not be
        # silently corrected/accepted.
        bad = legal_pyramid()
        bad["Notice"] = 4
        with pytest.raises(ValueError):
            get_ruleset_module("fate").apply_fate_chargen(
                rules=fate_rules(), choices=legal_choices(pyramid=bad)
            )

    def test_non_fate_module_has_no_apply_fate_chargen_path(self) -> None:
        # Paired negative: a dial module routes combat to beats; it carries no
        # Fate interactive chargen. Either the method is absent or it refuses.
        dial = get_ruleset_module("dial")
        if hasattr(dial, "apply_fate_chargen"):
            with pytest.raises(Exception):  # noqa: B017 - any loud refusal is acceptable
                dial.apply_fate_chargen(rules=RulesConfig(), choices=legal_choices())


# ---------------------------------------------------------------------------
# AC5 — fate.chargen.* OTEL spans (the GM-panel lie detector)
# ---------------------------------------------------------------------------

INTERACTIVE_SPAN_NAMES = [
    "fate.chargen.archetype_selected",
    "fate.chargen.aspects_authored",
    "fate.chargen.pyramid_allocated",
    "fate.chargen.stunts_selected",
    "fate.chargen.validated",
    "fate.chargen.completed",
]


class TestAC5ChargenSpans:
    def test_apply_emits_all_interactive_spans(self) -> None:
        exporter, tracer = _exporter()
        get_ruleset_module("fate").apply_fate_chargen(
            rules=fate_rules(), choices=legal_choices(stunts=STUNT_NAMES[:2]), _tracer=tracer
        )
        names = _names(exporter)
        for span_name in INTERACTIVE_SPAN_NAMES:
            assert span_name in names, f"missing GM-panel span {span_name}"

    def test_completed_span_carries_sheet_census(self) -> None:
        exporter, tracer = _exporter()
        get_ruleset_module("fate").apply_fate_chargen(
            rules=fate_rules(), choices=legal_choices(stunts=STUNT_NAMES[:2]), _tracer=tracer
        )
        completed = _span(exporter, "fate.chargen.completed")
        assert completed.attributes["skill_count"] == 10  # placed pyramid skills
        assert completed.attributes["aspect_count"] == 5  # HC + Trouble + 3 free
        assert completed.attributes["stunt_count"] == 2
        assert completed.attributes["refresh"] == 3  # 2 stunts <= free 3, undebited

    def test_validated_span_reports_legal_true_on_success(self) -> None:
        exporter, tracer = _exporter()
        get_ruleset_module("fate").apply_fate_chargen(
            rules=fate_rules(), choices=legal_choices(), _tracer=tracer
        )
        assert _span(exporter, "fate.chargen.validated").attributes["legal"] is True

    def test_illegal_apply_emits_validated_false_and_no_completed(self) -> None:
        # The lie detector must see the FAILED validation, and the sheet must NOT
        # be reported complete.
        exporter, tracer = _exporter()
        bad = legal_pyramid()
        bad["Notice"] = 4
        with pytest.raises(ValueError):
            get_ruleset_module("fate").apply_fate_chargen(
                rules=fate_rules(), choices=legal_choices(pyramid=bad), _tracer=tracer
            )
        names = _names(exporter)
        assert "fate.chargen.validated" in names
        assert _span(exporter, "fate.chargen.validated").attributes["legal"] is False
        assert "fate.chargen.completed" not in names

    def test_interactive_spans_are_registered_in_span_routes(self) -> None:
        # Import side effect registers SPAN_ROUTES; the GM panel can only surface a
        # span it can route (design §8: register in SPAN_ROUTES).
        assert fate_spans is not None  # ensure the registration module is imported
        for span_name in INTERACTIVE_SPAN_NAMES:
            assert span_name in SPAN_ROUTES, f"{span_name} not routed for the GM panel"


# ---------------------------------------------------------------------------
# AC6 — wiring through the REAL CharacterBuilder (mandatory; behavior, never grep)
# ---------------------------------------------------------------------------


class TestAC6BuilderWiring:
    def test_real_builder_attaches_interactive_legal_sheet(self) -> None:
        character = _build_fate_character(choices=legal_choices())
        sheet = character.core.fate_sheet
        assert sheet is not None, (
            "a ruleset: fate pack built through the production CharacterBuilder with "
            "recorded interactive choices must attach the validated FateSheet"
        )
        assert validate_fate_sheet(sheet, fate_rules().ruleset_config()) == []
        # The discriminator that proves the INTERACTIVE path ran (not the F4a Menu
        # seed): the sheet carries the player's 10-skill pyramid, not the 14-skill
        # cfg.skills default.
        assert sheet.skills == legal_pyramid()
        assert sheet.skills != NOIR_SKILLS

    def test_builder_without_choices_falls_back_to_menu_seed(self) -> None:
        # No recorded choices => the F4a default seed (Menu mode), unchanged: the
        # cfg.skills verbatim copy, NOT the interactive pyramid.
        character = _build_fate_character(choices=None)
        sheet = character.core.fate_sheet
        assert sheet is not None
        assert sheet.skills == NOIR_SKILLS

    def test_fate_pack_routes_to_fate_module_not_dial(self) -> None:
        from sidequest.game.ruleset.fate import FateRulesetModule

        module = get_ruleset_module(fate_rules().ruleset)
        assert isinstance(module, FateRulesetModule)
        assert not isinstance(get_ruleset_module("dial"), FateRulesetModule)


# ---------------------------------------------------------------------------
# AC7 — race/class resolution: no d20 default surfaces on a Fate sheet (§6)
# ---------------------------------------------------------------------------


class TestAC7NoD20DefaultOnFateSheet:
    def test_fate_character_carries_no_human_fighter_default(self) -> None:
        character = _build_fate_character(choices=legal_choices(), with_d20_stats=False)
        assert character.char_class != "Fighter"
        assert character.race != "Human"
        # §6: empty/None where validators allow, else the High Concept as a
        # display-only label — NEVER the d20 default.
        assert character.char_class in ("", None) or character.char_class == HIGH_CONCEPT
        assert character.race in ("", None) or character.race == HIGH_CONCEPT

    def test_fate_character_has_no_d20_stats(self) -> None:
        # A fate pack authors no ability scores; stats stay {}.
        character = _build_fate_character(choices=legal_choices(), with_d20_stats=False)
        assert character.stats == {}


# ---------------------------------------------------------------------------
# AC8 — paired-negative input_type emission (§7/§8.4): surfaces never co-render
# ---------------------------------------------------------------------------


class TestAC8PairedNegativeInputType:
    @pytest.mark.parametrize(
        ("step", "expected"),
        [
            ("aspects", "fate_aspects"),
            ("pyramid", "fate_skill_pyramid"),
            ("stunts", "fate_stunts"),
        ],
    )
    def test_fate_step_scene_emits_fate_input_type(self, step: str, expected: str) -> None:
        builder = CharacterBuilder(scenes=[fate_step_scene(step)], rules=fate_rules())
        payload = builder.to_scene_message(player_id="p1").payload
        assert payload.input_type == expected
        # A Fate chargen surface never emits a d20 input_type.
        assert payload.input_type not in ("roll_the_bones", "stat_arrange")

    def test_non_fate_scene_emits_no_fate_input_type(self) -> None:
        # The d20 side of the paired negative: a plain (non-fate-step) scene must
        # never emit a fate_* input_type, so the surfaces can never co-render.
        builder = CharacterBuilder(
            scenes=[make_scene("plain", choices=[make_choice("Go")])], rules=fate_rules()
        )
        input_type = builder.to_scene_message(player_id="p1").payload.input_type
        assert input_type is not None
        assert not input_type.startswith("fate_")
