"""End-to-end wiring tests for the reference body-presenter dispatch.

Drives the real production path (`assemble_lore_page`) with synthetic
fixtures; asserts on rendered HTML + OTEL spans emitted via the
sidequest.telemetry.spans.tracer hook.

Per server CLAUDE.md "No Source-Text Wiring Tests" — these tests construct
synthetic content + run the real assemblers; they do NOT grep source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# --- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def captured_spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a per-test tracer that exports to an in-memory buffer.

    Mirrors tests/server/test_opposed_check_wiring.py — monkeypatch
    spans.tracer so each test gets a fresh exporter.
    """
    from sidequest.telemetry import spans as spans_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exporter


@pytest.fixture
def synthetic_pack(tmp_path: Path) -> Path:
    """Minimal pack dir with the bare-minimum YAML for assemble_lore_page."""
    pack = tmp_path / "synth_pack"
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
    return pack


@pytest.fixture
def synthetic_world(synthetic_pack: Path) -> Path:
    world = synthetic_pack / "worlds" / "synth_world"
    world.mkdir(parents=True)
    return world


def _span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


# --- Tests ------------------------------------------------------------------


def test_keeper_field_silently_dropped(
    synthetic_pack: Path,
    synthetic_world: Path,
    captured_spans: InMemorySpanExporter,
) -> None:
    """KEEPER field is dropped silently — no per-render span. Load-time
    validation covers this."""
    from sidequest.server.reference_renderer import assemble_lore_page

    # tropes.yaml is in KEEPER as ("tropes", ())
    (synthetic_pack / "tropes.yaml").write_text(
        yaml.safe_dump([{"id": "spoiler-trope", "trigger": "spoiler-text"}])
    )

    html = assemble_lore_page(
        pack="space_opera",
        world="synth_world",
        pack_dir=synthetic_pack,
        world_dir=synthetic_world,
    )

    # The pack-flavor renderer reads tropes.yaml as a file; the dispatcher
    # sees ("tropes", ()) at file-root and classifies KEEPER → file body is
    # empty. The file wrapper may still be emitted but contents must not
    # include spoiler-text.
    assert "spoiler-text" not in html


def test_public_unpresented_field_renders_via_generic_fallback(
    synthetic_pack: Path,
    synthetic_world: Path,
    captured_spans: InMemorySpanExporter,
) -> None:
    """A PUBLIC field with no presenter renders via generic fallback and
    fires the unpresented_field INFO span.

    Uses ``cultural_notes`` — a lore PUBLIC field with no registered
    presenter (geography and setting_anchor both have presenters now).
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    (synthetic_world / "lore.yaml").write_text(
        yaml.safe_dump({"cultural_notes": "A rain-soaked plateau."})
    )

    html = assemble_lore_page(
        pack="space_opera",
        world="synth_world",
        pack_dir=synthetic_pack,
        world_dir=synthetic_world,
    )

    assert "rain-soaked plateau" in html, "PUBLIC field must render even without a presenter"
    info_spans = [
        s
        for s in captured_spans.get_finished_spans()
        if s.name == "sidequest.reference.unpresented_field"
    ]
    assert info_spans, f"Expected unpresented_field INFO span; saw {_span_names(captured_spans)}"


def test_file_root_presenter_fires_for_top_level_list(
    synthetic_pack: Path, synthetic_world: Path
) -> None:
    """A registry entry under (stem, ()) fires when the YAML root is a list."""
    from sidequest.server.reference_presenters import PRESENTERS
    from sidequest.server.reference_renderer import assemble_lore_page

    sentinel = "<div data-test-sentinel='1'>FILE_ROOT_HIT</div>"

    def _sentinel_presenter(node: object, ctx: object) -> str:
        return sentinel

    (synthetic_world / "cultures.yaml").write_text(yaml.safe_dump([{"name": "X"}, {"name": "Y"}]))
    PRESENTERS[("cultures", ())] = _sentinel_presenter
    try:
        html = assemble_lore_page(
            pack="space_opera",
            world="synth_world",
            pack_dir=synthetic_pack,
            world_dir=synthetic_world,
        )
    finally:
        del PRESENTERS[("cultures", ())]

    assert sentinel in html


def test_file_header_h1_suppressed_for_presented_file(
    synthetic_pack: Path, synthetic_world: Path
) -> None:
    """If a file_stem has any presenter registered, the <h1>{filename}</h1>
    file-header wrapper is suppressed — TOC label provides the section title.
    The section anchor wrapper is still emitted."""
    from sidequest.server.reference_renderer import assemble_lore_page

    (synthetic_world / "lore.yaml").write_text(
        yaml.safe_dump({"history": "Real prose.\n\nSecond paragraph."})
    )
    html = assemble_lore_page(
        pack="space_opera",
        world="synth_world",
        pack_dir=synthetic_pack,
        world_dir=synthetic_world,
    )
    assert "<h1>lore.yaml</h1>" not in html
    # But the section wrapper is still emitted so the anchor works
    assert 'id="file-lore"' in html


def test_file_header_h1_kept_for_unpresented_file(
    synthetic_pack: Path, synthetic_world: Path
) -> None:
    """Files whose stem has no registered presenter still emit the legacy
    <h1>{filename}</h1> file-header — the v1 raw fallback signal stays
    visible to authors during development."""
    from sidequest.server.reference_presenters import PRESENTERS
    from sidequest.server.reference_renderer import (
        EXCLUDED_FILES,
        LORE_WORLD_FILES,
        assemble_lore_page,
    )

    # Find a real LORE_WORLD_FILES entry that has no presenter.
    presented_stems = {reg_stem for reg_stem, _ in PRESENTERS}
    unpresented_lore = [
        filename
        for filename in LORE_WORLD_FILES
        if filename not in EXCLUDED_FILES and filename.removesuffix(".yaml") not in presented_stems
    ]
    if not unpresented_lore:
        # Skip — every world-lore stem has a presenter, which is fine and
        # means this test no longer expresses anything load-bearing.
        import pytest

        pytest.skip("Every LORE_WORLD_FILES stem has a presenter — nothing to assert.")

    target = unpresented_lore[0]
    (synthetic_world / target).write_text(yaml.safe_dump({"some_key": "some_value"}))
    html = assemble_lore_page(
        pack="space_opera",
        world="synth_world",
        pack_dir=synthetic_pack,
        world_dir=synthetic_world,
    )
    assert f"<h1>{target}</h1>" in html
