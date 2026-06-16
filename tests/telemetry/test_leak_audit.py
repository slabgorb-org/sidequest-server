"""Group G Task 7 — canonical-leak audit unit tests.

Verifies the safety-net OTEL span fires with leaks_detected=0 when the
canonical prose is clean, and fires with leaks_detected>=1 when a token
from an entity flagged ``redact_from_narrator_canonical`` leaked through
structural hiding. Per SOUL.md Zork constraint, the match is
entity-token-set vs. prose — not regex on arbitrary strings.
"""

from __future__ import annotations

from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.telemetry.leak_audit import audit_canonical_prose


def _redacted(actor: str, params: dict) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="lethal_strike",
        params=params,
        idempotency_key="k1",
        visibility=VisibilityTag(
            visible_to=[actor],
            perception_fidelity={},
            secrets_for=[actor],
            redact_from_narrator_canonical=True,
        ),
    )


def test_zero_leaks_when_prose_clean():
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="sneak",
                dispatch=[_redacted("player:Alice", {"target": "guard_A"})],
            )
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="The evening wears on at the inn.",
        package=pkg,
        entity_tokens_by_id={"guard_A": ["Rickard", "the guard"]},
    )
    assert result.leaks_detected == 0
    assert result.leaked_entities == []


def test_leak_detected_when_redacted_entity_appears():
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="sneak",
                dispatch=[_redacted("player:Alice", {"target": "guard_A"})],
            )
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Rickard the guard slumps against the crate.",
        package=pkg,
        entity_tokens_by_id={"guard_A": ["Rickard", "the guard"]},
    )
    assert result.leaks_detected >= 1
    assert "guard_A" in result.leaked_entities


def test_no_redacted_entries_means_no_audit_work():
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[PlayerDispatch(player_id="player:Alice", raw_action="look")],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Anything at all.",
        package=pkg,
        entity_tokens_by_id={},
    )
    assert result.leaks_detected == 0
    assert result.redact_tag_count == 0
