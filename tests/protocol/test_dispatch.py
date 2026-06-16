"""Tests for DispatchPackage types (Group B, Local DM decomposer output contract)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sidequest.protocol.dispatch import (
    CrossAction,
    DispatchPackage,
    LethalityVerdict,
    NarratorDirective,
    PlayerDispatch,
    Referent,
    SubsystemDispatch,
    VisibilityTag,
)


def test_dispatch_package_minimal_valid():
    """A package with no actions and no cross-player events is valid."""
    pkg = DispatchPackage(
        turn_id="turn-001",
        per_player=[],
        cross_player=[],
        confidence_global=1.0,
    )
    assert pkg.per_player == []


def test_dispatch_package_full_roundtrip():
    """A package containing every field type serializes and round-trips."""
    pkg = DispatchPackage(
        turn_id="turn-042",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="Let's attack him!",
                resolved=[
                    Referent(
                        token="him",
                        resolved_to="npc:goblin_2",
                        confidence=0.55,
                        alternatives=["npc:goblin_1", "npc:bandit_1"],
                        resolution_note="most recent direct combatant",
                    ),
                    Referent(
                        token="let's",
                        resolved_to=None,
                        confidence=0.0,
                        alternatives=[],
                        resolution_note="no party present",
                    ),
                ],
                dispatch=[
                    SubsystemDispatch(
                        subsystem="distinctive_detail_hint",
                        params={"target": "npc:goblin_2", "hint": "broken tooth"},
                        depends_on=[],
                        idempotency_key="idem:turn-042:alice:0",
                        visibility=VisibilityTag(
                            visible_to="all",
                            perception_fidelity={},
                            secrets_for=[],
                            redact_from_narrator_canonical=False,
                        ),
                    ),
                    SubsystemDispatch(
                        subsystem="reflect_absence",
                        params={"addressee_hint": "no party"},
                        depends_on=[],
                        idempotency_key="idem:turn-042:alice:1",
                        visibility=VisibilityTag(
                            visible_to="all",
                            perception_fidelity={},
                            secrets_for=[],
                            redact_from_narrator_canonical=False,
                        ),
                    ),
                ],
                lethality=[],
                narrator_instructions=[
                    NarratorDirective(
                        kind="must_not_narrate",
                        payload="inventing an NPC follower",
                        visibility=VisibilityTag(
                            visible_to="all",
                            perception_fidelity={},
                            secrets_for=[],
                            redact_from_narrator_canonical=False,
                        ),
                    ),
                ],
            ),
        ],
        cross_player=[],
        confidence_global=0.78,
    )
    serialized = pkg.model_dump_json()
    parsed = DispatchPackage.model_validate_json(serialized)
    assert parsed == pkg


def test_visibility_tag_defaults_are_explicit():
    """Visibility tags require explicit visible_to — no implicit fallback."""
    # 'all' is a conscious choice; model should accept it.
    tag = VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    assert tag.visible_to == "all"
    # Player-list is also accepted.
    tag2 = VisibilityTag(
        visible_to=["player:Alice"],
        perception_fidelity={"player:Alice": "full"},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    assert tag2.visible_to == ["player:Alice"]


def test_lethality_verdict_captures_witness_scope():
    """Spec §4.2 — verdict carries witness_scope for Group G consumption."""
    verdict = LethalityVerdict(
        entity="player:Alice",
        verdict="dead",
        cause="Salt Burrower mandible crush, 34 dmg, HP -8",
        reversibility="permanent",
        narrator_directive="Alice is dead. Compose a genre-true death.",
        soul_md_constraint="genre_truth:lethal_for_this_genre",
        witness_scope={
            "direct_witnesses": ["player:Alice"],
            "indirect_witnesses": ["player:Bob"],
            "unaware": ["player:Cass"],
            "perception_fidelity": {"player:Alice": "full", "player:Bob": "audio_only_muffled"},
        },
    )
    assert verdict.witness_scope["direct_witnesses"] == ["player:Alice"]


def test_cross_action_names_participants_and_witnesses():
    """Spec §5 — cross_player entries distinguish participants from witnesses."""
    ca = CrossAction(
        participants=["player:Alice", "player:Bob"],
        witnesses=["player:Alice", "player:Bob", "player:Cass"],
        dispatch=[],
    )
    assert set(ca.witnesses) >= set(ca.participants)


def test_dispatch_package_parses_from_llm_style_json():
    """The decomposer emits raw JSON; parser must accept it.

    Story 59-2 removed the legacy ``degraded`` / ``degraded_reason`` fields
    per ADR-113. Producer failure now raises ``IntentRouterFailure`` instead
    of returning a degraded shape; ``tests/agents/test_intent_router.py``
    covers the fail-loud retry semantics.
    """
    raw = json.dumps(
        {
            "turn_id": "turn-x",
            "per_player": [],
            "cross_player": [],
            "confidence_global": 0.9,
        }
    )
    pkg = DispatchPackage.model_validate_json(raw)
    assert pkg.turn_id == "turn-x"


def test_cross_action_rejects_participants_not_in_witnesses():
    """Validator: witnesses must include all participants (spec §5)."""
    with pytest.raises(ValidationError):
        CrossAction(
            participants=["player:Alice", "player:Bob"],
            witnesses=["player:Alice"],  # Bob is a participant but not a witness
            dispatch=[],
        )


def test_dispatch_package_rejects_duplicate_idempotency_keys_within_player():
    """Validator: idempotency_keys must be unique across per_player dispatches."""
    tag = VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    dup = SubsystemDispatch(
        subsystem="reflect_absence",
        params={},
        depends_on=[],
        idempotency_key="idem:same",
        visibility=tag,
    )
    with pytest.raises(ValidationError):
        DispatchPackage(
            turn_id="t",
            per_player=[
                PlayerDispatch(
                    player_id="p",
                    raw_action="",
                    resolved=[],
                    dispatch=[dup, dup],
                    lethality=[],
                    narrator_instructions=[],
                ),
            ],
            cross_player=[],
            confidence_global=1.0,
        )


def test_dispatch_package_rejects_duplicate_idempotency_keys_across_per_and_cross_player():
    """Validator: same idempotency_key in per_player and cross_player must fail (Fix 2)."""
    tag = VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )
    d_per = SubsystemDispatch(
        subsystem="reflect_absence",
        params={},
        depends_on=[],
        idempotency_key="idem:collision",
        visibility=tag,
    )
    d_cross = SubsystemDispatch(
        subsystem="npc_agency",
        params={"npc_name": "x"},
        depends_on=[],
        idempotency_key="idem:collision",
        visibility=tag,
    )
    with pytest.raises(ValidationError):
        DispatchPackage(
            turn_id="t",
            per_player=[
                PlayerDispatch(
                    player_id="p",
                    raw_action="",
                    resolved=[],
                    dispatch=[d_per],
                    lethality=[],
                    narrator_instructions=[],
                )
            ],
            cross_player=[CrossAction(participants=["p"], witnesses=["p"], dispatch=[d_cross])],
            confidence_global=1.0,
        )
