"""Group G Task 7 — canonical-leak audit unit tests.

Verifies the safety-net OTEL span fires with leaks_detected=0 when the
canonical prose is clean, and fires with leaks_detected>=1 when a token
from an entity flagged ``redact_from_narrator_canonical`` leaked through
structural hiding. Per SOUL.md Zork constraint, the match is
entity-token-set vs. prose — not regex on arbitrary strings.
"""

from __future__ import annotations

from sidequest.protocol.dispatch import (
    CrossAction,
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.telemetry.leak_audit import audit_canonical_prose


def _redacted(
    actor: str, params: dict, key: str = "k1", subsystem: str = "lethal_strike"
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem=subsystem,
        params=params,
        idempotency_key=key,
        confidence=1.0,
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


# ---------------------------------------------------------------------------
# Story 59-24: cross_player coverage. audit_canonical_prose historically
# collected redacted_entities from per_player ONLY; a cross_player redacted
# dispatch's target was never scanned, so its leak into prose produced a false
# leaks_detected=0. Sibling of 59-9 (which fixed the primary redactor).
# ---------------------------------------------------------------------------


def _cross(
    actor: str, params: dict, key: str = "cx1", subsystem: str = "lethal_strike"
) -> CrossAction:
    return CrossAction(
        participants=["player:Alice", "player:Bob"],
        witnesses=["player:Alice", "player:Bob"],
        dispatch=[_redacted(actor, params, key=key, subsystem=subsystem)],
    )


def test_leak_detected_when_redacted_cross_player_entity_appears():
    pkg = DispatchPackage(
        turn_id="t1",
        cross_player=[_cross("player:Alice", {"target": "guard_A"})],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Rickard the guard slumps against the crate.",
        package=pkg,
        entity_tokens_by_id={"guard_A": ["Rickard", "the guard"]},
    )
    assert result.leaks_detected >= 1
    assert "guard_A" in result.leaked_entities


def test_cross_player_redacted_counted_in_redact_tag_count_with_per_player():
    """per_player and cross_player redacted targets share the same
    redacted_entities accumulator the span's redact_tag_count reports."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="sneak",
                dispatch=[_redacted("player:Alice", {"target": "guard_A"}, key="pp1")],
            )
        ],
        cross_player=[_cross("player:Alice", {"target": "guard_B"}, key="cx1")],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="The evening wears on quietly.",
        package=pkg,
        entity_tokens_by_id={"guard_A": ["Rickard"], "guard_B": ["Mira"]},
    )
    # Both redacted targets counted; clean prose => no leaks.
    assert result.redact_tag_count == 2
    assert result.leaks_detected == 0


def test_cross_player_redacted_no_false_positive_when_prose_clean():
    pkg = DispatchPackage(
        turn_id="t1",
        cross_player=[_cross("player:Alice", {"target": "guard_A"})],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="The lanterns gutter in the empty hall.",
        package=pkg,
        entity_tokens_by_id={"guard_A": ["Rickard", "the guard"]},
    )
    assert result.leaks_detected == 0
    assert result.leaked_entities == []
    assert result.redact_tag_count == 1


# ---------------------------------------------------------------------------
# Story 59-25: multi-key entity extraction. audit_canonical_prose historically
# read the redacted entity from params["target"] ONLY (both branches). But
# subsystems key their secret entity differently: npc_agency -> params["npc_name"],
# magic_working -> params["actor"]. A redacted dispatch from those subsystems
# never entered redacted_entities, so its leak into canonical prose produced a
# false leaks_detected=0 — a silent lie-detector false negative. The fix widens
# extraction to ("target", "npc_name", "actor") in BOTH the per_player and
# cross_player branches, kept symmetric. Sibling of 59-9 / 59-24.
# ---------------------------------------------------------------------------


def test_leak_detected_npc_agency_npc_name_per_player():
    """AC1: npc_agency keys its redacted entity under params['npc_name']."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="watch the steward",
                dispatch=[
                    _redacted(
                        "player:Alice",
                        {"npc_name": "steward_A"},
                        subsystem="npc_agency",
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Aldous the steward bows and slips out the side door.",
        package=pkg,
        entity_tokens_by_id={"steward_A": ["Aldous", "the steward"]},
    )
    assert result.leaks_detected >= 1
    assert "steward_A" in result.leaked_entities


def test_leak_detected_npc_agency_npc_name_cross_player():
    """AC2: same npc_name coverage in the cross_player branch (symmetry)."""
    pkg = DispatchPackage(
        turn_id="t1",
        cross_player=[
            _cross(
                "player:Alice",
                {"npc_name": "steward_A"},
                key="cx-npc",
                subsystem="npc_agency",
            )
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Aldous the steward bows and slips out the side door.",
        package=pkg,
        entity_tokens_by_id={"steward_A": ["Aldous", "the steward"]},
    )
    assert result.leaks_detected >= 1
    assert "steward_A" in result.leaked_entities


def test_leak_detected_magic_working_actor_key():
    """AC3: magic_working keys its redacted entity under params['actor']."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="cast unseen",
                dispatch=[
                    _redacted(
                        "player:Alice",
                        {"actor": "mage_A"},
                        subsystem="magic_working",
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="Seraphine traces a sigil and is gone.",
        package=pkg,
        entity_tokens_by_id={"mage_A": ["Seraphine"]},
    )
    assert result.leaks_detected >= 1
    assert "mage_A" in result.leaked_entities
    # The entity was collected, so it also counts toward redact_tag_count.
    assert result.redact_tag_count == 1


def test_multi_key_no_false_positive_when_prose_clean():
    """AC4: npc_name/actor entities are collected (counted) across both
    branches, but clean prose yields no leaks — widening the key set must
    not manufacture false positives."""
    pkg = DispatchPackage(
        turn_id="t1",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="watch",
                dispatch=[
                    _redacted(
                        "player:Alice",
                        {"npc_name": "steward_A"},
                        key="pp-npc",
                        subsystem="npc_agency",
                    )
                ],
            )
        ],
        cross_player=[
            _cross("player:Bob", {"actor": "mage_A"}, key="cx-mage", subsystem="magic_working")
        ],
        confidence_global=1.0,
    )
    result = audit_canonical_prose(
        prose="The hall is quiet; nothing stirs.",
        package=pkg,
        entity_tokens_by_id={"steward_A": ["Aldous"], "mage_A": ["Seraphine"]},
    )
    assert result.redact_tag_count == 2
    assert result.leaks_detected == 0
    assert result.leaked_entities == []
