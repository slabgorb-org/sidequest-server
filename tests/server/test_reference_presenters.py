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
