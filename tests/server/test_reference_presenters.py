"""Unit tests for individual reference presenters.

Per project memory feedback_no_content_coupled_tests: fixtures only, no
live pack content. Each test constructs a synthetic node + PresenterContext
and asserts on the returned HTML fragment."""

from __future__ import annotations

import pytest

from sidequest.server.reference_presenters import PresenterContext
from sidequest.server.reference_theme import ReferenceTheme


@pytest.fixture
def fake_theme() -> ReferenceTheme:
    return ReferenceTheme(
        archetype="terminal",
        palette_primary="#4A90D9",
        palette_accent="#E8A838",
        palette_background="#0D1117",
        web_font_family="Rajdhani",
        display_font_family="Orbitron",
        dinkus_light="·",
        dinkus_medium="✦",
        dinkus_heavy="✦✦",
    )


def make_ctx(file_stem: str, key_path: tuple[str, ...], theme: ReferenceTheme) -> PresenterContext:
    return PresenterContext(
        pack="space_opera",
        world="coyote_star",
        file_stem=file_stem,
        key_path=key_path,
        theme=theme,
        depth=1,
    )


def test_world_name_suppress_returns_empty(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_world_name_suppress

    html = present_world_name_suppress("Coyote Star", make_ctx("lore", ("world_name",), fake_theme))
    assert html == ""


def test_setting_anchor_emits_narrative_flourish(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_lore_setting_anchor

    html = present_lore_setting_anchor(
        "One jump point in, one jump point out.",
        make_ctx("lore", ("setting_anchor",), fake_theme),
    )
    assert '<p class="narrative-flourish">' in html
    assert "<h2>" not in html
    assert "One jump point in" in html


def test_history_splits_on_double_newline(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_lore_history

    prose = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    html = present_lore_history(prose, make_ctx("lore", ("history",), fake_theme))

    # First paragraph should be pull-quoted with a drop-cap
    assert '<p class="ref-pull-quote' in html
    assert '<span class="ref-pull-quote__dropcap">F</span>' in html
    # Subsequent paragraphs are flat <p>
    assert "<p>Second paragraph.</p>" in html
    assert "<p>Third paragraph.</p>" in html


def test_history_emits_dinkus_between_groups(fake_theme: ReferenceTheme) -> None:
    """A dinkus divider appears between every 3rd–4th paragraph as a rest."""
    from sidequest.server.reference_presenters import present_lore_history

    prose = "\n\n".join(f"Paragraph {i}." for i in range(1, 8))
    html = present_lore_history(prose, make_ctx("lore", ("history",), fake_theme))
    # 7 paragraphs → at least one dinkus rest
    assert html.count('<hr class="ref-dinkus"') >= 1


def test_cosmology_emits_pull_quote_with_dinkus_surround(
    fake_theme: ReferenceTheme,
) -> None:
    from sidequest.server.reference_presenters import present_lore_cosmology

    html = present_lore_cosmology(
        "The void is a sea, and the gods are storms.",
        make_ctx("lore", ("cosmology",), fake_theme),
    )
    assert '<hr class="ref-dinkus"' in html
    assert '<p class="ref-pull-quote' in html
    assert "void is a sea" in html


def test_factions_emits_card_grid_with_disposition_badge(
    fake_theme: ReferenceTheme,
) -> None:
    from sidequest.server.reference_presenters import present_lore_factions

    factions = [
        {
            "name": "Vaskov Administration",
            "summary": "Governor's office on the habitable world.",
            "description": "Prefect Ilara Vaskov inherited the post…",
            "disposition": "neutral",
        },
        {
            "name": "Broken Drift Runners",
            "summary": "Outlaw clan that cut their tattoos.",
            "description": "Lost the Moana-Teru name when they ran.",
            "disposition": "hostile",
        },
    ]
    html = present_lore_factions(factions, make_ctx("lore", ("factions",), fake_theme))

    # Grid wrapper
    assert 'class="ref-card-grid ref-card-grid--cols-3"' in html
    # Per-card structure
    assert '<article class="ref-card" id="cult-vaskov-administration">' in html
    assert '<article class="ref-card" id="cult-broken-drift-runners">' in html
    # Card title is h3, NOT h2
    assert '<h3 class="ref-card__title">Vaskov Administration</h3>' in html
    # Kicker
    assert '<div class="ref-card__kicker">Faction</div>' in html
    # Summary
    assert "Governor's office on the habitable world." in html
    # Disposition badges
    assert 'class="ref-badge ref-badge--disposition-neutral">Neutral</span>' in html
    assert 'class="ref-badge ref-badge--disposition-hostile">Hostile</span>' in html
    # Field-name headings absent
    assert "<h2>name</h2>" not in html
    assert "<h2>summary</h2>" not in html
    assert "<h2>disposition</h2>" not in html
    assert "<h2>description</h2>" not in html


def test_factions_unknown_disposition_falls_back_to_neutral(
    fake_theme: ReferenceTheme,
) -> None:
    from sidequest.server.reference_presenters import present_lore_factions

    factions = [
        {
            "name": "X",
            "summary": "y",
            "description": "z",
            "disposition": "mysterious",
        }
    ]
    html = present_lore_factions(factions, make_ctx("lore", ("factions",), fake_theme))
    # Unknown disposition still emits a badge — does not crash. Use neutral
    # as fallback class so the chrome-wiring guard doesn't see an undefined
    # CSS class.
    assert 'class="ref-badge ref-badge--disposition-neutral">Mysterious</span>' in html


def test_world_meta_emits_label_grid(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_world_meta

    node = {
        "name": "Coyote Star",
        "description": "A lawless stretch of space beyond the Hegemony.",
        "axis_snapshot": {"scale": "intimate", "tone": "dry", "swagger": "high"},
        "starting_location": "kestrel_galley",
        "starting_time": "mid-coast",
        "cover_poi": "gate_approach",
    }
    html = present_world_meta(node, make_ctx("world", (), fake_theme))

    assert 'class="ref-world-meta"' in html
    assert 'class="ref-label-grid"' in html
    assert 'class="ref-label-grid__cell"' in html
    # Axis snapshot values
    assert "intimate" in html
    assert "dry" in html
    assert "high" in html
    # Starting fields
    assert "kestrel_galley" in html
    assert "mid-coast" in html
    # Description as narrative flourish
    assert 'class="narrative-flourish"' in html
    assert "lawless stretch" in html
    # cover_poi must NOT appear — daemon hint
    assert "gate_approach" not in html
    # No raw field-name headings
    assert "<h2>name</h2>" not in html
    assert "<h2>axis_snapshot</h2>" not in html


def test_history_chapters_emits_timeline(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_history_chapters

    chapters = [
        {
            "id": "ch1",
            "label": "The First Crossing",
            "session_range": [1, 3],
            "description": "The crew arrived at the gate.",
            "events": ["event1", "event2"],
            "points_of_interest": ["poi1"],
        },
        {
            "id": "ch2",
            "label": "The Long Coast",
            "description": "Weeks of quiet transit.",
        },
    ]
    html = present_history_chapters(chapters, make_ctx("history", ("chapters",), fake_theme))

    assert 'class="ref-timeline"' in html
    assert 'class="ref-timeline__chapter"' in html
    assert 'class="ref-card__title"' in html
    assert "The First Crossing" in html
    assert "The Long Coast" in html
    assert "The crew arrived" in html
    assert "Weeks of quiet transit" in html
    # Session range chip
    assert 'class="ref-chip"' in html
    assert "Sessions 1" in html
    # events and points_of_interest NOT rendered in v1
    assert "event1" not in html
    assert "poi1" not in html
    # No raw field-name headings
    assert "<h2>label</h2>" not in html
    assert "<h2>description</h2>" not in html


def test_calendar_emits_table(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_calendar

    node = {
        "days_per_week": 8,
        "months": ["Frostmarch", "Thawmonth", "Bloomtide"],
        "year_length": 320,
    }
    html = present_calendar(node, make_ctx("calendar", (), fake_theme))

    assert 'class="ref-table"' in html
    assert "<th>" in html
    assert "days_per_week" in html
    assert "8" in html
    assert "Frostmarch" in html
    assert "year_length" in html
    # No raw h2 field-name headings
    assert "<h2>days_per_week</h2>" not in html


def test_demographics_emits_label_grid(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_demographics

    node = {
        "total_population": "~40,000",
        "dominant_culture": "Hegemony transplants",
        "languages": ["Jovian pidgin", "Standard"],
    }
    html = present_demographics(node, make_ctx("demographics", (), fake_theme))

    assert 'class="ref-label-grid"' in html
    assert 'class="ref-label-grid__cell"' in html
    assert 'class="ref-card__kicker"' in html
    assert "Total Population" in html
    assert "~40,000" in html
    assert "Dominant Culture" in html
    assert "Hegemony transplants" in html
    # No raw h2 field-name headings
    assert "<h2>total_population</h2>" not in html


def test_legends_emits_article_cards(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_legends

    legends = [
        {
            "name": "The First Crossing",
            "era": "Pre-collapse",
            "summary": "When the gate was found.",
            "cultural_impact": "Changed everything about how people thought of distance.",
            "affected_cultures": ["moana-teru", "vacworld-born"],
            "terrain_scars": ["gate-scar"],
        },
        {
            "name": "The Long Winter",
            "summary": "A generation of darkness.",
        },
    ]
    html = present_legends(legends, make_ctx("legends", (), fake_theme))

    assert 'class="ref-legends"' in html
    assert 'class="ref-card"' in html
    assert 'class="ref-card__title"' in html
    assert "The First Crossing" in html
    assert "Pre-collapse" in html
    assert "When the gate was found." in html
    assert "Changed everything" in html
    assert "The Long Winter" in html
    # v1 skips terrain_scars and affected_cultures
    assert "moana-teru" not in html
    assert "gate-scar" not in html
    # No raw h2 headings
    assert "<h2>name</h2>" not in html
    assert "<h2>era</h2>" not in html


def test_openings_emits_card_grid(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_openings

    node = [
        {
            "id": "solo_came_through_gate",
            "name": "Galley, Coast — Gate Behind",
            "establishing_narration": "Mid-coast. The Kestrel hums.",
            "starting_location": "kestrel_galley",
            "triggers": {"mode": "solo"},
        },
        {
            "id": "mp_galley",
            "name": "Galley, Jumprest",
            "establishing_narration": "Jumpspace always smells of burnt sweetness.",
        },
    ]
    html = present_openings(node, make_ctx("openings", (), fake_theme))

    assert 'class="ref-card-grid ref-card-grid--cols-3"' in html
    assert 'class="ref-card"' in html
    assert 'class="ref-card__title"' in html
    assert "Galley, Coast — Gate Behind" in html
    assert "Galley, Jumprest" in html
    assert "Mid-coast" in html
    assert "burnt sweetness" in html
    assert 'class="ref-pull-quote"' in html
    # fields beyond title/prose are silently dropped
    assert "kestrel_galley" not in html
    assert "<h2>name</h2>" not in html


def test_cultures_emits_card_grid(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_cultures

    cultures = [
        {
            "name": "Vacworld-Born",
            "summary": "Children of the orbital ring stations.",
            "description": "A pragmatic people who grew up recycling everything.",
            "slots": {"naming": "markov", "corpus_file": "vacworld.txt"},
        },
        {
            "name": "Moana-Teru",
            "summary": "Ocean-traders from the habitable world.",
        },
    ]
    html = present_cultures(cultures, make_ctx("cultures", (), fake_theme))

    assert 'class="ref-card-grid ref-card-grid--cols-3"' in html
    assert 'class="ref-card"' in html
    assert '<div class="ref-card__kicker">Culture</div>' in html
    assert "Vacworld-Born" in html
    assert "Children of the orbital ring" in html
    assert "pragmatic people" in html
    assert "Moana-Teru" in html
    # slots MUST NOT appear — generator config
    assert "markov" not in html
    assert "corpus_file" not in html
    assert "slots" not in html
    # No raw h2 headings
    assert "<h2>name</h2>" not in html
    assert "<h2>summary</h2>" not in html


def test_geography_emits_poi_card_grid(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_lore_geography

    pois = [
        {
            "id": "vaskov-centrum",
            "name": "Vaskov Centrum",
            "region": "habitable_world",
            "type": "city",
            "environment": "humid coastal capital",
            "description": "The governor's seat; rain-soaked rooftops and tax offices.",
            "visual_prompt": "skyline at dusk",  # ignored by presenter, present in YAML
        },
        {
            "id": "broken-drift",
            "name": "The Broken Drift",
            "region": "asteroid_belt",
            "type": "void_drift",
            "environment": "low-g shipwreck field",
            "description": "Where the lost ships go.",
        },
    ]
    html = present_lore_geography(pois, make_ctx("lore", ("geography",), fake_theme))

    assert 'class="ref-card-grid"' in html
    assert '<article class="ref-card" id="location-vaskov-centrum">' in html
    assert '<h3 class="ref-card__title">Vaskov Centrum</h3>' in html
    assert '<span class="ref-chip">City</span>' in html
    assert '<span class="ref-chip">Habitable World</span>' in html
    assert "rain-soaked rooftops" in html
    # Unknown-to-presenter fields (visual_prompt) MUST NOT leak as headings
    assert "<h2>visual_prompt" not in html


def test_rules_root_emits_axiom_strip(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_rules_root

    node = {
        "stat_generation": "point_buy",
        "point_buy_budget": 27,
        "magic_level": "none",
        "lethality": "moderate",
        "tone": "gonzo-sincere",
        "default_class": "Smuggler",
        "default_race": "Spacer",
        "default_time_of_day": "morning",
        "race_label": "Origin",
        "ability_score_names": ["STR", "DEX", "CON", "INT", "WIS", "CHA"],
        "allowed_classes": ["Smuggler", "Pilot", "Medic"],
        "allowed_races": ["Spacer", "Coreworlder"],
        "default_location": "The crew quarters of a beat-up freighter.",
        "custom_rules": {
            "ship_combat": "Ships have condition tracks, not HP.",
            "crew_bonds": "Bond advantage on protect rolls.",
        },
        "confrontations": "should be ignored",
        "edge_config": {"thresholds": []},
    }
    html = present_rules_root(node, make_ctx("rules", (), fake_theme))

    # Axiom strip — 5 cards in priority order
    assert html.count('class="ref-stat-card"') == 5
    # Default frame label grid
    assert 'class="ref-label-grid"' in html
    assert ">Smuggler<" in html and ">Spacer<" in html and ">morning<" in html
    # Origin label (custom race_label) wins over default "Race"
    assert "Origin" in html
    # Allowed chips
    assert html.count('class="ref-chip">') >= 5  # 3 classes + 2 races
    assert ">Classes<" in html or ">Origins<" in html
    # Opening location pull-quote
    assert '<p class="ref-pull-quote' in html
    assert "crew quarters of a beat-up freighter" in html
    # Custom rules cards
    assert html.count('class="ref-card"') >= 2  # both ship_combat and crew_bonds
    assert "Ships have condition tracks" in html
    # Silent skip
    assert "should be ignored" not in html
    assert "edge_config" not in html


def test_rules_root_omits_axioms_for_missing_keys(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_rules_root

    node = {
        "stat_generation": "roll_3d6_strict",
        "magic_level": "none",
        # lethality, tone, default_class absent
    }
    html = present_rules_root(node, make_ctx("rules", (), fake_theme))
    assert html.count('class="ref-stat-card"') == 2  # only 2 keys present


def test_archetypes_picker_emits_island_markup(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_archetypes_picker

    archetypes = [
        {
            "name": "Station Bartender",
            "description": "Knows everyone.",
            "personality_traits": ["perceptive", "discreet"],
            "typical_classes": ["Smuggler"],
        },
        {
            "name": "Dock Rat",
            "description": "Sells passage for food.",
            "personality_traits": ["skittish"],
        },
    ]
    html = present_archetypes_picker(archetypes, make_ctx("archetypes", (), fake_theme))

    assert 'data-island="picker"' in html
    assert html.count('class="ref-picker__chip"') == 2
    # Default chip selected
    assert 'aria-selected="true"' in html
    # Exactly one hidden panel (second item)
    assert html.count(" hidden>") == 1
    # Per-panel content
    assert "Knows everyone" in html and "Sells passage" in html
    # Chip strip for personality_traits
    assert "perceptive" in html and "discreet" in html
    # Anchor IDs use 'archetype-' prefix
    assert 'data-target="archetype-station-bartender"' in html


def test_classes_picker_emits_island_markup(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_classes_picker

    classes = [
        {
            "id": "fighter",
            "display_name": "Fighter",
            "rpg_role": "tank",
            "prime_requisite": "STR",
            "flavor": "Plate, polearm, patience.",
            "encounter_beat_choices": ["attack", "defend", "flee"],
        },
        {
            "id": "rogue",
            "display_name": "Rogue",
            "rpg_role": "scout",
            "prime_requisite": "DEX",
            "flavor": "In and out.",
            "encounter_beat_choices": ["sneak_attack", "hide"],
        },
    ]
    html = present_classes_picker(classes, make_ctx("classes", (), fake_theme))

    assert 'data-island="picker"' in html
    assert html.count('class="ref-picker__chip"') == 2
    assert 'data-target="class-fighter"' in html
    assert "Plate, polearm" in html
    # Label grid for role + prime requisite
    assert "ref-label-grid" in html
    assert "Tank" in html or "tank" in html
    assert "STR" in html


def test_picker_no_ops_on_empty_input(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import (
        present_archetypes_picker,
        present_classes_picker,
    )

    assert present_archetypes_picker([], make_ctx("archetypes", (), fake_theme)) == ""
    assert present_classes_picker([], make_ctx("classes", (), fake_theme)) == ""
    assert present_archetypes_picker("not a list", make_ctx("archetypes", (), fake_theme)) == ""


# ---------------------------------------------------------------------------
# present_progression
# ---------------------------------------------------------------------------


def test_progression_emits_affinity_cards(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_progression

    node = {
        "affinities": [
            {
                "name": "Command",
                "description": "Leadership and tactics.",
                "triggers": ["lead the crew", "make tactical calls"],
                "tier_thresholds": [6, 15, 30],
            },
            {
                "name": "Grit",
                "description": "Endurance under fire.",
                "triggers": ["take a hit"],
            },
        ]
    }
    html = present_progression(node, make_ctx("progression", (), fake_theme))
    assert "ref-progression" in html
    assert "Command" in html
    assert "Grit" in html
    assert "lead the crew" in html
    assert "6 / 15 / 30" in html
    assert "Affinity" in html
    assert "<h2>name</h2>" not in html
    assert "<h2>triggers</h2>" not in html


def test_progression_empty_returns_empty_string(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_progression

    assert present_progression({}, make_ctx("progression", (), fake_theme)) == ""
    assert present_progression({"other_key": "x"}, make_ctx("progression", (), fake_theme)) == ""


# ---------------------------------------------------------------------------
# present_magic
# ---------------------------------------------------------------------------


def test_magic_emits_label_grid(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_magic

    node = {
        "genre": "space_opera",
        "allowed_sources": ["innate", "item_based"],
        "permitted_plugins": ["psionics", "force_fields"],
    }
    html = present_magic(node, make_ctx("magic", (), fake_theme))
    assert "ref-label-grid" in html
    assert "innate" in html
    assert "item_based" in html
    assert "psionics" in html
    assert "Sources" in html
    assert "<h2>genre</h2>" not in html
    assert "<h2>allowed_sources</h2>" not in html


def test_magic_empty_returns_empty_string(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_magic

    assert present_magic({}, make_ctx("magic", (), fake_theme)) == ""
    assert present_magic({"unrelated_key": "x"}, make_ctx("magic", (), fake_theme)) == ""


# ---------------------------------------------------------------------------
# present_power_tiers
# ---------------------------------------------------------------------------


def test_power_tiers_emits_class_tables(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_power_tiers

    node = {
        "Officer": [
            {
                "level_range": [1, 3],
                "label": "ensign",
                "player": "a uniform that still creases",
                "npc": "NARRATOR ONLY — should not appear",
            },
            {
                "level_range": [4, 7],
                "label": "lieutenant",
                "player": "commanding small units",
                "npc": "NARRATOR ONLY 2",
            },
        ],
        "Smuggler": [
            {
                "level_range": [1, 3],
                "label": "runner",
                "player": "quick, quiet, cheap",
                "npc": "hidden npc text",
            },
        ],
    }
    html = present_power_tiers(node, make_ctx("power_tiers", (), fake_theme))
    assert "ref-power-tiers" in html
    assert "Officer" in html
    assert "Smuggler" in html
    assert "ensign" in html
    assert "lieutenant" in html
    assert "runner" in html
    assert "1–3" in html
    assert "4–7" in html
    assert 'class="ref-table"' in html
    # npc column must NOT appear
    assert "NARRATOR ONLY" not in html
    assert "hidden npc text" not in html
    # no raw field-name headings
    assert "<h2>label</h2>" not in html


def test_power_tiers_skips_npc_column(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_power_tiers

    node = {
        "Officer": [
            {
                "level_range": [1, 3],
                "label": "ensign",
                "player": "earnest posture",
                "npc": "NARRATOR ONLY — should not appear",
            },
        ]
    }
    html = present_power_tiers(node, make_ctx("power_tiers", (), fake_theme))
    assert "ensign" in html
    assert "earnest posture" in html
    assert "NARRATOR ONLY" not in html


def test_power_tiers_non_dict_returns_empty(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_power_tiers

    assert present_power_tiers([], make_ctx("power_tiers", (), fake_theme)) == ""
    assert present_power_tiers("bad", make_ctx("power_tiers", (), fake_theme)) == ""


# ---------------------------------------------------------------------------
# present_achievements
# ---------------------------------------------------------------------------


def test_achievements_emits_card_grid(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_achievements

    node = [
        {
            "name": "First Blood",
            "condition": "Win a confrontation for the first time.",
            "reward": "+5 XP",
        },
        {
            "name": "Smooth Operator",
            "condition": "Bluff your way past three checkpoints.",
        },
    ]
    html = present_achievements(node, make_ctx("achievements", (), fake_theme))
    assert "ref-card-grid" in html
    assert "First Blood" in html
    assert "Win a confrontation" in html
    assert "+5 XP" in html
    assert "Smooth Operator" in html
    assert "Achievement" in html
    assert "<h2>name</h2>" not in html
    assert "<h2>condition</h2>" not in html


def test_achievements_empty_list_returns_empty_string(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_achievements

    assert present_achievements([], make_ctx("achievements", (), fake_theme)) == ""
    assert (
        present_achievements({"achievements": []}, make_ctx("achievements", (), fake_theme)) == ""
    )


# ---------------------------------------------------------------------------
# present_inventory
# ---------------------------------------------------------------------------


def test_inventory_emits_currency_and_catalog(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_inventory

    node = {
        "currency": {
            "name": "Credits",
            "denominations": ["copper chip", "silver slug", "gold bar"],
        },
        "item_catalog": [
            {
                "id": "blaster",
                "name": "Blaster Pistol",
                "description": "Standard sidearm.",
                "category": "weapon",
                "value": 150,
                "weight": 1.2,
                "rarity": "common",
            },
            {
                "id": "medkit",
                "name": "Medkit",
                "description": "Heals 1d6 HP.",
                "category": "consumable",
                "value": 50,
                "weight": 0.5,
                "rarity": "common",
            },
        ],
    }
    html = present_inventory(node, make_ctx("inventory", (), fake_theme))
    assert "Credits" in html
    assert "copper chip" in html
    assert "Blaster Pistol" in html
    assert "Medkit" in html
    assert "weapon" in html
    assert "consumable" in html
    assert 'class="ref-table"' in html
    assert "<h2>currency</h2>" not in html
    assert "<h2>item_catalog</h2>" not in html


def test_inventory_empty_returns_empty_string(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_inventory

    assert present_inventory({}, make_ctx("inventory", (), fake_theme)) == ""
    assert (
        present_inventory(
            {"item_catalog": [], "currency": None}, make_ctx("inventory", (), fake_theme)
        )
        == ""
    )


# ---------------------------------------------------------------------------
# present_equipment_tables
# ---------------------------------------------------------------------------


def test_equipment_tables_emits_section_per_list_key(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_equipment_tables

    node = {
        "starting_gear": [
            {"name": "Worn Jacket", "slots": 1},
            {"name": "Battered Pistol", "slots": 1},
        ],
        "weapons": [
            {"name": "Combat Knife", "damage": "1d4"},
        ],
    }
    html = present_equipment_tables(node, make_ctx("equipment_tables", (), fake_theme))
    assert "starting_gear" in html or "Starting Gear" in html
    assert "weapons" in html or "Weapons" in html
    assert "Worn Jacket" in html
    assert "Combat Knife" in html
    assert 'class="ref-table"' in html


def test_equipment_tables_non_dict_returns_empty(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_equipment_tables

    assert present_equipment_tables([], make_ctx("equipment_tables", (), fake_theme)) == ""
    assert present_equipment_tables("bad", make_ctx("equipment_tables", (), fake_theme)) == ""
    assert present_equipment_tables({}, make_ctx("equipment_tables", (), fake_theme)) == ""


# ---------------------------------------------------------------------------
# present_beat_vocabulary
# ---------------------------------------------------------------------------


def test_beat_vocabulary_emits_dl_sections(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_beat_vocabulary

    node = {
        "beats": [
            {"name": "advance", "description": "Move forward aggressively."},
            {"name": "parley", "description": "Attempt negotiation."},
        ],
        "obstacles": [
            {"name": "locked_door", "description": "KEEPER content — should NOT appear"},
        ],
    }
    html = present_beat_vocabulary(node, make_ctx("beat_vocabulary", (), fake_theme))
    assert "advance" in html
    assert "parley" in html
    assert "Move forward aggressively" in html
    assert "KEEPER content" not in html
    assert "locked_door" not in html
    assert "<dl" in html
    assert "<h2>beats</h2>" not in html


def test_beat_vocabulary_empty_returns_empty_string(fake_theme: ReferenceTheme) -> None:
    from sidequest.server.reference_presenters import present_beat_vocabulary

    assert present_beat_vocabulary({}, make_ctx("beat_vocabulary", (), fake_theme)) == ""
    assert (
        present_beat_vocabulary({"obstacles": []}, make_ctx("beat_vocabulary", (), fake_theme))
        == ""
    )
