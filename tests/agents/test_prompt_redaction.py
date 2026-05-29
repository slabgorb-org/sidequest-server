"""Unit tests for sidequest.agents.prompt_redaction.

Structural hiding is Group G's primary defense: the narrator cannot leak
what it was never told. These tests exercise the `redact_dispatch_package`
helper in isolation; its integration into the narrator prompt pipeline is
covered by the orchestrator-level test in tests/agents/test_orchestrator.py.
"""

from __future__ import annotations

from typing import cast

from sidequest.agents.prompt_redaction import redact_dispatch_package
from sidequest.protocol.dispatch import (
    CrossAction,
    DispatchPackage,
    NarratorDirective,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)


def _redacted_viz(who: str) -> VisibilityTag:
    return VisibilityTag(
        visible_to=[who],
        perception_fidelity={},
        secrets_for=[who],
        redact_from_narrator_canonical=True,
    )


def _open_viz() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def test_redacted_dispatch_stripped_entirely():
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="kill guard",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "guard_A"},
                        idempotency_key="k1",
                        confidence=1.0,
                        visibility=_redacted_viz("player:Alice"),
                    ),
                    SubsystemDispatch(
                        subsystem="movement",
                        params={"to": "warehouse"},
                        idempotency_key="k2",
                        confidence=1.0,
                        visibility=_open_viz(),
                    ),
                ],
            )
        ],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)
    assert len(removed) == 1
    assert cast(SubsystemDispatch, removed[0]).idempotency_key == "k1"
    assert len(redacted.per_player[0].dispatch) == 1
    assert redacted.per_player[0].dispatch[0].idempotency_key == "k2"


def test_redacted_narrator_directive_stripped():
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="observe",
                narrator_instructions=[
                    NarratorDirective(
                        kind="must_not_narrate",
                        payload="Alice_assassination_event",
                        visibility=_redacted_viz("player:Alice"),
                    ),
                    NarratorDirective(
                        kind="must_narrate",
                        payload="The dogs bark.",
                        visibility=_open_viz(),
                    ),
                ],
            )
        ],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)
    assert len(removed) == 1
    assert len(redacted.per_player[0].narrator_instructions) == 1
    assert redacted.per_player[0].narrator_instructions[0].payload == "The dogs bark."


def test_no_redactions_is_noop():
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[PlayerDispatch(player_id="player:Alice", raw_action="look")],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)
    assert removed == []
    assert redacted == pkg


# ---------------------------------------------------------------------------
# Story 59-9: cross_player redaction. The redactor historically iterated only
# per_player; a cross_player dispatch tagged redact_from_narrator_canonical=True
# was copied through unfiltered and reached the narrator — an ADR-104/105
# perception-firewall hole. These pin the cross_player branch.
# ---------------------------------------------------------------------------


def test_redacted_cross_player_dispatch_stripped_entirely():
    pkg = DispatchPackage(
        turn_id="t1",
        cross_player=[
            CrossAction(
                participants=["player:Alice", "player:Bob"],
                witnesses=["player:Alice", "player:Bob"],
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "player:Bob"},
                        idempotency_key="cx1",
                        confidence=1.0,
                        visibility=_redacted_viz("player:Alice"),
                    ),
                    SubsystemDispatch(
                        subsystem="movement",
                        params={"to": "warehouse"},
                        idempotency_key="cx2",
                        confidence=1.0,
                        visibility=_open_viz(),
                    ),
                ],
            )
        ],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)
    assert len(removed) == 1
    assert cast(SubsystemDispatch, removed[0]).idempotency_key == "cx1"
    assert len(redacted.cross_player[0].dispatch) == 1
    assert redacted.cross_player[0].dispatch[0].idempotency_key == "cx2"


def test_mixed_per_player_and_cross_player_removals_share_accumulator():
    """Per_player and cross_player removals flow through the SAME `removed`
    accumulator the existing prompt.redaction.structural span reads."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="kill guard",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="lethal_strike",
                        params={"target": "guard_A"},
                        idempotency_key="pp1",
                        confidence=1.0,
                        visibility=_redacted_viz("player:Alice"),
                    ),
                ],
            )
        ],
        cross_player=[
            CrossAction(
                participants=["player:Alice", "player:Bob"],
                witnesses=["player:Alice", "player:Bob"],
                dispatch=[
                    SubsystemDispatch(
                        subsystem="secret_pact",
                        params={"with": "player:Bob"},
                        idempotency_key="cx1",
                        confidence=1.0,
                        visibility=_redacted_viz("player:Alice"),
                    ),
                ],
            )
        ],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)
    removed_keys = {cast(SubsystemDispatch, r).idempotency_key for r in removed}
    assert removed_keys == {"pp1", "cx1"}
    assert redacted.per_player[0].dispatch == []
    assert redacted.cross_player[0].dispatch == []


def test_cross_player_no_redactions_is_noop():
    pkg = DispatchPackage(
        turn_id="t1",
        cross_player=[
            CrossAction(
                participants=["player:Alice", "player:Bob"],
                witnesses=["player:Alice", "player:Bob"],
                dispatch=[
                    SubsystemDispatch(
                        subsystem="movement",
                        params={"to": "plaza"},
                        idempotency_key="cx1",
                        confidence=1.0,
                        visibility=_open_viz(),
                    ),
                ],
            )
        ],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)
    assert removed == []
    assert redacted == pkg
