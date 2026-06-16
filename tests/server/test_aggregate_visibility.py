"""Tests for aggregate_visibility — Group G Task 4.

Unions dispatch-level VisibilityTag.visible_to into the canonical NARRATION
payload's `_visibility` sidecar. Redacted tags are excluded — they route via
SECRET_NOTE (Task 6, not yet landed).
"""

from __future__ import annotations

from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.server.session_handler import aggregate_visibility


def _viz(visible_to, *, redact: bool = False) -> VisibilityTag:
    return VisibilityTag(
        visible_to=visible_to,
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=redact,
    )


def _d(key: str, visible_to, *, redact: bool = False) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="stealth_roll_check",
        params={},
        idempotency_key=key,
        visibility=_viz(visible_to, redact=redact),
    )


def test_empty_package_aggregates_to_empty_list():
    pkg = DispatchPackage(turn_id="t", per_player=[], confidence_global=1.0)
    assert aggregate_visibility(pkg) == {"visible_to": [], "fidelity": {}}


def test_union_of_visible_to_lists():
    pkg = DispatchPackage(
        turn_id="t",
        per_player=[
            PlayerDispatch(
                player_id="p:A",
                raw_action="",
                dispatch=[_d("k1", ["p:A"]), _d("k2", ["p:B"])],
            )
        ],
        confidence_global=1.0,
    )
    result = aggregate_visibility(pkg)
    assert sorted(result["visible_to"]) == ["p:A", "p:B"]


def test_all_collapses_union():
    pkg = DispatchPackage(
        turn_id="t",
        per_player=[
            PlayerDispatch(
                player_id="p:A",
                raw_action="",
                dispatch=[_d("k1", ["p:A"]), _d("k2", "all")],
            )
        ],
        confidence_global=1.0,
    )
    assert aggregate_visibility(pkg)["visible_to"] == "all"


def test_redacted_tags_excluded_from_aggregation():
    pkg = DispatchPackage(
        turn_id="t",
        per_player=[
            PlayerDispatch(
                player_id="p:A",
                raw_action="",
                dispatch=[_d("k1", ["p:A"], redact=True), _d("k2", ["p:B"])],
            )
        ],
        confidence_global=1.0,
    )
    assert aggregate_visibility(pkg)["visible_to"] == ["p:B"]
