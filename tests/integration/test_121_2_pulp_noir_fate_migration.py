"""ADR-144 F4b (story 121-2): pulp_noir Fate Core migration — the pilot, now GREEN.

F4a (story 121-1) built the engine spine: ``FateConfig`` schema, the
``seed_chargen_resources`` apply layer, builder ``fate_sheet`` attach, and the
``fate.chargen.seeded`` span — proven end-to-end against a *synthetic* fate pack.
F4a's reviewer explicitly deferred the **real-pack** integration test to F4b:

    "121-2 (F4b) should add a skipif-gated real-pack chargen integration test
     (mirror test_real_neon_character_gets_strain_pool)."

This file is that test. It drives the **real** ``pulp_noir`` content pack through
the production loader + builder and pins story 121-2's acceptance criteria:

- AC1  pack loads and authors a valid ``fate:`` config (``ruleset: fate``)
- AC2  a created PC has a populated ``FateSheet`` (skills + HC/trouble aspects + refresh)
- AC3  a Fate action routes to the Fate engine (``fate_conflict``), not native
- AC4  native d20 config is stripped — pack-side (power_tiers gone, no ability scores)
       AND on the BUILT character (empty ``stats`` attribute pool)
- AC5  the F4a wiring contract holds with pulp_noir as the *real* test pack, proven by
       the ``fate.chargen.seeded`` OTEL span firing against real content

**Status:** pulp_noir was migrated to ``ruleset: fate`` when 121-2 landed (its
``power_tiers.yaml`` was deleted, the ``fate:`` block authored), so these tests are
**GREEN**, not RED. They are the regression net that keeps the pack fate-bound, and the
**hardened template** that F4c–F4e (tea_and_murder / wry_whimsy / spaghetti_western,
stories 121-3/4/5) copy — story 121-9 sharpened the assertions here *before* they
propagate to three more packs. **No engine change is in scope** (if an AC can't be met
without touching the engine, that is an F4a gap — escalate, don't patch from F4b).

Pattern mirrored: ``tests/game/test_builder_seeds_strain.py::test_real_neon_character_gets_strain_pool``
(real-pack load + production CharacterBuilder walk + CreatureCore assertions) and
``tests/game/ruleset/test_121_1_fate_chargen_seed.py`` (the F4a fate_sheet/routing/span contract).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.builder import CharacterBuilder
from sidequest.game.character import Character
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import FateConfig
from tests._helpers.genre_paths import PackNotFound, find_pack_path

PACK = "pulp_noir"


def _has_pulp_noir_content() -> bool:
    try:
        find_pack_path(PACK)
        return True
    except PackNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _has_pulp_noir_content(), reason="pulp_noir pack not on disk"
)


def _load_pack() -> GenrePack:
    """Load the real pulp_noir pack through the production loader."""
    from sidequest.genre.loader import load_genre_pack

    try:
        pack_path = find_pack_path(PACK)
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))
    return load_genre_pack(pack_path)


def _fate_cfg(pack: GenrePack) -> FateConfig:
    """Return pulp_noir's authored ``fate:`` block, failing loud if it is absent.

    ``RulesConfig.fate`` is ``FateConfig | None`` (None for every non-fate pack).
    Guarding the access here keeps the assertion sites honest — a regression that
    un-binds pulp_noir from ``ruleset: fate`` (dropping the ``fate:`` block) surfaces
    as a clear failure on this assert, not an ``AttributeError`` on ``None.skills``.
    """
    fate = pack.rules.fate
    assert fate is not None, "pulp_noir must author a fate: block (ruleset: fate)"
    return fate


def _build_pulp_noir_character(name: str = "Sam Spade") -> Character:
    """Walk pulp_noir's full choice-based chargen and build a PC.

    Generic walk (pick choice 0, auto-advance choiceless scenes) mirrors the
    neon real-pack test — robust to the scene changes the Fate migration makes.
    """
    pack = _load_pack()
    builder = (
        CharacterBuilder(
            scenes=list(pack.char_creation),
            rules=pack.rules,
            backstory_tables=pack.backstory_tables,
        )
        .with_lobby_name(name)
        .with_classes(pack.classes)
    )
    # pulp_noir authors no genre-tier equipment_tables override (the field is
    # Optional); attach it only when present so the type stays honest instead of
    # passing None through a non-Optional setter.
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            builder.apply_choice(0)
        else:
            builder.apply_auto_advance()
    return builder.build(name)


def _exporter() -> tuple[InMemorySpanExporter, object]:
    """InMemorySpanExporter + injected tracer, mirroring F4a's test_fate_spans setup."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


# ---------------------------------------------------------------------------
# AC1 — pack loads + authors a valid fate config (ruleset: fate)
# ---------------------------------------------------------------------------


