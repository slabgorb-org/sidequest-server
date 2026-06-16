"""Tests for sidequest.server.dispatch.chargen_summary — Slice C (Story 2.2).

Covers Confirmation-phase summary rendering: name resolution (scene > lobby >
omit), equipment merge (pack starting_equipment + scene item_hints), and the
lie-detector OTEL event that catches silent field drops (the 2026-04-09 Thessa
playtest regression).
"""

from __future__ import annotations

import copy
import random
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.character import (
    CharCreationChoice,
    CharCreationScene,
    MechanicalEffects,
)
from sidequest.genre.models.inventory import CatalogItem, InventoryConfig
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import RulesConfig
from sidequest.server.dispatch.chargen_summary import render_confirmation_summary

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def caverns_pack() -> GenrePack:
    """Load caverns_and_claudes — used as a realistic pack backbone for
    unit-level tests that override ``inventory`` in-place.

    caverns has ``default_class: Delver`` and no class-picker scenes, so it
    exercises the default_class branch of summary rendering cleanly.
    """
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


def _clone_with_inventory(base: GenrePack, inventory: InventoryConfig | None) -> GenrePack:
    """Copy a loaded pack and swap its inventory. Tests mutate the clone
    freely without cross-test contamination at module scope."""
    pack = copy.deepcopy(base)
    pack.inventory = inventory
    return pack


def make_choice(
    label: str, description: str = "A description.", **effect_fields: object
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
    mechanical_effects: MechanicalEffects | None = None,
) -> CharCreationScene:
    return CharCreationScene(
        id=scene_id,
        title=scene_id,
        narration="scene",
        choices=choices or [],
        allows_freeform=allows_freeform,
        mechanical_effects=mechanical_effects,
    )


def simple_rules(default_class: str | None = None, default_race: str | None = None) -> RulesConfig:
    return RulesConfig(
        stat_generation="standard_array",
        ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        point_buy_budget=27,
        default_class=default_class,
        default_race=default_race,
    )


def _fresh_otel() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _capture_events(provider: TracerProvider, fn) -> list:  # type: ignore[no-untyped-def]
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("test_harness"):
        fn()
    for processor in provider._active_span_processor._span_processors:  # type: ignore[attr-defined]
        if isinstance(processor, SimpleSpanProcessor):
            inner = processor.span_exporter  # type: ignore[attr-defined]
            if isinstance(inner, InMemorySpanExporter):
                finished = inner.get_finished_spans()
                assert finished, "no span was exported"
                return list(finished[-1].events)
    raise AssertionError("no InMemorySpanExporter found on provider")


# ---------------------------------------------------------------------------
# Phase guard
# ---------------------------------------------------------------------------


class TestPhaseGuard:
    def test_raises_when_not_in_confirmation(self, caverns_pack: GenrePack) -> None:
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        # Builder is in InProgress, not Confirmation.
        assert b.is_in_progress()
        with pytest.raises(AssertionError):
            render_confirmation_summary(b, caverns_pack, "Rux", "player-1")


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


