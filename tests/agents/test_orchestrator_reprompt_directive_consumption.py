"""Test that snapshot.next_turn_directives is consumed + cleared by prompt assembly.

Spec 2026-05-20 confrontation-intent-validator step 7.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import _consume_next_turn_directives
from sidequest.game.session import GameSnapshot


def test_consume_returns_empty_when_queue_empty() -> None:
    snap = GameSnapshot()
    assert _consume_next_turn_directives(snap) == ""
    assert snap.next_turn_directives == []


def test_consume_renders_and_clears() -> None:
    snap = GameSnapshot(
        next_turn_directives=["Open the negotiation.", "Open the duel."]
    )
    rendered = _consume_next_turn_directives(snap)
    assert "Open the negotiation." in rendered
    assert "Open the duel." in rendered
    assert snap.next_turn_directives == []  # consumed = cleared


def test_consume_idempotent_after_clear() -> None:
    snap = GameSnapshot(next_turn_directives=["x"])
    _consume_next_turn_directives(snap)
    # Second consume returns empty (queue was cleared)
    assert _consume_next_turn_directives(snap) == ""
