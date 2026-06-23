"""RED (story 126-24): narrative chargen seeds the Fate pyramid + aspects as
EDITABLE DEFAULTS via a genre-tier translation table with world overrides.

The bug (sq-playtest-pingpong 2026-06-19, Keith in play): the narrative chargen
wizard for pulp_noir/annees_folles collects rich answers (Origin=The Service,
Signature=I Find Things Out, Connection=A Ghost, Drive=Answers) but DISCARDS them
at the Fate steps — ``fate_aspects`` renders empty placeholders and ``fate_pyramid``
is fully blank, so the on-ramp bait-and-switches to a blank Fate sheet. The forensic
save (2026-06-19-annees_folles-cd25d503) shows the seeded path never fired.

These tests mirror ``tests/game/ruleset/test_121_7_fate_interactive_chargen.py``:
synthetic ``FateConfig`` fixtures driven through the REAL ``CharacterBuilder`` (no
live pulp_noir pack — the real-content e2e is the integration test
``tests/integration/test_126_24_annees_folles_chargen_seed.py``), ``InMemorySpanExporter``
+ injected ``_tracer`` for the GM-panel span assertions, and paired-negative guards.

PINNED PUBLIC CONTRACT (Dev implements to these names — see the TEA Assessment; the
ImportError/ValidationError in RED is the first signal Dev must satisfy):

- ``sidequest.genre.models.rules.FateHintSeed`` — a pydantic model carrying one
  narrative hint's seed: ``pyramid: dict[str, int]`` (a COMPLETE legal allocation for
  the pack's chargen_pyramid) + ``aspects: list[str]`` (free-aspect text seeds).
- ``FateConfig.chargen_seed_table: dict[str, FateHintSeed]`` — genre-tier, keyed by
  the narrative-hint VALUE (e.g. ``"Detective"``). Loader-injected/world-merged like
  ``gear_catalog`` (Crunch in the Genre — the skill list is the rulebook, ADR-140).
- ``sidequest.game.ruleset.fate_chargen.resolve_fate_chargen_seed_table(pack, world_slug)
  -> dict[str, FateHintSeed]`` — mirrors ``resolve_fate_gear_catalog``: union genre-tier
  with world-tier ``chargen_seed_table``; world wins per hint key (ADR-121 layered
  per-field resolution). Emits a merge span carrying ``world_override_applied``.
- ``CharacterBuilder.to_scene_message`` for a ``fate_pyramid`` step with NO player
  allocation yet seeds ``payload.fate_current_allocation`` (legal) from the accumulated
  hints via ``cfg.chargen_seed_table``; the ``fate_aspects`` step seeds the free-aspect
  slots' ``value`` (editable default). High Concept / Trouble ``value`` stay EMPTY (the
  no-silent-default invariant — placeholder only).
- span ``fate.chargen.seed_applied`` (registered in ``SPAN_ROUTES``) at present-time,
  attributes: ``hint`` (matched value), ``skill_count``, ``aspect_count``.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.builder import CharacterBuilder

# fate_chargen.resolve_fate_chargen_seed_table is NEW in 126-24. The ImportError in
# RED is the first signal Dev must satisfy.
from sidequest.game.ruleset.fate_chargen import (
    pyramid_violations,
    resolve_fate_chargen_seed_table,
)
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)

# FateHintSeed is NEW in 126-24.
from sidequest.genre.models.rules import FateConfig, FateHintSeed, FateStuntDef, RulesConfig
from sidequest.telemetry import spans as spans_module

# ---------------------------------------------------------------------------
# Fixtures / helpers (pulp_noir-shaped; mirrors test_121_7)
# ---------------------------------------------------------------------------

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
HIGH_CONCEPT = "Disbarred Lawyer Working the Other Side of the Law"
TROUBLE = "I Can't Leave a Loose Thread Alone"
STUNT_NAMES = ["Read the Room", "Know a Guy", "Gun Nut", "Quick on the Draw"]

# The class_hint the crucible choice "I Find Things Out" carries (char_creation.yaml).
DETECTIVE_HINT = "Detective"

# A complete, legal allocation for chargen_pyramid [1,2,3,4] @ apex 4, with the
# Detective signature skill (Investigate) at the apex. This is what the seed table
# entry for "Detective" should produce.
DETECTIVE_PYRAMID = {
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
DETECTIVE_ASPECTS = ["I Find Things Out", "A Ghost in My Past"]


def seed_table_dict() -> dict[str, dict[str, object]]:
    """A genre-tier seed table authored as plain dicts (as it would appear in the
    fate block) — pydantic coerces to ``dict[str, FateHintSeed]``."""
    return {
        DETECTIVE_HINT: {"pyramid": dict(DETECTIVE_PYRAMID), "aspects": list(DETECTIVE_ASPECTS)},
    }


def fate_config(**overrides: object) -> FateConfig:
    kwargs: dict[str, object] = {
        "skills": dict(NOIR_SKILLS),
        "refresh": 3,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
        "stunts": [FateStuntDef(name=n) for n in STUNT_NAMES],
        "chargen_seed_table": seed_table_dict(),
    }
    kwargs.update(overrides)
    return FateConfig(**kwargs)  # type: ignore[arg-type]


def fate_rules(*, with_seed_table: bool = True, **fate_overrides: object) -> RulesConfig:
    """A minimal ``ruleset: fate`` RulesConfig whose ``fate`` block matches
    ``fate_config()``. ``with_seed_table=False`` drops the table for the no-match guard."""
    fate_block: dict[str, object] = {
        "skills": dict(NOIR_SKILLS),
        "refresh": 3,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
        "stunts": [{"name": n} for n in STUNT_NAMES],
    }
    if with_seed_table:
        fate_block["chargen_seed_table"] = seed_table_dict()
    fate_block.update(fate_overrides)
    return RulesConfig.model_validate({"ruleset": "fate", "fate": fate_block})


def crucible_choice(class_hint: str = DETECTIVE_HINT) -> CharCreationChoice:
    """A crucible-style narrative choice carrying a class_hint, as char_creation.yaml
    emits ("I Find Things Out" -> class_hint: Detective)."""
    return CharCreationChoice(
        label="I Find Things Out",
        description="You learn what others would rather keep buried.",
        mechanical_effects=MechanicalEffects(class_hint=class_hint, rpg_role_hint="control"),  # type: ignore[call-arg]
    )


def narrative_scene(choice: CharCreationChoice) -> CharCreationScene:
    return CharCreationScene(
        id="crucible", title="What did the city make of you?", narration="N", choices=[choice]
    )


def fate_step_scene(step: str) -> CharCreationScene:
    return CharCreationScene(
        id=f"fate_{step}",
        title="T",
        narration="N",
        choices=[],
        mechanical_effects=MechanicalEffects(fate_chargen_step=step),  # type: ignore[call-arg]
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture spans emitted via ``Span.open`` with no ``_tracer`` (builder-emitted at
    present-time), by monkeypatching the lazily-resolved ``spans.tracer`` callable.
    Mirrors tests/game/ruleset/test_fate_chargen_outcome_span.py."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(spans_module, "tracer", lambda: provider.get_tracer("test"))
    return exporter


def builder_with_hint(
    *, class_hint: str = DETECTIVE_HINT, with_seed_table: bool = True
) -> CharacterBuilder:
    """A builder parked at the fate_aspects step with ``class_hint`` accumulated:
    [crucible(class_hint) -> aspects -> pyramid -> stunts]; the crucible choice is
    applied so ``accumulated().class_hint`` is set before the fate steps present."""
    scenes = [
        narrative_scene(crucible_choice(class_hint)),
        fate_step_scene("aspects"),
        fate_step_scene("pyramid"),
        fate_step_scene("stunts"),
    ]
    builder = CharacterBuilder(scenes=scenes, rules=fate_rules(with_seed_table=with_seed_table))
    builder.apply_choice(0)  # apply crucible -> accumulate class_hint, park on aspects
    return builder


def _advance_to_pyramid(builder: CharacterBuilder) -> None:
    """Submit the aspects step (player-authored HC/Trouble) so the builder parks on
    the pyramid step, where the allocation seed should present."""
    builder.apply_fate_aspects(
        high_concept="My Own High Concept", trouble="My Own Trouble", free_aspects=[]
    )


# ---------------------------------------------------------------------------
# AC1 / AC3 / AC4 — present-time pyramid seed from narrative hints (legal)
# ---------------------------------------------------------------------------


class TestAC1PyramidSeedFromHints:
    def test_present_pyramid_seeds_nonempty_allocation(self) -> None:
        """Today ``fate_current_allocation`` is the empty ``self._fate_pyramid`` ({});
        the blank-sheet bug. After 126-24 it is seeded from the accumulated hint."""
        builder = builder_with_hint()
        _advance_to_pyramid(builder)
        msg = builder.to_scene_message("p1")
        assert msg.payload.input_type == "fate_skill_pyramid"
        assert msg.payload.fate_current_allocation, (
            "fate_pyramid presented a BLANK allocation — the narrative-hint seed did not fire"
        )

    def test_seeded_allocation_is_legal(self) -> None:
        """AC4: the seeded pyramid passes the single legality authority."""
        builder = builder_with_hint()
        _advance_to_pyramid(builder)
        cfg = builder.rules.ruleset_config()
        assert isinstance(cfg, FateConfig)
        alloc = builder.to_scene_message("p1").payload.fate_current_allocation
        assert pyramid_violations(alloc, cfg) == []

    def test_seeded_allocation_places_signature_skill_at_apex(self) -> None:
        """AC1: a Detective hint seeds Investigate high (at the apex rating)."""
        builder = builder_with_hint()
        _advance_to_pyramid(builder)
        cfg = builder.rules.ruleset_config()
        assert isinstance(cfg, FateConfig)
        alloc = builder.to_scene_message("p1").payload.fate_current_allocation
        assert alloc.get("Investigate") == cfg.chargen_apex_rating

    def test_unmatched_hint_leaves_allocation_blank(self) -> None:
        """No Silent Fallbacks: a hint with no seed-table entry must NOT fabricate a
        pyramid — the player ranks manually rather than be handed a wrong default."""
        builder = builder_with_hint(class_hint="NoSuchVocation")
        _advance_to_pyramid(builder)
        alloc = builder.to_scene_message("p1").payload.fate_current_allocation
        assert alloc == {}


# ---------------------------------------------------------------------------
# AC1 / AC3 / AC5 — present-time aspect seed + the HC/Trouble invariant
# ---------------------------------------------------------------------------


class TestAC1AspectSeedFromHints:
    def test_present_aspects_seeds_free_slot_values(self) -> None:
        """Today free-aspect slots carry value="" (empty placeholders); after 126-24 at
        least one free slot pre-fills an editable value from the hint's aspect seeds."""
        builder = builder_with_hint()
        slots = builder.to_scene_message("p1").payload.fate_aspect_slots
        free = [s for s in slots if s.kind not in ("high_concept", "trouble")]
        seeded = [s for s in free if (s.value or "").strip()]
        assert seeded, "no free-aspect slot was seeded from the narrative hint"
        assert any(s.value in DETECTIVE_ASPECTS for s in seeded)

    def test_high_concept_and_trouble_value_stay_empty(self) -> None:
        """AC5 / no-silent-default: the seed must NOT pre-fill HC or Trouble ``value``
        (which would let a player click Confirm and ship a default). They stay
        placeholder-only so confirm still requires the player to author them."""
        builder = builder_with_hint()
        slots = builder.to_scene_message("p1").payload.fate_aspect_slots
        hc = next(s for s in slots if s.kind == "high_concept")
        trouble = next(s for s in slots if s.kind == "trouble")
        assert (hc.value or "") == ""
        assert (trouble.value or "") == ""

    def test_empty_high_concept_still_rejected_after_seeding(self) -> None:
        """AC5: the no-silent-default invariant holds — an empty HC/Trouble submission is
        rejected even though the step is now seeded. ``apply_fate_aspects`` is the record
        seam; an empty High Concept must fail loud rather than silently keep the seed."""
        builder = builder_with_hint()
        with pytest.raises((ValueError, RuntimeError)):
            builder.apply_fate_aspects(high_concept="  ", trouble="  ", free_aspects=[])


