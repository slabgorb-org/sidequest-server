from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset.fate_projection import build_fate_projection
from sidequest.game.session import GameSnapshot
from sidequest.server.intent_router_pass import _build_fate_summary, _build_state_summary


def _pc(name: str, skills: dict[str, int], fate_points: int = 3) -> Character:
    sheet = FateSheet(skills=skills, fate_points=fate_points)
    sheet.aspects.append(Aspect(text="Last Honest Cop in Vega", kind="high_concept"))
    return Character(
        core=CreatureCore(name=name, description="d", personality="p", fate_sheet=sheet),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


def _conflict_snapshot() -> GameSnapshot:
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[EncounterActor(name="Vance", role="lead", side="player")],
    )
    enc.situation_aspects.append(Aspect(text="Overturned Table", kind="situation", free_invokes=1))
    return GameSnapshot(
        genre_slug="pulp_noir", characters=[_pc("Vance", {"Fight": 3, "Notice": 2})], encounter=enc
    )


def _fate_pack():
    return SimpleNamespace(
        rules=SimpleNamespace(ruleset="fate", confrontations=[]), worlds=None, witnessed_acts=None
    )


def _dial_pack():
    return SimpleNamespace(
        rules=SimpleNamespace(ruleset="dial", confrontations=[]), worlds=None, witnessed_acts=None
    )


def test_build_fate_summary_shape():
    summary = _build_fate_summary(_conflict_snapshot())
    assert summary["skills"]["Vance"] == {"Fight": 3, "Notice": 2}
    assert summary["fate_points"]["Vance"] == 3
    assert "Last Honest Cop in Vega" in summary["character_aspects"]["Vance"]
    assert summary["scene_aspects"] == ["Overturned Table"]
    assert summary["active_conflict"] is True


def test_state_summary_carries_fate_block_for_fate_pack():
    summary = _build_state_summary(_conflict_snapshot(), pack=_fate_pack())
    assert "fate" in summary
    assert summary["fate"]["active_conflict"] is True


def test_state_summary_omits_fate_block_for_non_fate_pack():
    summary = _build_state_summary(_conflict_snapshot(), pack=_dial_pack())
    assert "fate" not in summary


def test_fate_routing_rules_spliced_into_system_prompt():
    # AC3 second half: the routing rules must be DEFINED *and* spliced into the
    # static router prompt. The plan's reliance on the existing router suite
    # cannot catch a forgotten splice (the addition is purely additive), so the
    # wiring is pinned here directly.
    from sidequest.agents.intent_router import _SYSTEM_PROMPT, FATE_ROUTING_RULES

    assert FATE_ROUTING_RULES.strip()  # non-empty rules text
    assert FATE_ROUTING_RULES in _SYSTEM_PROMPT


# ===========================================================================
# Story 126-10 — the ROUTER's Fate projection must be a TRIMMED variant of the
# full narrator projection.
#
# F2b made the narrator and the router share ONE projector
# (``build_fate_projection`` — one source of truth, no drift). But the router
# call site (``intent_router_pass.py``) ships the *same fat projection* (PC
# skills + ALL live aspects) into the Haiku router's state_summary. On Fate
# worlds that bloats the structured router prompt enough that the single
# classification call intermittently can't finalize inside the turn budget
# (the original symptom was at the ``max_turns=2`` floor — ``2`` is the
# mandatory FLOOR; the structured-output choke point now passes ``max_turns=4``
# for headroom, 2026-06-19 — but trimming the fat projection is the right fix
# regardless) — ``intent_router_pass`` spikes 37-81s vs the ~4-6s
# non-Fate baseline (annees_folles, ~162s/turn).
#
# The lever (AC2): the router needs far less than the narrator — the PC skills
# to classify a freeform action into one of the four Fate actions, NOT every
# live aspect. These tests pin the contract WITHOUT dictating exactly which
# aspects survive (the empirical trim is the Dev's call, validated on
# annees_folles turn_telemetry, AC4):
#
#   * the router block carries strictly FEWER live aspects than the narrator's
#     full projection, and is strictly smaller serialized (prompt-cost proxy);
#   * it KEEPS the routing essentials — per-PC skills + the active_conflict
#     flag — so the trim never breaks Fate routing accuracy (AC2);
#   * the NARRATOR's projection is unchanged (anti-confabulation, ADR-144 F2b);
#   * a GM-panel OTEL span records the trim engaged (OTEL Observability
#     Principle + AC1: GM-panel evidence the lever fired);
#   * the trim is wired into the production pre-narrator pass, not just the
#     unit-level builder.
# ===========================================================================