class TestAC1PackLoadsAsFate:
    def test_pulp_noir_binds_fate_ruleset(self) -> None:
        pack = _load_pack()
        assert pack.rules.ruleset == "fate", (
            "pulp_noir must bind ruleset: fate (defaults to 'native' pre-migration)"
        )

    def test_pulp_noir_authors_a_valid_fate_block(self) -> None:
        # load_genre_pack() runs RulesConfig validation; a ruleset: fate pack with
        # a missing/invalid fate block fails loud at load (_validate_fate). Reaching
        # a populated FateConfig here IS the AC1 "sidequest-validate passes" proof
        # for the fate semantics (the `validate pack` CLI only checks dir structure).
        cfg = _load_pack().rules.ruleset_config()
        assert isinstance(cfg, FateConfig), (
            "ruleset_config() must return a FateConfig for a fate-bound pack"
        )
        assert cfg.skills, "pulp_noir fate config must author a non-empty skill list"
        assert cfg.refresh >= 1, "refresh is a positive resource count"
        assert cfg.default_high_concept, "fate config must seed a default high concept"
        assert cfg.default_trouble, "fate config must seed a default trouble"

    def test_pulp_noir_authors_hardboiled_core_skills(self) -> None:
        # The genre's signature skills must be present (story 121-2 skill list).
        cfg = _load_pack().rules.ruleset_config()
        assert isinstance(cfg, FateConfig)
        core = {"Investigate", "Contacts", "Deceive", "Shoot", "Notice", "Rapport"}
        missing = core - set(cfg.skills)
        assert not missing, f"pulp_noir fate skills missing hard-boiled core: {sorted(missing)}"


# ---------------------------------------------------------------------------
# AC2 — a created PC has a populated FateSheet
# ---------------------------------------------------------------------------


class TestAC2CharacterGetsFateSheet:
    def test_real_pulp_noir_character_gets_fate_sheet(self) -> None:
        character = _build_pulp_noir_character()
        assert character.core.fate_sheet is not None, (
            "a ruleset: fate pulp_noir PC built through the production CharacterBuilder "
            "must have a populated FateSheet on CreatureCore.fate_sheet"
        )
        assert isinstance(character.core.fate_sheet, FateSheet)

    def test_fate_sheet_skills_match_pack_config(self) -> None:
        pack = _load_pack()
        character = _build_pulp_noir_character()
        sheet = character.core.fate_sheet
        assert sheet is not None
        # De-tautologized (121-9): pin a concrete authored rating, not just a
        # self-comparison of two reads of the same source. A seed bug that
        # truncates or reorders the ladder would still satisfy a bare
        # ``sheet.skills == pack.fate.skills`` if both sides read the corrupted
        # value; pinning Investigate==4 (pulp_noir's signature peak skill, see
        # rules.yaml fate.skills) catches a dropped or mis-rated entry outright.
        assert sheet.skills["Investigate"] == 4, (
            "pulp_noir's signature peak skill Investigate must seed at rating 4"
        )
        # And the full ladder must round-trip verbatim from the authored config.
        assert sheet.skills == _fate_cfg(pack).skills, (
            "the seeded fate sheet skills must match the pack's authored fate.skills"
        )

    def test_fate_sheet_has_high_concept_and_trouble_aspects(self) -> None:
        character = _build_pulp_noir_character()
        sheet = character.core.fate_sheet
        assert sheet is not None
        kinds = [a.kind for a in sheet.aspects]
        assert kinds.count("high_concept") == 1, "exactly one high-concept aspect seeded"
        assert kinds.count("trouble") == 1, "exactly one trouble aspect seeded"

    def test_fate_sheet_refresh_and_starting_fate_points(self) -> None:
        pack = _load_pack()
        character = _build_pulp_noir_character()
        sheet = character.core.fate_sheet
        assert sheet is not None
        assert sheet.refresh == _fate_cfg(pack).refresh, (
            "fate sheet refresh must match the pack's authored refresh"
        )
        assert sheet.refresh >= 1
        # SRD: a character starts a session with fate points == refresh.
        assert sheet.fate_points == sheet.refresh


# ---------------------------------------------------------------------------
# AC3 — a Fate action routes to the Fate engine (fate_conflict), not native
# ---------------------------------------------------------------------------


class TestAC3RoutesToFateNotNative:
    def test_pulp_noir_resolves_to_fate_module(self) -> None:
        # dispatch_fate_action() gates on isinstance(module, FateRulesetModule)
        # (fate_conflict.py) — so binding ruleset: fate is exactly what routes a
        # combat/social action to the Fate engine instead of the native dial path.
        # Behavior-based wiring proof (NOT a source grep), mirroring F4a's
        # test_fate_pack_routes_to_fate_module_not_native.
        pack = _load_pack()
        module = get_ruleset_module(pack.rules.ruleset)
        assert isinstance(module, FateRulesetModule), (
            "a fate-bound pulp_noir must resolve to FateRulesetModule so dispatch "
            "routes actions to fate_conflict, not the native engine"
        )

    def test_native_module_is_not_the_fate_module(self) -> None:
        # Paired negative: proves the isinstance gate actually discriminates.
        assert not isinstance(get_ruleset_module("native"), FateRulesetModule)


