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


def test_unknown_field_fires_warn_span_and_drops_field(
    synthetic_pack: Path,
    synthetic_world: Path,
    captured_spans: InMemorySpanExporter,
) -> None:
    """A YAML field not in PUBLIC ∪ KEEPER fires the WARN span and is
    dropped from rendered output."""
    from sidequest.server.reference_renderer import assemble_lore_page

    (synthetic_world / "lore.yaml").write_text(
        yaml.safe_dump({"history": "Prose.", "totally_made_up_field": "leak"})
    )

    html = assemble_lore_page(
        pack="space_opera",
        world="synth_world",
        pack_dir=synthetic_pack,
        world_dir=synthetic_world,
    )

    assert "totally_made_up_field" not in html, "Unknown field key leaked into HTML"
    assert "leak" not in html, "Unknown field value leaked into HTML"
    unknown_spans = [
        s
        for s in captured_spans.get_finished_spans()
        if s.name == "sidequest.reference.unknown_field"
    ]
    assert unknown_spans, f"Expected unknown_field WARN span; saw {_span_names(captured_spans)}"
    attrs = unknown_spans[0].attributes
    assert attrs["reference.file_stem"] == "lore"
    assert "totally_made_up_field" in attrs["reference.key_path"]


@pytest.mark.xfail(
    reason=(
        "Task 8 enumerates KEEPER reachable from real file walk. "
        "The current KEEPER set holds ('tropes', ()) which classifies the "
        "whole-file root, but _render_dict classifies individual keys "
        "(key_path always has ≥1 element). A tropes.yaml placed at the pack "
        "tier is not reached by assemble_lore_page (tropes.yaml absent from "
        "LORE_PACK_FLAVOR_FILES). KEEPER-via-dict-key dispatch needs either "
        "a dict-keyed KEEPER entry or file-root classification in _render_file "
        "— both are Task 8 scope."
    ),
    strict=False,
)
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

    Uses ``geography`` — a lore PUBLIC field with no registered presenter
    (setting_anchor gained a presenter in Task 6).
    """
    from sidequest.server.reference_renderer import assemble_lore_page

    (synthetic_world / "lore.yaml").write_text(
        yaml.safe_dump({"geography": "A rain-soaked plateau."})
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