# Distinct, substring-searchable "live aspect" texts planted across the fat
# snapshot. Plain prose (no punctuation that ``sanitize_player_text`` might
# transform) so a substring search over the serialized block is exact. The full
# projection carries every one; the trimmed router block must carry fewer.
_PLANTED_ASPECTS = (
    "Last Honest Cop In The RainSlick Vega Sprawl",
    "I Owe The Wrong People A Very Large Favor",
    "Quick With A Quip Quicker With A Pistol",
    "Haunted By The Marlowe Case",
    "Never Met A Lock I Could Not Pick",
    "A Face The Marks Always Trust",
    "Overturned Mahogany Card Table",
    "Gunsmoke Hanging In The Chandelier Light",
    "Spilled Bourbon Slicking The Parquet",
)


def _pc_with_aspects(name: str, skills: dict[str, int], aspect_texts: list[str]) -> Character:
    sheet = FateSheet(skills=skills, fate_points=3)
    for i, text in enumerate(aspect_texts):
        kind = "high_concept" if i == 0 else ("trouble" if i == 1 else "character")
        sheet.aspects.append(Aspect(text=text, kind=kind))
    return Character(
        core=CreatureCore(name=name, description="d", personality="p", fate_sheet=sheet),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


def _fat_conflict_snapshot() -> GameSnapshot:
    """A Fate snapshot heavy with live aspects: two PCs each carrying three
    character aspects, plus an active conflict with three situation aspects —
    all nine ``_PLANTED_ASPECTS``. The full projection carries every one; the
    trimmed router block must carry strictly fewer."""
    pcs = [
        _pc_with_aspects(
            "Vance", {"Fight": 3, "Notice": 2, "Contacts": 2}, list(_PLANTED_ASPECTS[0:3])
        ),
        _pc_with_aspects("Mireille", {"Deceive": 3, "Burglary": 2}, list(_PLANTED_ASPECTS[3:6])),
    ]
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[EncounterActor(name="Vance", role="lead", side="player")],
    )
    for text in _PLANTED_ASPECTS[6:9]:
        enc.situation_aspects.append(Aspect(text=text, kind="situation", free_invokes=1))
    return GameSnapshot(genre_slug="pulp_noir", characters=pcs, encounter=enc)


def _planted_aspects_in(block: object) -> int:
    """Count how many planted live-aspect texts survive in a projection block."""
    blob = json.dumps(block, sort_keys=True)
    return sum(1 for text in _PLANTED_ASPECTS if text in blob)


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
# AC-2: the router block trims the live aspects (the bloat lever)
# ---------------------------------------------------------------------------


def test_router_fate_block_trims_live_aspects():
    """The router's Fate block carries strictly fewer live aspects — and is
    strictly smaller serialized — than the narrator's full projection. RED
    today: the router ships ``build_fate_projection`` verbatim, so the two are
    identical."""
    snap = _fat_conflict_snapshot()
    full = build_fate_projection(snap)  # the narrator's exact call (session_helpers.py)
    router_block = _build_state_summary(snap, pack=_fate_pack())["fate"]

    full_count = _planted_aspects_in(full)
    # Guard the fixture: the full projection really does carry every live aspect.
    assert full_count == len(_PLANTED_ASPECTS)

    # The lever: the bloated live-aspect dump is trimmed out of the router prompt.
    assert _planted_aspects_in(router_block) < full_count
    # Prompt-cost proxy: the serialized router block is strictly smaller.
    assert len(json.dumps(router_block, sort_keys=True)) < len(json.dumps(full, sort_keys=True))


# ---------------------------------------------------------------------------
# AC-2 (don't over-trim): the routing-critical signal survives
# ---------------------------------------------------------------------------