class TestNameResolution:
    def _builder_with_name_scene(self, name: str) -> CharacterBuilder:
        scenes = [
            make_scene("origin", choices=[make_choice("Human", race_hint="Human")]),
            make_scene("name", allows_freeform=True),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        b.apply_freeform(name)
        return b

    def test_scene_name_wins_over_lobby(self, caverns_pack: GenrePack) -> None:
        b = self._builder_with_name_scene("Thessa")
        msg = render_confirmation_summary(b, caverns_pack, "LobbyRux", "p1")
        assert "Name: Thessa" in msg.payload.summary  # type: ignore[operator]
        assert "LobbyRux" not in msg.payload.summary  # type: ignore[operator]

    def test_lobby_fallback_when_no_name_scene(self, caverns_pack: GenrePack) -> None:
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        msg = render_confirmation_summary(b, caverns_pack, "Rux", "p1")
        assert msg.payload.summary is not None
        assert "Name: Rux" in msg.payload.summary

    def test_name_omitted_when_no_source(self, caverns_pack: GenrePack) -> None:
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        msg = render_confirmation_summary(b, caverns_pack, None, "p1")
        assert msg.payload.summary is not None
        assert "Name:" not in msg.payload.summary

    def test_blank_lobby_name_treated_as_none(self, caverns_pack: GenrePack) -> None:
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        msg = render_confirmation_summary(b, caverns_pack, "   ", "p1")
        assert msg.payload.summary is not None
        assert "Name:" not in msg.payload.summary


# ---------------------------------------------------------------------------
# Race / class / personality
# ---------------------------------------------------------------------------


class TestCoreFields:
    def test_race_and_class_hints_use_builder_labels(self, caverns_pack: GenrePack) -> None:
        rules = RulesConfig(
            stat_generation="standard_array",
            ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            point_buy_budget=27,
            race_label="Species",
            class_label="Path",
        )
        scenes = [
            make_scene("origin", choices=[make_choice("Mutant", race_hint="Mutant")]),
            make_scene("class", choices=[make_choice("Ranger", class_hint="Ranger")]),
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_choice(0)
        b.apply_choice(0)
        msg = render_confirmation_summary(b, caverns_pack, "Rux", "p1")
        summary = msg.payload.summary or ""
        assert "Species: Mutant" in summary
        assert "Path: Ranger" in summary

    def test_default_class_shown_when_class_hint_absent(self, caverns_pack: GenrePack) -> None:
        # Synthetic rules with default_class set — caverns_and_claudes no
        # longer has default_class (chargen-visible-dice 2026-05-09 dropped
        # it because the_calling is mandatory). The fallback path itself
        # is still real code for any pack that opts in.
        rules = simple_rules(default_class="Delver")
        scenes = [make_scene("name", allows_freeform=True)]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_freeform("Rux")
        msg = render_confirmation_summary(b, caverns_pack, None, "p1")
        summary = msg.payload.summary or ""
        assert "Delver" in summary

    def test_personality_and_pronouns_rendered_when_accumulated(
        self, caverns_pack: GenrePack
    ) -> None:
        scenes = [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "Human",
                        race_hint="Human",
                        personality_trait="brooding",
                        pronoun_hint="she/her",
                    )
                ],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, caverns_pack, "Rux", "p1").payload.summary) or ""
        # Personality is humanize_display'd so the surface stays TitleCase
        # alongside Origin/Role/Equipment (playtest 2026-04-30 #casing).
        assert "Personality: Brooding" in summary
        assert "Pronouns: she/her" in summary

    def test_mutation_is_humanized(self, caverns_pack: GenrePack) -> None:
        scenes = [
            make_scene(
                "origin",
                choices=[make_choice("Mutant", race_hint="Mutant", mutation_hint="ash_lung")],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, caverns_pack, "Rux", "p1").payload.summary) or ""
        assert "Mutation: Ash Lung" in summary

    def test_affinity_rig_rendered(self, caverns_pack: GenrePack) -> None:
        scenes = [
            make_scene(
                "path",
                choices=[
                    make_choice(
                        "Fire",
                        affinity_hint="Fire",
                        rig_type_hint="Interceptor",
                        rig_trait="armored",
                    )
                ],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, caverns_pack, "Rux", "p1").payload.summary) or ""
        assert "Affinity: Fire" in summary
        assert "Rig: Interceptor" in summary
        # rig_trait is humanize_display'd to keep TitleCase surface (playtest
        # 2026-04-30 #casing) — "armored" becomes "Armored".
        assert "Rig Trait: Armored" in summary

    def test_kebab_case_personality_humanized(self, caverns_pack: GenrePack) -> None:
        """Playtest 2026-04-30: ``personality_trait: trouble-magnet`` in
        coyote_star YAML rendered as the raw kebab token next to
        TitleCase Origin/Equipment. ``humanize_display`` must split on
        ``-`` and Title-case each token so the surface is consistent."""
        scenes = [
            make_scene(
                "trait",
                choices=[
                    make_choice(
                        "Trouble Magnet",
                        personality_trait="trouble-magnet",
                    )
                ],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (
            render_confirmation_summary(b, caverns_pack, "Parsley", "p1").payload.summary
        ) or ""
        assert "Personality: Trouble Magnet" in summary
        assert "trouble-magnet" not in summary

    def test_kebab_case_background_humanized(self, caverns_pack: GenrePack) -> None:
        """Same playtest: ``background: Outsystem-arrived`` (Pascal-with-
        hyphen) leaked into the Backstory line. humanize_display must
        normalize regardless of input casing."""
        scenes = [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "Through the Gate",
                        background="Outsystem-arrived",
                    )
                ],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (
            render_confirmation_summary(b, caverns_pack, "Parsley", "p1").payload.summary
        ) or ""
        assert "Backstory: Outsystem Arrived" in summary
        assert "Outsystem-arrived" not in summary

    def test_humanize_display_helper_handles_both_separators(self) -> None:
        """Unit-level guard for the helper itself — split on ``-`` and
        ``_``, capitalize each token, drop empties, idempotent on
        already-Title-cased input."""
        from sidequest.server.dispatch.chargen_summary import humanize_display

        assert humanize_display("trouble-magnet") == "Trouble Magnet"
        assert humanize_display("Outsystem-arrived") == "Outsystem Arrived"
        assert humanize_display("ash_lung") == "Ash Lung"
        assert humanize_display("Coreworlder") == "Coreworlder"
        assert humanize_display("quietly grieving") == "Quietly Grieving"
        # Empty/None-equivalent passthrough.
        assert humanize_display("") == ""


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_rolled_stats_line_space_separated(self, caverns_pack: GenrePack) -> None:
        # Two scenes so the name-scene heuristic (last-scene-no-choices-with-
        # freeform) doesn't bind to the roll scene's freeform advance token.
        stat_effects = MechanicalEffects(stat_generation="roll_3d6_strict")
        scenes = [
            make_scene("roll", mechanical_effects=stat_effects, allows_freeform=True),
            make_scene("origin", choices=[make_choice("Human", race_hint="Human")]),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules(), rng=random.Random(42))
        b.apply_freeform("advance")
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, caverns_pack, None, "p1").payload.summary) or ""
        assert "Stats: STR " in summary
        # Stats are rendered double-space-separated.
        for ability in ("DEX", "CON", "INT", "WIS", "CHA"):
            assert f"  {ability} " in summary

    def test_no_stats_line_when_none_rolled(self, caverns_pack: GenrePack) -> None:
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, caverns_pack, "Rux", "p1").payload.summary) or ""
        assert "Stats:" not in summary