# ---------------------------------------------------------------------------
# AC5 — seed is an EDITABLE DEFAULT: a player allocation overrides the seed
# ---------------------------------------------------------------------------


class TestAC5SeedIsEditableDefault:
    def test_player_pyramid_overrides_the_seed_on_build(self) -> None:
        """The seed never clobbers an explicit player allocation — the override wins."""
        builder = builder_with_hint()
        _advance_to_pyramid(builder)
        # A legal allocation that DIFFERS from the Detective seed (Shoot at apex).
        player_alloc = {
            "Shoot": 4,
            "Fight": 3,
            "Athletics": 3,
            "Notice": 2,
            "Will": 2,
            "Provoke": 2,
            "Investigate": 1,
            "Contacts": 1,
            "Stealth": 1,
            "Drive": 1,
        }
        assert player_alloc != DETECTIVE_PYRAMID
        builder.apply_fate_pyramid(player_alloc)
        builder.apply_fate_stunts([])
        character = builder.build("Sam Quaid")
        sheet = character.core.fate_sheet
        assert sheet is not None
        assert sheet.skills == player_alloc


# ---------------------------------------------------------------------------
# AC6 — OTEL span on seed computation (the GM-panel lie-detector)
# ---------------------------------------------------------------------------


class TestAC6SeedOtelSpan:
    def test_present_pyramid_emits_seed_applied_span(self, captured) -> None:
        builder = builder_with_hint()
        _advance_to_pyramid(builder)
        builder.to_scene_message("p1")
        names = [s.name for s in captured.get_finished_spans()]
        assert "fate.chargen.seed_applied" in names

    def test_seed_applied_span_carries_hint_and_counts(self, captured) -> None:
        builder = builder_with_hint()
        _advance_to_pyramid(builder)
        builder.to_scene_message("p1")
        span = next(
            s for s in captured.get_finished_spans() if s.name == "fate.chargen.seed_applied"
        )
        assert span.attributes["hint"] == DETECTIVE_HINT
        assert span.attributes["skill_count"] == len(DETECTIVE_PYRAMID)
        assert span.attributes["aspect_count"] == len(DETECTIVE_ASPECTS)

    def test_unmatched_hint_emits_no_seed_applied_span(self, captured) -> None:
        """Paired negative (the lie-detector half): no hint match -> no seed span fires,
        so a ``seed_applied`` span on the GM panel always means a real seed was placed."""
        builder = builder_with_hint(class_hint="NoSuchVocation")
        _advance_to_pyramid(builder)
        builder.to_scene_message("p1")
        names = [s.name for s in captured.get_finished_spans()]
        assert "fate.chargen.seed_applied" not in names


