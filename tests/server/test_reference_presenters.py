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
