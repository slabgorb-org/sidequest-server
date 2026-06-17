"""Terminal Fate-chargen OTEL spans — ``fate.chargen.derived`` / ``.default_fallback``.

Playtest 2026-06-17 (Keith's "Both" decision, code half): at chargen FINALIZE the
builder emits a terminal lie-detector span — ``derived`` when the interactive steps
authored the sheet, ``default_fallback`` when only the pack-default seed survived (the
silent default-sheet bug). The load guard makes ``default_fallback`` impossible for a
SHIPPED pack, so on the GM panel a ``default_fallback`` span is the alarm. Non-Fate
packs emit neither.

Two layers:
- the span functions emit with the right name + attributes under an injected tracer;
- ``CharacterBuilder.build()`` emits the RIGHT one at finalize (the wiring), captured
  via the documented ``spans.tracer`` monkeypatch (``Span.open`` resolves the default
  tracer there when no ``_tracer`` is threaded — build() threads none, like the seed).
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.builder import CharacterBuilder
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.spans.fate import (
    fate_chargen_default_fallback_span,
    fate_chargen_derived_span,
)

# Reuse the established Fate + native chargen fixtures rather than re-deriving the
# FateConfig / RulesConfig scaffolding (they are module-level builders).
from tests.game.ruleset.test_121_7_fate_interactive_chargen import (
    fate_rules,
    legal_choices,
    make_choice,
    make_scene,
)
from tests.game.test_character_chargen_fields import base_rules


@pytest.fixture
def captured(monkeypatch):
    """Capture spans emitted via ``Span.open`` with no ``_tracer`` (build()-emitted),
    by monkeypatching the lazily-resolved ``spans.tracer`` callable to our exporter.
    Mirrors the ``otel_exporter`` fixture in tests/server/conftest."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(spans_module, "tracer", lambda: provider.get_tracer("test"))
    return exporter


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _build_fate(choices) -> object:
    scenes = [
        make_scene("origins", choices=[make_choice("The City", description="Neon and rain.")])
    ]
    builder = CharacterBuilder(scenes=scenes, rules=fate_rules(with_d20_stats=False))
    if choices is not None:
        builder.record_fate_chargen(choices)
    builder.apply_choice(0)
    return builder.build("Sam Quaid")


def _build_native() -> object:
    scenes = [make_scene("origins", choices=[make_choice("A Soldier", description="War made me.")])]
    builder = CharacterBuilder(scenes=scenes, rules=base_rules())
    builder.apply_choice(0)
    return builder.build("Native Pete")


# ---------------------------------------------------------------------------
# Span functions emit correctly (unit)
# ---------------------------------------------------------------------------


class TestSpanFunctions:
    def test_derived_span_emits_with_attributes(self) -> None:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        fate_chargen_derived_span(
            actor="Sam", skill_count=8, aspect_count=5, refresh=3, _tracer=tracer
        )
        span = next(s for s in exporter.get_finished_spans() if s.name == "fate.chargen.derived")
        assert (span.attributes or {})["field"] == "chargen_derived"
        assert (span.attributes or {})["actor"] == "Sam"
        assert (span.attributes or {})["skill_count"] == 8

    def test_default_fallback_span_emits_with_attributes(self) -> None:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        fate_chargen_default_fallback_span(
            actor="Dot", skill_count=4, aspect_count=2, refresh=3, _tracer=tracer
        )
        span = next(
            s for s in exporter.get_finished_spans() if s.name == "fate.chargen.default_fallback"
        )
        assert (span.attributes or {})["field"] == "chargen_default_fallback"
        assert (span.attributes or {})["actor"] == "Dot"


# ---------------------------------------------------------------------------
# build() emits the RIGHT terminal span (the wiring)
# ---------------------------------------------------------------------------


class TestBuildFinalizeWiring:
    def test_recorded_choices_emit_derived_not_fallback(self, captured) -> None:
        _build_fate(choices=legal_choices())
        names = _names(captured)
        assert "fate.chargen.derived" in names
        assert "fate.chargen.default_fallback" not in names

    def test_no_choices_emit_default_fallback_not_derived(self, captured) -> None:
        _build_fate(choices=None)
        names = _names(captured)
        assert "fate.chargen.default_fallback" in names
        assert "fate.chargen.derived" not in names

    def test_non_fate_build_emits_neither_terminal_span(self, captured) -> None:
        _build_native()
        names = _names(captured)
        assert "fate.chargen.derived" not in names
        assert "fate.chargen.default_fallback" not in names
