"""OTEL span tests for the POV swap helper + visibility classifier
(Story 49-8).

GM panel is the lie detector — without OTEL spans on every subsystem
decision, the only signal we have is the wire output. Story 49-8
adds two named spans the GM panel must be able to see:

  - ``narration.visibility_classified`` — emitted once per narration
    turn when the classifier runs. Attributes: anchor_pc (str | None),
    visible_to (str — "all" or comma-joined list), pov_strategy.
  - ``narration.second_person_swap`` — emitted once per recipient who
    received a swapped frame. Attributes: recipient_pc (str),
    swap_target_name (str), swap_count (int).

These tests RED until both modules exist AND emit the named spans.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from sidequest.agents.orchestrator import ActionRewrite, NarrationTurnResult

# Imports at module scope — RED until the helpers exist.
from sidequest.agents.pov_swap import swap_to_second_person
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.server.visibility_classifier import classify_narration_visibility


@pytest.fixture
def otel_capture() -> Iterator:
    """Drain OTEL spans into an in-memory exporter so tests can read
    span names + attributes. Mirrors tests/magic/test_e2e_cnc_memorization.py
    fixture so the dependency footprint is unchanged."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

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


def _pc(name: str, pronouns: str = "he/him") -> Character:
    core = CreatureCore(
        name=name,
        description="A test PC.",
        personality="test",
        inventory=Inventory(),
    )
    return Character(
        core=core,
        backstory="Test wanderer.",
        char_class="Fighter",
        race="Human",
        pronouns=pronouns,
    )


def _spans_named(exporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# visibility classifier emits its OTEL span
# ---------------------------------------------------------------------------


def test_classifier_emits_visibility_classified_span(otel_capture):
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="sunden")
    snap.characters = [_pc("Carl"), _pc("Donut")]
    result = NarrationTurnResult(
        narration="Carl plants a boot.",
        action_rewrite=ActionRewrite(
            you="You plant a boot",
            named="Carl plants a boot",
            intent="plant boot",
        ),
    )
    classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    spans = _spans_named(otel_capture, "narration.visibility_classified")
    assert len(spans) == 1, (
        "classifier must emit exactly one narration.visibility_classified "
        f"span; got names: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes)
    assert attrs.get("anchor_pc") == "Carl"
    assert attrs.get("pov_strategy") == "pc_anchored"
    # visible_to may be 'all' or a list; surface either as a stable str.
    assert attrs.get("visible_to") in {"all", "Carl,Donut", "p1,p2"}


def test_classifier_emits_atmospheric_span(otel_capture):
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="sunden")
    snap.characters = [_pc("Carl"), _pc("Donut")]
    result = NarrationTurnResult(
        narration="Rain hammers the slate roof.",
        action_rewrite=ActionRewrite(you="", named="", intent=""),
    )
    classify_narration_visibility(
        result=result,
        snapshot=snap,
        connected_player_ids=["p1", "p2"],
        player_id_to_character={"p1": "Carl", "p2": "Donut"},
    )
    spans = _spans_named(otel_capture, "narration.visibility_classified")
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    # Atmospheric: anchor_pc absent or empty string (OTEL forbids None
    # attribute values — the impl must encode "no anchor" as empty
    # string or omit the key entirely).
    raw_anchor = attrs.get("anchor_pc", "")
    assert raw_anchor in {"", None}, (
        f"atmospheric span must encode no-anchor cleanly; got {raw_anchor!r}"
    )
    assert attrs.get("pov_strategy") == "atmospheric"


# ---------------------------------------------------------------------------
# pov_swap helper emits its OTEL span
# ---------------------------------------------------------------------------


def test_swap_emits_second_person_swap_span(otel_capture):
    """Each invocation of swap_to_second_person must emit exactly one
    narration.second_person_swap span carrying swap_target_name and
    swap_count attributes the GM panel reads."""
    swap_to_second_person(
        "Carl plants a boot. He hauls the polearm out.",
        target_name="Carl",
        pronouns="he/him",
    )
    spans = _spans_named(otel_capture, "narration.second_person_swap")
    assert len(spans) == 1, (
        "swap_to_second_person must emit one narration.second_person_swap "
        f"span per call; got names: {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes)
    assert attrs.get("swap_target_name") == "Carl"
    swap_count = attrs.get("swap_count")
    assert isinstance(swap_count, int) and swap_count >= 1, (
        f"swap_count must be a positive int; got {swap_count!r}"
    )


def test_swap_with_no_matches_still_emits_span_with_zero_count(otel_capture):
    """When the target name does not appear in the text, the span
    still fires with swap_count=0 — that's a load-bearing signal for
    the GM panel ('classifier said anchor but text had no match' is
    a real bug class)."""
    swap_to_second_person(
        "Rain falls on the slate roof.",
        target_name="Carl",
        pronouns="he/him",
    )
    spans = _spans_named(otel_capture, "narration.second_person_swap")
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs.get("swap_count") == 0
    assert attrs.get("swap_target_name") == "Carl"


# ---------------------------------------------------------------------------
# Story 153-29: swap_count must include the new pronoun substitutions
# ---------------------------------------------------------------------------


def test_swap_count_includes_pronoun_substitutions(otel_capture):
    """AC 7: the narration.second_person_swap span's swap_count must grow to
    include the new possessive / subject / object pronoun edits — not just the
    name + adjacent verb. The GM panel reads swap_count to confirm pronoun
    agreement actually fired (the lie-detector), so re-introducing the gated
    pronoun passes without counting them would leave the panel blind.

    'Carl plants a boot and he hauls his polearm.' ->
    'You plant a boot and you haul your polearm.'
      name(Carl->You)=1, verb(plants->plant)=1, subj-pron(he->you)=1,
      verb(hauls->haul)=1, poss-pron(his->your)=1  => 5 edits.
    Under the retired-pass contract only the name + first verb count (=2),
    so this asserts the count climbs to include the pronoun work."""
    swap_to_second_person(
        "Carl plants a boot and he hauls his polearm.",
        target_name="Carl",
        pronouns="he/him",
    )
    spans = _spans_named(otel_capture, "narration.second_person_swap")
    assert len(spans) == 1
    swap_count = dict(spans[0].attributes).get("swap_count")
    assert isinstance(swap_count, int) and swap_count >= 5, (
        f"swap_count must include the pronoun substitutions (expected >= 5: "
        f"name + verb + subject-pronoun + its verb + possessive-pronoun); "
        f"got {swap_count!r}"
    )
