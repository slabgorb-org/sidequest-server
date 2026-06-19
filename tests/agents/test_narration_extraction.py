"""Tests for BeatSelection and NpcMention extraction and validation.

Port tests of orchestrator.rs::BeatSelection and orchestrator.rs::NpcMention
validation. Dual-track momentum (spec §Outcome declaration, §Side declaration).
"""

import pytest

from sidequest.agents.orchestrator import (
    BeatSelection,
    NpcMention,
    extract_structured_from_response,
)
from sidequest.protocol.dice import RollOutcome


def test_beat_selection_outcome_required():
    """BeatSelection.from_dict parses outcome to RollOutcome enum."""
    sel = BeatSelection.from_dict(
        {
            "actor": "Sam",
            "beat_id": "attack",
            "outcome": "Success",
        }
    )
    assert sel.outcome is RollOutcome.Success


def test_beat_selection_invalid_outcome_raises():
    """BeatSelection.from_dict raises ValueError + emits OTEL span on invalid outcome."""
    with pytest.raises(ValueError, match="declared_tier"):
        BeatSelection.from_dict(
            {
                "actor": "Sam",
                "beat_id": "attack",
                "outcome": "Wibble",
            }
        )


def test_beat_selection_missing_outcome_defaults_to_success():
    """Per spec §Outcome declaration: missing outcome on free-text turns defaults to Success."""
    sel = BeatSelection.from_dict({"actor": "Sam", "beat_id": "attack"})
    assert sel.outcome is RollOutcome.Success


def test_npc_mention_side_required():
    """NpcMention.from_value parses side field."""
    npc = NpcMention.from_value({"name": "Promo", "side": "opponent", "role": "hostile"})
    assert npc.side == "opponent"


def test_npc_mention_invalid_side_raises():
    """NpcMention.from_value raises ValueError + emits OTEL span on invalid side."""
    with pytest.raises(ValueError, match="declared_side"):
        NpcMention.from_value({"name": "??", "side": "enemy"})


def test_npc_mention_bare_string_default_side_neutral():
    """NpcMention.from_value accepts bare string and defaults side to neutral."""
    npc = NpcMention.from_value("Random Bystander")
    assert npc.side == "neutral"


def test_narration_result_parses_status_changes():
    """extract_structured_from_response picks up status_changes from game_patch."""
    raw = (
        "**The Arena**\n\n"
        "Sam ducks the swing.\n\n"
        "```game_patch\n"
        "{\n"
        '  "beat_selections": [{"actor": "Sam", "beat_id": "defend", "outcome": "Success"}],\n'
        '  "npcs_present": [{"name": "Promo", "side": "opponent", "role": "hostile"}],\n'
        '  "status_changes": [{"actor": "Sam", "status": {"text": "Bruised Ribs", "severity": "Wound"}}]\n'
        "}\n"
        "```\n"
    )
    result = extract_structured_from_response(raw)
    assert result["beat_selections"][0]["outcome"] == "Success"
    # Story 151-5 (ADR-150 step 4, cutover II): npcs_present is retired from the
    # game_patch (extractor-sourced now) — it no longer surfaces here even when the
    # narrator emits it. status_changes (tool-owned) and beat_selections are
    # unaffected.
    assert result["npcs_present"] == []
    assert result["status_changes"] == [
        {"actor": "Sam", "status": {"text": "Bruised Ribs", "severity": "Wound"}},
    ]