# ---------------------------------------------------------------------------
# AC4 — native d20 config is stripped
# ---------------------------------------------------------------------------


class TestAC4NativeConfigStripped:
    def test_power_tiers_removed(self) -> None:
        # power_tiers.yaml is loaded optionally into pack.power_tiers; a Fate pack
        # has no native power tiers, so the migrated pack must carry an empty map.
        pack = _load_pack()
        assert pack.power_tiers == {}, (
            "native power_tiers config must be stripped from a fate-bound pulp_noir"
        )

    def test_no_d20_ability_scores(self) -> None:
        # The de-d20 invariant (F4a base.py guard): a Fate pack declares no ability
        # scores, so the builder generates no attribute pool. Story 121-2 AC4: the
        # pack loads without d20-shaped attributes.
        pack = _load_pack()
        assert not pack.rules.ability_score_names, (
            "a fate-bound pulp_noir must not declare d20 ability_score_names"
        )

    def test_built_character_has_no_d20_attribute_pool(self) -> None:
        # AC4 hardening (121-9): the de-d20 invariant must hold on the BUILT
        # character, not just the pack config. A pack could declare no
        # ability_score_names yet the builder still synthesize a d20 stat pool
        # from a standard_array default; assert the produced PC carries an empty
        # ``stats`` map (Fate resolves on the skill ladder + fate_sheet, not
        # d20 attributes).
        character = _build_pulp_noir_character()
        assert character.stats == {}, (
            "a fate-bound pulp_noir PC must build with no d20 attribute pool "
            f"(character.stats); got {sorted(character.stats)}"
        )


# ---------------------------------------------------------------------------
# AC5 — the F4a wiring contract holds with pulp_noir as the REAL test pack
# ---------------------------------------------------------------------------


class TestAC5RealPackWiring:
    def test_f4a_wiring_holds_for_real_pulp_noir(self) -> None:
        # The single end-to-end claim F4a proved synthetically and AC5 requires on
        # real content: a fate-bound pack, through the production builder, yields a
        # populated FateSheet AND resolves to the Fate engine (combat → fate_conflict).
        pack = _load_pack()
        character = _build_pulp_noir_character()

        assert character.core.fate_sheet is not None
        assert character.core.fate_sheet.skills, "real pulp_noir PC has seeded skills"
        assert isinstance(get_ruleset_module(pack.rules.ruleset), FateRulesetModule)

    def test_real_pulp_noir_seed_emits_fate_chargen_seeded_span(self) -> None:
        # AC5 hardening (121-9): the F4a OTEL contract — proven synthetically in
        # tests/game/ruleset/test_121_1_fate_chargen_seed.py::TestAC5ChargenSeededSpan —
        # must hold for the REAL pulp_noir fate config. The GM panel is the lie
        # detector (CLAUDE.md OTEL Observability Principle): seeding a fate sheet
        # MUST emit ``fate.chargen.seeded`` so a future silent regression in the
        # real-pack seed path can't pass for "still working". Mirrors F4a's
        # InMemorySpanExporter + injected _tracer, against real content.
        pack = _load_pack()
        cfg = _fate_cfg(pack)
        exporter, tracer = _exporter()

        # The _tracer kwarg lives on FateRulesetModule.seed_chargen_resources (the
        # base ABC signature omits it); narrow the type so the injection is honest.
        module = get_ruleset_module("fate")
        assert isinstance(module, FateRulesetModule)
        module.seed_chargen_resources(
            rules=pack.rules, stats={}, class_def=None, _tracer=tracer
        )

        span = next(
            (s for s in exporter.get_finished_spans() if s.name == "fate.chargen.seeded"),
            None,
        )
        assert span is not None, (
            "seeding a fate sheet from real pulp_noir content must emit the "
            "fate.chargen.seeded OTEL span (GM-panel lie detector)"
        )
        assert span.attributes is not None
        # Span counts must reflect the REAL authored config, not a synthetic stub:
        # every authored skill, exactly the two seeded aspects (HC + trouble), and
        # the authored refresh. Pinned to the pack so a content edit that drops a
        # skill is caught by the span, not just the sheet.
        assert span.attributes["skill_count"] == len(cfg.skills)
        assert span.attributes["aspect_count"] == 2
        assert span.attributes["refresh"] == cfg.refresh