# ---------------------------------------------------------------------------
# AC2 — world override replaces the genre default for a hint (per-key, world wins)
# ---------------------------------------------------------------------------


class _FakeFate:
    def __init__(self, table: dict[str, FateHintSeed]) -> None:
        self.chargen_seed_table = table


class _FakeRules:
    def __init__(self, fate: _FakeFate) -> None:
        self.fate = fate


class _FakeWorld:
    def __init__(self, table: dict[str, FateHintSeed]) -> None:
        self.chargen_seed_table = table


class _FakePack:
    """Duck-typed pack mirroring how ``resolve_fate_gear_catalog`` reads pack/world
    (``pack.rules.fate`` + ``pack.worlds.get(slug)`` + ``getattr(world, ...)``)."""

    def __init__(self, genre: dict[str, FateHintSeed], worlds: dict[str, _FakeWorld]) -> None:
        self.rules = _FakeRules(_FakeFate(genre))
        self.worlds = worlds


def _seed(pyramid: dict[str, int], aspects: list[str]) -> FateHintSeed:
    return FateHintSeed(pyramid=dict(pyramid), aspects=list(aspects))  # type: ignore[arg-type]


class TestAC2WorldOverride:
    def test_world_override_replaces_genre_seed_for_a_hint(self) -> None:
        genre = {DETECTIVE_HINT: _seed(DETECTIVE_PYRAMID, DETECTIVE_ASPECTS)}
        world_pyramid = dict(DETECTIVE_PYRAMID)
        world_pyramid["Notice"], world_pyramid["Contacts"] = 3, 3  # same shape, world flavor
        world_seed = _seed(world_pyramid, ["A Name in the Annees Folles"])
        pack = _FakePack(
            genre=genre, worlds={"annees_folles": _FakeWorld({DETECTIVE_HINT: world_seed})}
        )

        resolved = resolve_fate_chargen_seed_table(pack, "annees_folles")

        assert resolved[DETECTIVE_HINT].aspects == ["A Name in the Annees Folles"]
        assert resolved[DETECTIVE_HINT] != genre[DETECTIVE_HINT]

    def test_non_overridden_hint_keeps_the_genre_default(self) -> None:
        genre = {
            DETECTIVE_HINT: _seed(DETECTIVE_PYRAMID, DETECTIVE_ASPECTS),
            "Fixer": _seed(DETECTIVE_PYRAMID, ["I Know People"]),
        }
        pack = _FakePack(
            genre=genre,
            worlds={
                "annees_folles": _FakeWorld({DETECTIVE_HINT: _seed(DETECTIVE_PYRAMID, ["world"])})
            },
        )
        resolved = resolve_fate_chargen_seed_table(pack, "annees_folles")
        assert resolved["Fixer"].aspects == ["I Know People"]  # untouched genre default

    def test_no_world_returns_genre_table_unchanged(self) -> None:
        genre = {DETECTIVE_HINT: _seed(DETECTIVE_PYRAMID, DETECTIVE_ASPECTS)}
        pack = _FakePack(genre=genre, worlds={})
        resolved = resolve_fate_chargen_seed_table(pack, None)
        assert resolved[DETECTIVE_HINT].aspects == DETECTIVE_ASPECTS


