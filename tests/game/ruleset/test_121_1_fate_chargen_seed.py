"""RED (ADR-144 F4a / story 121-1): Fate chargen-seeding spine + pack-content schema.

F4a is the engine PREREQUISITE that lets a pack bind ``ruleset: fate`` and produce a
PC with a populated FateSheet. It is NOT content and NOT the interactive chargen flow
(player-authored aspects / skill-pyramid allocation / stunt picks = story 121-7 / F4a2).
F4a delivers: the FateConfig schema + the seeding/apply layer + builder wiring + the
``fate.chargen.seeded`` OTEL span.

Today (F1 merged) nothing constructs a FateSheet: ``CreatureCore.fate_sheet`` is declared
but always ``None``; the genre loader has no ``FateConfig``; the chargen seam
(``seed_chargen_resources`` -> ``ChargenResources``) is WN-shaped (effort/spellcasting/
system_strain, no fate_sheet slot); and ``FateRulesetModule`` overrides no chargen hook.
These tests pin the contract that closes that gap.

Patterns mirrored:
- ``test_143_chargen_resources.py``  — seed_chargen_resources unit shape
- ``test_builder_seeds_strain.py``   — synthetic builder + real-pack-shaped core attach
- ``test_fate_spans.py``             — InMemorySpanExporter + injected ``_tracer``
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from sidequest.game.builder import CharacterBuilder
from sidequest.game.chargen_contribution import ChargenResources
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)

# FateConfig is NEW in F4a. The import failing in RED is the first signal Dev must
# satisfy (add the model); once present, the behavioral assertions below take over.
from sidequest.genre.models.rules import FateConfig, RulesConfig

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A trimmed pulp_noir-shaped skill list (the content authors the real one in F4b);
# here it only has to be a non-empty name->ladder map the seed copies verbatim.
NOIR_SKILLS = {"Investigate": 3, "Contacts": 2, "Deceive": 2, "Shoot": 1, "Notice": 1}
HIGH_CONCEPT = "Hard-Boiled Private Eye"
TROUBLE = "Can't Walk Away From a Dame in Trouble"


def fate_config(**overrides: object) -> FateConfig:
    kwargs: dict[str, object] = {
        "skills": dict(NOIR_SKILLS),
        "refresh": 3,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
    }
    kwargs.update(overrides)
    return FateConfig(**kwargs)  # type: ignore[arg-type]


def fate_rules(*, with_d20_stats: bool = True, fate_refresh: int = 3) -> RulesConfig:
    """A minimal ``ruleset: fate`` RulesConfig.

    ``with_d20_stats=True`` provides ability_score_names + standard_array so the
    builder's legacy stat path is satisfied and does NOT confound the fate_sheet
    attach assertion. ``with_d20_stats=False`` is the de-d20 risk pin.
    """
    fate_block = {
        "skills": dict(NOIR_SKILLS),
        "refresh": fate_refresh,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
    }
    data: dict[str, object] = {"ruleset": "fate", "fate": fate_block}
    if with_d20_stats:
        data["stat_generation"] = "standard_array"
        data["standard_array"] = [15, 14, 13, 12, 10, 8]
        data["ability_score_names"] = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
    return RulesConfig.model_validate(data)


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


def _exporter() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


# ---------------------------------------------------------------------------
# AC1 — FateConfig schema + RulesConfig binding
# ---------------------------------------------------------------------------


class TestAC1FateConfigSchema:
    def test_fate_config_carries_skill_list_refresh_and_aspect_templates(self) -> None:
        cfg = fate_config()
        assert cfg.skills == NOIR_SKILLS
        assert cfg.refresh == 3
        assert cfg.default_high_concept == HIGH_CONCEPT
        assert cfg.default_trouble == TROUBLE

    def test_fate_config_forbids_unknown_keys(self) -> None:
        # All ruleset configs use extra="forbid" (No Silent Fallbacks on typos).
        with pytest.raises(ValidationError):
            FateConfig(skills=dict(NOIR_SKILLS), refresh=3, bogus_key=1)  # type: ignore[call-arg]

    def test_ruleset_config_returns_fate_block_for_fate_pack(self) -> None:
        rules = fate_rules()
        cfg = rules.ruleset_config()
        assert isinstance(cfg, FateConfig)
        assert cfg.skills == NOIR_SKILLS

    def test_non_fate_pack_ruleset_config_is_not_fateconfig(self) -> None:
        # Regression: ruleset_config() must not leak a FateConfig for native.
        assert not isinstance(RulesConfig().ruleset_config(), FateConfig)

    def test_fate_pack_without_fate_block_fails_loud(self) -> None:
        # A pack binding ruleset: fate MUST author its fate config — No Silent
        # Fallbacks, mirroring the swn/cwn "attribute_map required" validator.
        with pytest.raises(ValidationError):
            RulesConfig.model_validate({"ruleset": "fate", "fate": None})


# ---------------------------------------------------------------------------
# AC2 — ChargenResources carries the fate facet (additive)
# ---------------------------------------------------------------------------


class TestAC2ChargenResourcesFateSlot:
    def test_chargen_resources_has_fate_sheet_field_defaulting_none(self) -> None:
        res = ChargenResources()
        assert res.fate_sheet is None

    def test_existing_fields_untouched(self) -> None:
        # Additive change must not disturb the WN-shaped fields.
        res = ChargenResources()
        assert res.effort == {}
        assert res.spellcasting is None
        assert res.system_strain is None


# ---------------------------------------------------------------------------
# AC3 — seed_chargen_resources builds a populated FateSheet
# ---------------------------------------------------------------------------


class TestAC3SeedBuildsPopulatedSheet:
    def test_fate_seed_returns_populated_sheet(self) -> None:
        module = get_ruleset_module("fate")
        res = module.seed_chargen_resources(rules=fate_rules(), stats={}, class_def=None)
        sheet = res.fate_sheet
        assert isinstance(sheet, FateSheet)
        assert sheet.skills == NOIR_SKILLS

    def test_fate_seed_places_high_concept_and_trouble_aspects(self) -> None:
        res = get_ruleset_module("fate").seed_chargen_resources(
            rules=fate_rules(), stats={}, class_def=None
        )
        sheet = res.fate_sheet
        assert sheet is not None
        hc = [a for a in sheet.aspects if a.kind == "high_concept"]
        trouble = [a for a in sheet.aspects if a.kind == "trouble"]
        assert len(hc) == 1 and hc[0].text == HIGH_CONCEPT
        assert len(trouble) == 1 and trouble[0].text == TROUBLE

    def test_fate_seed_sets_refresh_and_starting_fate_points(self) -> None:
        res = get_ruleset_module("fate").seed_chargen_resources(
            rules=fate_rules(fate_refresh=2), stats={}, class_def=None
        )
        sheet = res.fate_sheet
        assert sheet is not None
        assert sheet.refresh == 2
        # SRD: a character starts a session with fate points == refresh.
        assert sheet.fate_points == 2

    def test_fate_seed_independent_of_d20_stats_and_class(self) -> None:
        # The seed must not read ability scores or a class def — Fate has neither.
        res = get_ruleset_module("fate").seed_chargen_resources(
            rules=fate_rules(with_d20_stats=False), stats={}, class_def=None
        )
        assert res.fate_sheet is not None
        assert res.fate_sheet.skills == NOIR_SKILLS

    def test_non_fate_modules_return_no_fate_sheet(self) -> None:
        # Regression: broadening ChargenResources must NOT start handing WN/native
        # characters a fate sheet.
        wwn_rules = RulesConfig.model_validate(
            {
                "ruleset": "wwn",
                "stat_generation": "standard_array",
                "standard_array": [14, 12, 11, 10, 9, 7],
                "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
                "wwn": {
                    "attribute_map": {
                        "STRENGTH": "STR",
                        "DEXTERITY": "DEX",
                        "CONSTITUTION": "CON",
                        "INTELLIGENCE": "INT",
                        "WISDOM": "WIS",
                        "CHARISMA": "CHA",
                    }
                },
            }
        )
        assert (
            get_ruleset_module("wwn")
            .seed_chargen_resources(rules=wwn_rules, stats={"CON": 12}, class_def=None)
            .fate_sheet
            is None
        )
        assert (
            get_ruleset_module("native")
            .seed_chargen_resources(rules=RulesConfig(), stats={}, class_def=None)
            .fate_sheet
            is None
        )


# ---------------------------------------------------------------------------
# AC5 — fate.chargen.seeded OTEL span (the GM-panel lie detector)
# ---------------------------------------------------------------------------


class TestAC5ChargenSeededSpan:
    def test_seed_emits_fate_chargen_seeded_span(self) -> None:
        exporter, tracer = _exporter()
        get_ruleset_module("fate").seed_chargen_resources(
            rules=fate_rules(), stats={}, class_def=None, _tracer=tracer
        )
        names = [s.name for s in exporter.get_finished_spans()]
        assert "fate.chargen.seeded" in names

    def test_chargen_seeded_span_carries_counts(self) -> None:
        exporter, tracer = _exporter()
        get_ruleset_module("fate").seed_chargen_resources(
            rules=fate_rules(), stats={}, class_def=None, _tracer=tracer
        )
        span = next(s for s in exporter.get_finished_spans() if s.name == "fate.chargen.seeded")
        # 5 skills seeded, 2 aspects (high concept + trouble), refresh 3.
        assert span.attributes["skill_count"] == len(NOIR_SKILLS)
        assert span.attributes["aspect_count"] == 2
        assert span.attributes["refresh"] == 3

    def test_non_fate_seed_does_not_emit_chargen_seeded_span(self) -> None:
        exporter, tracer = _exporter()
        # native module: even if it accepted a tracer, no fate.chargen.seeded.
        get_ruleset_module("native").seed_chargen_resources(
            rules=RulesConfig(), stats={}, class_def=None
        )
        assert "fate.chargen.seeded" not in [s.name for s in exporter.get_finished_spans()]


# ---------------------------------------------------------------------------
# AC4 + AC6 — builder attaches fate_sheet; wiring through the REAL builder
# ---------------------------------------------------------------------------


def _build_fate_character(*, with_d20_stats: bool = True) -> object:
    scenes = [
        make_scene("origins", choices=[make_choice("The City", description="Neon and rain.")])
    ]
    builder = CharacterBuilder(scenes=scenes, rules=fate_rules(with_d20_stats=with_d20_stats))
    builder.apply_choice(0)
    return builder.build("Sam Quaid")


class TestAC4AndAC6BuilderWiring:
    def test_real_builder_attaches_populated_fate_sheet(self) -> None:
        character = _build_fate_character()
        assert character.core.fate_sheet is not None, (
            "a ruleset: fate pack built through the production CharacterBuilder must "
            "attach the seeded FateSheet onto CreatureCore.fate_sheet"
        )
        assert character.core.fate_sheet.skills == NOIR_SKILLS

    def test_non_fate_character_has_no_fate_sheet(self) -> None:
        # Paired negative: a native pack build leaves fate_sheet None.
        native_rules = RulesConfig(
            stat_generation="standard_array",
            ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            default_class="Fighter",
            default_race="Human",
        )
        scenes = [make_scene("noop", choices=[make_choice("Go")])]
        builder = CharacterBuilder(scenes=scenes, rules=native_rules)
        builder.apply_choice(0)
        character = builder.build("Arven Steel")
        assert character.core.fate_sheet is None

    def test_fate_pack_routes_to_fate_module_not_native(self) -> None:
        # The mechanical-engagement wire: a fate-bound pack resolves to the Fate
        # module, so dispatch routes combat to fate_conflict (isinstance gate),
        # NOT to native beats. Behavior-based — never a source grep.
        module = get_ruleset_module(fate_rules().ruleset)
        assert isinstance(module, FateRulesetModule)
        assert not isinstance(get_ruleset_module("native"), FateRulesetModule)

    def test_fate_pack_builds_without_d20_stats(self) -> None:
        # De-d20 risk pin (SM Assessment): the builder/chargen path must not
        # hard-require d20 ability scores for a fate pack. The PC still gets a
        # populated FateSheet with no ability_score_names authored.
        character = _build_fate_character(with_d20_stats=False)
        assert character.core.fate_sheet is not None
        assert character.core.fate_sheet.skills == NOIR_SKILLS