# ---------------------------------------------------------------------------
# Equipment resolution
# ---------------------------------------------------------------------------


class TestEquipment:
    def test_pack_starting_equipment_by_class_hint(self, caverns_pack: GenrePack) -> None:
        inv = InventoryConfig(
            starting_equipment={"Ranger": ["short_bow", "hunting_knife"]},
            item_catalog=[
                CatalogItem(id="short_bow", name="Short Bow", description="x", category="weapon"),
                CatalogItem(
                    id="hunting_knife",
                    name="Hunting Knife",
                    description="x",
                    category="weapon",
                ),
            ],
        )
        pack = _clone_with_inventory(caverns_pack, inv)
        scenes = [
            make_scene("class", choices=[make_choice("Ranger", class_hint="Ranger")]),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, pack, "Rux", "p1").payload.summary) or ""
        assert "Equipment: Short Bow, Hunting Knife" in summary

    def test_pack_starting_equipment_case_insensitive_class_lookup(
        self, caverns_pack: GenrePack
    ) -> None:
        # Pack YAML commonly lower-cases class keys; class_hint from chargen
        # is Title Case. The lookup is case-insensitive.
        inv = InventoryConfig(starting_equipment={"ranger": ["short_bow"]})
        pack = _clone_with_inventory(caverns_pack, inv)
        scenes = [make_scene("class", choices=[make_choice("Ranger", class_hint="Ranger")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, pack, "Rux", "p1").payload.summary) or ""
        assert "Equipment: Short Bow" in summary

    def test_falls_back_to_default_class_when_no_class_hint(self, caverns_pack: GenrePack) -> None:
        inv = InventoryConfig(starting_equipment={"Delver": ["rope", "torch"]})
        pack = _clone_with_inventory(caverns_pack, inv)
        scenes = [make_scene("name", allows_freeform=True)]
        # Synthetic rules with default_class set — see
        # test_default_class_shown_when_class_hint_absent for why we no
        # longer use caverns_pack.rules here (the C&C pack dropped
        # default_class with chargen-visible-dice 2026-05-09).
        rules = simple_rules(default_class="Delver")
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_freeform("Rux")
        summary = (render_confirmation_summary(b, pack, None, "p1").payload.summary) or ""
        assert "Equipment: Rope, Torch" in summary

    def test_scene_item_hints_merged_onto_pack_loadout(self, caverns_pack: GenrePack) -> None:
        inv = InventoryConfig(starting_equipment={"Ranger": ["short_bow"]})
        pack = _clone_with_inventory(caverns_pack, inv)
        scenes = [
            make_scene(
                "class",
                choices=[make_choice("Ranger", class_hint="Ranger", item_hint="compass")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, pack, "Rux", "p1").payload.summary) or ""
        # Order: pack items first, scene hints appended (dedup).
        assert "Equipment: Short Bow, Compass" in summary

    def test_scene_item_hints_alone_when_pack_has_no_inventory(self) -> None:
        # A barebones pack with inventory=None — only scene hints contribute.
        rules = simple_rules()
        scenes = [
            make_scene("pick", choices=[make_choice("x", class_hint="Fighter", item_hint="rope")])
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_choice(0)
        # Construct a minimal GenrePack surrogate by loading caverns then
        # stripping inventory.
        content_path = CONTENT_ROOT / "caverns_and_claudes"
        if not content_path.is_dir():
            pytest.skip("content not available")
        pack = _clone_with_inventory(load_genre_pack(content_path), None)
        summary = (render_confirmation_summary(b, pack, "Rux", "p1").payload.summary) or ""
        assert "Equipment: Rope" in summary

    def test_scene_hint_duplicate_not_added_twice(self, caverns_pack: GenrePack) -> None:
        inv = InventoryConfig(starting_equipment={"Ranger": ["short_bow", "compass"]})
        pack = _clone_with_inventory(caverns_pack, inv)
        scenes = [
            make_scene(
                "class",
                choices=[make_choice("Ranger", class_hint="Ranger", item_hint="compass")],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, pack, "Rux", "p1").payload.summary) or ""
        # compass appears exactly once despite being in both sources.
        assert summary.count("Compass") == 1

    def test_no_equipment_line_when_no_sources(self, caverns_pack: GenrePack) -> None:
        pack = _clone_with_inventory(caverns_pack, InventoryConfig())
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, pack, "Rux", "p1").payload.summary) or ""
        assert "Equipment:" not in summary

    def test_item_id_without_catalog_entry_humanized(self, caverns_pack: GenrePack) -> None:
        # No item_catalog entry for the item → fall back to Title-Cased
        # snake_case.
        inv = InventoryConfig(starting_equipment={"Ranger": ["mystery_compass"]})
        pack = _clone_with_inventory(caverns_pack, inv)
        scenes = [make_scene("class", choices=[make_choice("Ranger", class_hint="Ranger")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, pack, "Rux", "p1").payload.summary) or ""
        assert "Equipment: Mystery Compass" in summary


# ---------------------------------------------------------------------------
# Backstory and wire shape
# ---------------------------------------------------------------------------


class TestWireShape:
    def test_backstory_prefixed_with_blank_line(self, caverns_pack: GenrePack) -> None:
        scenes = [
            make_scene(
                "origin",
                choices=[make_choice("Human", race_hint="Human", background="a bleak childhood")],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (render_confirmation_summary(b, caverns_pack, "Rux", "p1").payload.summary) or ""
        # humanize_display Title-cases the value (playtest 2026-04-30 #casing)
        # — single canonicalization rule across the preview row.
        assert "\n\nBackstory: A Bleak Childhood" in summary

    def test_drive_scene_label_overrides_origin_routing_tag(
        self,
        caverns_pack: GenrePack,
    ) -> None:
        """Parsley playtest BUG-LOW (2026-04-30): origin scene set
        ``background: Outsystem-arrived`` as a routing tag; drive scene
        chose "Someone Went Into the Drift" but the preview kept showing
        the origin tag because ``acc.background`` overrode display.

        Fix: when a scene's effects look "drive-shaped" (touches
        relationship/goals/emotional_state, doesn't touch race/class/
        mutation/rig hints), record the choice label as
        ``acc.backstory_label`` and prefer it for display.
        """
        scenes = [
            # Origin scene — sets race + background routing tag.
            make_scene(
                "origins",
                choices=[
                    make_choice(
                        "I Came Through the Gate",
                        race_hint="Coreworlder",
                        background="Outsystem-arrived",
                    ),
                ],
            ),
            # Drive scene — sets the inner-life triplet (relationship/
            # goals/emotional_state) without race/class/mutation. This is
            # the canonical "backstory hook" shape.
            make_scene(
                "drive",
                choices=[
                    make_choice(
                        "Someone Went Into the Drift",
                        relationship="lost_beloved",
                        goals="find_what_was_lost",
                        emotional_state="quietly grieving",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)  # origin
        b.apply_choice(0)  # drive
        summary = (
            render_confirmation_summary(
                b,
                caverns_pack,
                "Parsley",
                "p1",
            ).payload.summary
        ) or ""
        # Drive scene's choice label wins; origin routing tag is hidden.
        assert "Backstory: Someone Went Into The Drift" in summary
        assert "Outsystem" not in summary

    def test_origin_background_still_used_when_no_drive_scene(
        self,
        caverns_pack: GenrePack,
    ) -> None:
        """Mutant_wasteland-shape: origin scene's ``background`` IS the
        meaningful label ("Vault Dweller", "Heap Rat"). No drive scene
        sets relationship/goals — the existing fallback to
        ``acc.background`` must still fire.
        """
        scenes = [
            make_scene(
                "origins",
                choices=[
                    make_choice(
                        "A Sealed Vault",
                        race_hint="Pure Strain Human",
                        background="Vault Dweller",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (
            render_confirmation_summary(
                b,
                caverns_pack,
                "Rux",
                "p1",
            ).payload.summary
        ) or ""
        assert "Backstory: Vault Dweller" in summary

    def test_drive_choice_with_race_hint_does_not_overwrite_label(
        self,
        caverns_pack: GenrePack,
    ) -> None:
        """Heuristic guard: a scene that sets BOTH inner-life fields AND
        race_hint is treated as origin/profession-shape, NOT drive-shape.
        Avoids polluting backstory_label with the chosen origin label
        when an unusual genre couples both.
        """
        scenes = [
            make_scene(
                "origin_with_drive_fields",
                choices=[
                    make_choice(
                        "The Drifter",
                        race_hint="Human",
                        background="Drifter",
                        emotional_state="restless",
                    ),
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (
            render_confirmation_summary(
                b,
                caverns_pack,
                "Rux",
                "p1",
            ).payload.summary
        ) or ""
        # Falls back to background — heuristic correctly didn't
        # promote the choice label to backstory_label.
        assert "Backstory: Drifter" in summary

    def test_freeform_prose_backstory_preserves_sentence_case(
        self, caverns_pack: GenrePack
    ) -> None:
        """sq-playtest 2026-05-09 [BUG-LOW]: the_story flow routes
        sentence-case prose into ``acc.background``. Pre-fix, every word
        was Title-Cased, mangling "Two delves under his belt." into
        "Two Delves Under His Belt." Detect prose by sentence punctuation
        and pass it through unchanged.
        """
        prose = "Two delves under his belt. Came back from the second with a tooth in a cloth bag."
        scenes = [make_scene("origin", choices=[make_choice("Human", background=prose)])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (
            render_confirmation_summary(b, caverns_pack, "Narder", "p1").payload.summary
        ) or ""
        assert prose in summary
        assert "Two Delves Under His Belt" not in summary

    def test_pipe_separated_background_description_renders_as_paragraphs(
        self, caverns_pack: GenrePack
    ) -> None:
        """the_story scene joins background + description with " | " in
        ``MechanicalEffects.background``. Display splits on the pipe so
        the player-facing summary shows two stacked paragraphs instead
        of a literal pipe.
        """
        joined = (
            "Former cobbler with ambitions. Ears pierced with bent iron nails. | "
            "Tall, soot-stained, missing a tooth."
        )
        scenes = [make_scene("origin", choices=[make_choice("Human", background=joined)])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        summary = (
            render_confirmation_summary(b, caverns_pack, "Willes", "p1").payload.summary
        ) or ""
        assert " | " not in summary
        assert "Former cobbler with ambitions." in summary
        assert "Tall, soot-stained, missing a tooth." in summary

    def test_message_wire_shape(self, caverns_pack: GenrePack) -> None:
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)
        msg = render_confirmation_summary(b, caverns_pack, "Rux", "player-xyz")

        assert msg.type == "CHARACTER_CREATION"
        assert msg.player_id == "player-xyz"
        p = msg.payload
        assert p.phase == "confirmation"
        assert p.scene_index is None
        assert p.total_scenes == 1
        assert p.summary is not None and p.summary != ""
        assert p.prompt is None
        assert p.choices is None
        assert p.allows_freeform is None
        assert p.input_type is None
        assert p.rolled_stats is None
        assert p.character is None
        assert p.action is None


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class TestTelemetry:
    def test_emits_confirmation_rendered_event_with_sources(self, caverns_pack: GenrePack) -> None:
        inv = InventoryConfig(starting_equipment={"Ranger": ["short_bow"]})
        pack = _clone_with_inventory(caverns_pack, inv)
        scenes = [
            make_scene(
                "class",
                choices=[make_choice("Ranger", class_hint="Ranger", item_hint="compass")],
            )
        ]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)

        provider, _ = _fresh_otel()
        events = _capture_events(
            provider, lambda: render_confirmation_summary(b, pack, "Rux", "player-xyz")
        )
        rendered = [e for e in events if e.name == "character_creation.confirmation_rendered"]
        assert len(rendered) == 1
        attrs = dict(rendered[0].attributes or {})
        assert attrs["event"] == "confirmation_rendered"
        assert attrs["name_source"] == "lobby"
        assert attrs["has_name"] is True
        assert attrs["equipment_source"] == "merged"
        assert attrs["equipment_count"] == 2
        assert attrs["lookup_class"] == "Ranger"
        assert attrs["has_rolled_stats"] is False
        assert attrs["player_id"] == "player-xyz"

    def test_empty_sources_reported_as_none(self, caverns_pack: GenrePack) -> None:
        pack = _clone_with_inventory(caverns_pack, InventoryConfig())
        scenes = [make_scene("origin", choices=[make_choice("Human", race_hint="Human")])]
        b = CharacterBuilder(scenes=scenes, rules=simple_rules())
        b.apply_choice(0)

        provider, _ = _fresh_otel()
        events = _capture_events(
            provider, lambda: render_confirmation_summary(b, pack, None, "player-xyz")
        )
        rendered = [e for e in events if e.name == "character_creation.confirmation_rendered"]
        assert len(rendered) == 1
        attrs = dict(rendered[0].attributes or {})
        assert attrs["name_source"] == "none"
        assert attrs["has_name"] is False
        assert attrs["equipment_source"] == "none"
        assert attrs["equipment_count"] == 0


# ---------------------------------------------------------------------------
# chargen_field_labels — per-pack character-sheet vocabulary
# ---------------------------------------------------------------------------


class TestChargenFieldLabels:
    """Verifies that ``rules.chargen_field_labels`` re-labels the
    confirmation summary lines and the structured ``character_preview``
    dict that's emitted alongside the joined summary text.

    Bug context: the Tea & Murder pack rendered "Race: Colonial" because
    the chargen summary hard-coded English fantasy labels regardless
    of the genre pack. The fix routes every label through
    ``field_label(rules, key)``, which prefers the per-pack override
    when present and falls back to the canonical default otherwise.
    """

    def _tea_and_murder_rules(self) -> RulesConfig:
        return RulesConfig(
            stat_generation="point_buy",
            point_buy_budget=27,
            ability_score_names=["STR", "DEX", "CON", "INT", "WIS", "CHA"],
            chargen_field_labels={
                "race": "Origin",
                "class": "Calling",
                "personality": "Bearing",
                "backstory": "Past",
            },
        )

    def test_pydantic_round_trip_preserves_labels(self) -> None:
        # Schema test — round-trip a YAML-style dict through pydantic
        # and confirm the override map survives validation + dump.
        raw = {
            "stat_generation": "point_buy",
            "point_buy_budget": 27,
            "chargen_field_labels": {
                "race": "Origin",
                "class": "Calling",
            },
        }
        rules = RulesConfig.model_validate(raw)
        assert rules.chargen_field_labels == {"race": "Origin", "class": "Calling"}
        dumped = rules.model_dump(exclude_defaults=True)
        assert dumped["chargen_field_labels"] == {"race": "Origin", "class": "Calling"}

    def test_field_label_helper_precedence(self) -> None:
        from sidequest.server.dispatch.chargen_summary import (
            DEFAULT_CHARGEN_FIELD_LABELS,
            field_label,
        )

        # 1. chargen_field_labels override wins.
        rules = RulesConfig(
            stat_generation="point_buy",
            chargen_field_labels={"race": "Origin"},
            race_label="Species",
        )
        assert field_label(rules, "race") == "Origin"

        # 2. Legacy race_label/class_label honored when the new map omits.
        rules = RulesConfig(stat_generation="point_buy", race_label="Species")
        assert field_label(rules, "race") == "Species"

        # 3. Default fallback when nothing is set.
        rules = RulesConfig(stat_generation="point_buy")
        assert field_label(rules, "race") == DEFAULT_CHARGEN_FIELD_LABELS["race"]
        assert field_label(rules, "personality") == "Personality"
        assert field_label(rules, "backstory") == "Backstory"

    def test_summary_uses_overridden_labels(self, caverns_pack: GenrePack) -> None:
        rules = self._tea_and_murder_rules()
        scenes = [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "Colonial",
                        race_hint="Colonial",
                        personality_trait="guarded",
                    )
                ],
            ),
            make_scene("class", choices=[make_choice("Detective", class_hint="Detective")]),
            make_scene(
                "past",
                choices=[make_choice("Returned", background="Returned")],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_choice(0)
        b.apply_choice(0)
        b.apply_choice(0)

        msg = render_confirmation_summary(b, caverns_pack, "Lady Victoria", "p1")
        summary = msg.payload.summary or ""

        # Pre-fix breakage was "Race: Colonial" — assert the new label.
        assert "Origin: Colonial" in summary
        assert "Calling: Detective" in summary
        # Personality is humanize_display'd (playtest 2026-04-30 #casing) —
        # "guarded" → "Guarded".
        assert "Bearing: Guarded" in summary
        assert "Past: Returned" in summary
        # And the broken labels are gone.
        assert "Race:" not in summary
        assert "Class:" not in summary
        assert "Personality:" not in summary
        assert "Backstory:" not in summary

    def test_character_preview_dict_uses_resolved_labels(self, caverns_pack: GenrePack) -> None:
        rules = self._tea_and_murder_rules()
        scenes = [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "Colonial",
                        race_hint="Colonial",
                        personality_trait="guarded",
                    )
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_choice(0)

        msg = render_confirmation_summary(b, caverns_pack, "Lady Victoria", "p1")
        preview = msg.payload.character_preview
        assert isinstance(preview, dict)
        # Keys are the genre-resolved display labels — UI renders them
        # verbatim. Values are humanize_display'd so YAML kebab/snake
        # tokens land Title-cased (playtest 2026-04-30 #casing).
        assert preview["Name"] == "Lady Victoria"
        assert preview["Origin"] == "Colonial"
        assert preview["Bearing"] == "Guarded"
        assert "Race" not in preview
        assert "Personality" not in preview

    def test_character_preview_falls_back_to_defaults(self, caverns_pack: GenrePack) -> None:
        # No chargen_field_labels set → defaults preserved (existing
        # packs unaffected, per the No-Silent-Fallbacks principle:
        # this is an intentional default, not a hidden alternative).
        rules = simple_rules()
        scenes = [
            make_scene(
                "origin",
                choices=[
                    make_choice(
                        "Human",
                        race_hint="Human",
                        personality_trait="brooding",
                    )
                ],
            ),
        ]
        b = CharacterBuilder(scenes=scenes, rules=rules)
        b.apply_choice(0)

        msg = render_confirmation_summary(b, caverns_pack, "Rux", "p1")
        preview = msg.payload.character_preview
        assert isinstance(preview, dict)
        assert preview["Name"] == "Rux"
        assert preview["Race"] == "Human"
        # humanize_display title-cases single-word lowercase tokens too —
        # canonical surface is TitleCase across the preview row.
        assert preview["Personality"] == "Brooding"
