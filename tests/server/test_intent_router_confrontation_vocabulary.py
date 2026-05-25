"""Tests for Story 59-10: confrontation type vocabulary in the Intent Router's
state summary.

The Intent Router (ADR-113) replaced narrator-driven confrontation engagement
in Story 59-4, but the router's Haiku call lacked genre-specific confrontation
type vocabulary. These tests verify that ``_build_state_summary`` injects the
pack's confrontation types into the state dict, that the call site wires pack
through, and that the OTEL span fires.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.server.intent_router_pass import _build_state_summary

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"


def _load_test_pack():
    return load_genre_pack(_FIXTURE_PACK)


@pytest.fixture
def test_pack():
    return _load_test_pack()


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# AC-1: Router user prompt includes confrontation type vocabulary
# ---------------------------------------------------------------------------


def test_state_summary_includes_confrontation_types_from_pack(test_pack) -> None:
    """When pack is provided, _build_state_summary includes a
    confrontation_types key with type/category/intent_verbs per def."""
    snap = GameSnapshot(genre="caverns_and_claudes")
    summary = _build_state_summary(snap, pack=test_pack)

    assert "confrontation_types" in summary
    types = summary["confrontation_types"]
    assert isinstance(types, list)
    assert len(types) > 0

    type_names = {t["type"] for t in types}
    assert "combat" in type_names

    combat_entry = next(t for t in types if t["type"] == "combat")
    assert combat_entry["category"] == "combat"
    assert set(combat_entry.keys()) == {"type", "category"}


# ---------------------------------------------------------------------------
# AC-2: _build_state_summary signature accepts pack (backward compat)
# ---------------------------------------------------------------------------


def test_state_summary_without_pack_has_no_confrontation_types() -> None:
    """When pack is None (default), confrontation_types key is absent."""
    snap = GameSnapshot(genre="caverns_and_claudes")
    summary = _build_state_summary(snap)
    assert "confrontation_types" not in summary


def test_state_summary_with_pack_none_explicit_has_no_confrontation_types() -> None:
    """Explicit pack=None also omits the key."""
    snap = GameSnapshot(genre="caverns_and_claudes")
    summary = _build_state_summary(snap, pack=None)
    assert "confrontation_types" not in summary


# ---------------------------------------------------------------------------
# AC-3: execute_intent_router_pre_narrator_pass passes pack to summary builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_narrator_pass_feeds_pack_to_state_summary(test_pack) -> None:
    """The full pre-narrator pass wires pack through to _build_state_summary,
    so the router's Haiku call receives confrontation type vocabulary."""
    from sidequest.protocol.dispatch import DispatchPackage

    snap = GameSnapshot(genre="caverns_and_claudes")
    snap.genre_slug = "caverns_and_claudes"

    empty_package = DispatchPackage(
        turn_id="test-turn",
        per_player=[],
        cross_player=[],
        confidence_global=0.8,
    )

    mock_router = AsyncMock()
    mock_router.decompose = AsyncMock(return_value=empty_package)

    from sidequest.server.intent_router_pass import (
        execute_intent_router_pre_narrator_pass,
    )

    await execute_intent_router_pre_narrator_pass(
        intent_router=mock_router,
        snapshot=snap,
        pack=test_pack,
        action="I look around",
        player_name="Rux",
    )

    mock_router.decompose.assert_called_once()
    call_kwargs = mock_router.decompose.call_args[1]
    state_summary = call_kwargs["state_summary"]
    assert "confrontation_types" in state_summary


# ---------------------------------------------------------------------------
# AC-4: OTEL span on confrontation type vocabulary injection
# ---------------------------------------------------------------------------


def test_vocabulary_injection_emits_otel_span(test_pack, otel_capture) -> None:
    """intent_router.confrontation_vocabulary span fires with type_count
    and genre_slug when vocabulary is injected."""
    snap = GameSnapshot(genre="caverns_and_claudes")
    snap.genre_slug = "caverns_and_claudes"
    _build_state_summary(snap, pack=test_pack)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.confrontation_vocabulary"
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["genre_slug"] == "caverns_and_claudes"
    assert span.attributes["type_count"] > 0


def test_no_vocabulary_span_without_pack(otel_capture) -> None:
    """No vocabulary span fires when pack is not provided."""
    snap = GameSnapshot(genre="caverns_and_claudes")
    _build_state_summary(snap)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "intent_router.confrontation_vocabulary"
    ]
    assert len(spans) == 0
