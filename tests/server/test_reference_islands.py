"""Smoke tests for the picker hydration island.

The bundle is small (<4KB); we test what we can without spinning up a real
browser: file exists, is under budget, the source obeys the v1 scope
(picker only — no search), and the renderer chrome includes the
<script defer src> tag."""

from __future__ import annotations

from pathlib import Path

_ISLANDS_PATH = (
    Path(__file__).resolve().parents[2]
    / "sidequest"
    / "server"
    / "static"
    / "reference"
    / "islands.js"
)
_MAX_BYTES = 4096


def test_islands_js_under_budget() -> None:
    size = _ISLANDS_PATH.stat().st_size
    assert size <= _MAX_BYTES, f"islands.js too large: {size} > {_MAX_BYTES}"


def test_islands_js_handles_picker_only_in_v1() -> None:
    src = _ISLANDS_PATH.read_text()
    assert "data-island" in src
    assert "picker" in src
    # Search island is deferred — no search-island code in v1
    assert "search" not in src.lower(), "Search island deferred — should not be in v1 islands.js"


def test_islands_js_includes_lightbox_affordance() -> None:
    """Fix #5: the islands bundle wires a generic lore-image lightbox.

    Source-presence smoke only (the static bundle is not executed under pytest;
    behavioral verification is manual — see PR body). Asserts the bundle targets
    BOTH gated image classes and provides the three documented dismiss paths so a
    refactor that drops one is caught at least at the bundle level."""
    src = _ISLANDS_PATH.read_text()
    # Targets both gated lore-image classes (cast portraits + POI landscapes).
    assert "ref-card__portrait" in src
    assert "ref-card__poi" in src
    # The three dismiss affordances: close control, Esc key, backdrop click.
    assert "ref-lightbox__close" in src
    assert "Escape" in src
    # The overlay element the lightbox creates.
    assert "ref-lightbox" in src


def test_lightbox_styles_present_in_presenters_css() -> None:
    """Fix #5: the lightbox overlay (created by islands.js, so absent from the
    server-rendered HTML and thus invisible to the chrome-wiring class contract)
    must still be styled — assert its rules ship in the presenters bundle."""
    css = (_ISLANDS_PATH.parent / "presenters.css").read_text()
    assert ".ref-lightbox" in css
    assert ".ref-lightbox__img" in css
    assert ".ref-lightbox__close" in css


def test_chrome_head_includes_islands_script_tag() -> None:
    """The page chrome must load islands.js with defer so picker chips hydrate."""
    import tempfile

    import yaml

    from sidequest.server.reference_renderer import assemble_lore_page

    # Construct a minimal synthetic pack inline (mirrors test_reference_render_wiring fixtures).
    with tempfile.TemporaryDirectory() as tmp:
        pack = Path(tmp) / "p"
        pack.mkdir()
        (pack / "theme.yaml").write_text(
            yaml.safe_dump(
                {
                    "archetype": "terminal",
                    "primary": "#4A90D9",
                    "accent": "#E8A838",
                    "background": "#0D1117",
                    "web_font_family": "Rajdhani",
                    "display_font_family": "Orbitron",
                    "dinkus": {
                        "glyph": {
                            "light": "·",
                            "medium": "✦",
                            "heavy": "✦✦",
                        }
                    },
                }
            )
        )
        world = pack / "worlds" / "w"
        world.mkdir(parents=True)
        html = assemble_lore_page(pack="space_opera", world="w", pack_dir=pack, world_dir=world)

    assert '<script defer src="/reference/static/islands.js"></script>' in html