# ---------------------------------------------------------------------------
# AC8 — confirmation prose renders the Fate High Concept, not native {race} {class}
# ---------------------------------------------------------------------------


class TestAC8ConfirmationHighConcept:
    def test_high_concept_token_resolves_to_recorded_fate_hc(self) -> None:
        """The char_creation.yaml ``confirmation`` step reads "a {race} {class}…",
        which under Fate renders the category-error mash "a Military I Find Things Out".
        126-24 adds a ``{high_concept}`` token resolving to the recorded Fate HC so the
        confirmation prose can read the sheet, not the native hints. Today
        ``{high_concept}`` is an UNRECOGNIZED placeholder and survives verbatim — RED."""
        hc = "The Hard-Luck Investigator"
        builder = builder_with_hint()
        builder.apply_fate_aspects(high_concept=hc, trouble="A Loose Thread", free_aspects=[])
        rendered = builder.interpolate_scene_narration(
            "They step out, a {high_concept}, into the night."
        )
        assert hc in rendered
        assert "{high_concept}" not in rendered


# ---------------------------------------------------------------------------
# AC9 — pack signature gear compiles onto the sheet via the NARRATIVE-WIZARD path
# ---------------------------------------------------------------------------


class TestAC9NarrativeWizardGear:
    def test_narrative_wizard_build_carries_gear_derived_aspects(self) -> None:
        """The stored save (2026-06-19) showed source_gear=null for every aspect — the
        narrative-wizard path bypasses ``compile_gear_onto_sheet``. After 126-24 the
        pack's signature gear compiles onto the sheet (aspects carry ``source_gear``)."""
        gear_catalog = [
            {
                "id": "noir_pi_license",
                "name": "Investigator's License",
                "grants_aspects": [{"text": "Licensed Private Investigator", "kind": "permission"}],
            },
            {
                "id": "noir_little_black_book",
                "name": "The Little Black Book",
                "grants_aspects": [{"text": "A Contact for Every Occasion", "kind": "character"}],
            },
        ]
        rules = fate_rules(
            gear=["noir_pi_license", "noir_little_black_book"], gear_catalog=gear_catalog
        )
        scenes = [
            narrative_scene(crucible_choice()),
            fate_step_scene("aspects"),
            fate_step_scene("pyramid"),
            fate_step_scene("stunts"),
        ]
        builder = CharacterBuilder(scenes=scenes, rules=rules)
        builder.apply_choice(0)
        builder.apply_fate_aspects(high_concept="HC", trouble="T", free_aspects=[])
        builder.apply_fate_pyramid(dict(DETECTIVE_PYRAMID))
        builder.apply_fate_stunts([])
        character = builder.build("Sam Quaid")
        sheet = character.core.fate_sheet
        assert sheet is not None
        gear_aspects = {a.source_gear for a in sheet.aspects if a.source_gear}
        assert gear_aspects == {"noir_pi_license", "noir_little_black_book"}
        texts = {a.text for a in sheet.aspects}
        assert "Licensed Private Investigator" in texts