def test_router_fate_block_preserves_routing_essentials():
    """The trim must not break Fate routing accuracy: the router still needs
    each PC's skills (to classify a freeform action into one of the four Fate
    actions) and whether a conflict is live (``dispatch_fate_action``'s
    in-conflict scope)."""
    snap = _fat_conflict_snapshot()
    router_block = _build_state_summary(snap, pack=_fate_pack())["fate"]

    assert router_block["skills"]["Vance"] == {"Fight": 3, "Notice": 2, "Contacts": 2}
    assert router_block["skills"]["Mireille"] == {"Deceive": 3, "Burglary": 2}
    assert router_block["active_conflict"] is True


# ---------------------------------------------------------------------------
# Regression: the NARRATOR's projection must NOT be starved by the router trim
# ---------------------------------------------------------------------------


def test_narrator_fate_projection_unchanged_by_router_trim():
    """``build_fate_projection(snapshot)`` with no router scope is the EXACT
    call the narrator makes (session_helpers.py). The router trim must not
    regress the narrator's anti-confabulation Fate section — every live aspect
    stays in the narrator's projection (ADR-144 F2b: one source of truth, the
    narrator keeps the full vocabulary)."""
    snap = _fat_conflict_snapshot()
    full = build_fate_projection(snap)

    assert _planted_aspects_in(full) == len(_PLANTED_ASPECTS)
    assert _PLANTED_ASPECTS[0] in full["character_aspects"]["Vance"]
    assert _PLANTED_ASPECTS[6] in full["scene_aspects"]


# ---------------------------------------------------------------------------
# AC-1: GM-panel OTEL evidence that the trim engaged (the lie detector)
# ---------------------------------------------------------------------------


def test_fate_vocabulary_span_fires_with_trim_evidence(otel_capture):
    """Building the router summary on a Fate pack fires
    ``intent_router.fate_vocabulary`` once, carrying before/after bytes that
    prove the trim engaged plus the routing-critical skill count — the GM-panel
    evidence the lever fired, not just that routing looked fine. RED today: no
    such span exists."""
    snap = _fat_conflict_snapshot()

    _build_state_summary(snap, pack=_fate_pack())

    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.fate_vocabulary"
    ]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["bytes_after"] < attrs["bytes_before"]
    assert attrs["skill_count"] >= 1
    assert attrs["genre_slug"] == "pulp_noir"


def test_no_fate_vocabulary_span_for_non_fate_pack(otel_capture):
    """A non-Fate pack carries no Fate block and fires no Fate-vocabulary span
    (the same conditional-vocab discipline as confrontation_types)."""
    snap = _fat_conflict_snapshot()

    summary = _build_state_summary(snap, pack=_dial_pack())

    assert "fate" not in summary
    spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "intent_router.fate_vocabulary"
    ]
    assert spans == []


# ---------------------------------------------------------------------------
# Wiring: the production pre-narrator pass ships the TRIMMED block to decompose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_narrator_pass_ships_trimmed_fate_block():
    """End-to-end through ``execute_intent_router_pre_narrator_pass``: the
    state_summary handed to ``decompose`` carries the trimmed Fate block (fewer
    live aspects than the full projection) — proving the trim is wired into the
    production router pass, not just the unit-level builder."""
    from unittest.mock import AsyncMock

    from sidequest.protocol.dispatch import DispatchPackage
    from sidequest.server.intent_router_pass import (
        execute_intent_router_pre_narrator_pass,
    )

    snap = _fat_conflict_snapshot()
    full = build_fate_projection(snap)

    empty_package = DispatchPackage(
        turn_id="test-turn", per_player=[], cross_player=[], confidence_global=0.8
    )
    mock_router = AsyncMock()
    mock_router.decompose = AsyncMock(return_value=empty_package)

    await execute_intent_router_pre_narrator_pass(
        intent_router=mock_router,
        snapshot=snap,
        pack=_fate_pack(),
        action="I flip the table and draw on him",
        player_name="Vance",
    )

    mock_router.decompose.assert_called_once()
    shipped = mock_router.decompose.call_args[1]["state_summary"]
    assert "fate" in shipped
    assert _planted_aspects_in(shipped["fate"]) < _planted_aspects_in(full)
