"""Tests for advancement model types."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models import (
    AdvancementEffectBeatDiscount,
    AdvancementEffectHpMaxBonus,
    AdvancementEffectHpRecovery,
    AdvancementEffectLeverageBonus,
    AdvancementEffectLoreRevealBonus,
    AdvancementTier,
    AdvancementTree,
    LoreRevealScope,
    RecoveryTriggerOnBeatSuccess,
    RecoveryTriggerOnResolution,
)


class TestRecoveryTrigger:
    def test_on_resolution(self) -> None:
        t = RecoveryTriggerOnResolution.model_validate({"kind": "on_resolution"})
        assert t.kind == "on_resolution"

    def test_on_beat_success_defaults_while_strained_false(self) -> None:
        t = RecoveryTriggerOnBeatSuccess.model_validate(
            {
                "kind": "on_beat_success",
                "beat_id": "strike",
                "amount": 1,
            }
        )
        assert t.while_strained is False

    def test_extra_forbidden_on_resolution(self) -> None:
        with pytest.raises(ValidationError):
            RecoveryTriggerOnResolution.model_validate({"kind": "on_resolution", "extra": True})


class TestAdvancementEffect:
    def test_edge_max_bonus(self) -> None:
        e = AdvancementEffectHpMaxBonus.model_validate({"type": "edge_max_bonus", "amount": 5})
        assert e.amount == 5

    def test_edge_recovery(self) -> None:
        e = AdvancementEffectHpRecovery.model_validate(
            {
                "type": "edge_recovery",
                "trigger": {"kind": "on_resolution"},
                "amount": 3,
            }
        )
        assert e.amount == 3

    def test_beat_discount(self) -> None:
        e = AdvancementEffectBeatDiscount.model_validate(
            {
                "type": "beat_discount",
                "beat_id": "strike",
                "edge_delta_mod": -1,
            }
        )
        assert e.edge_delta_mod == -1
        assert e.resource_mod is None

    def test_leverage_bonus(self) -> None:
        e = AdvancementEffectLeverageBonus.model_validate(
            {
                "type": "leverage_bonus",
                "beat_id": "strike",
                "target_edge_delta_mod": 2,
            }
        )
        assert e.target_edge_delta_mod == 2

    def test_lore_reveal_bonus(self) -> None:
        e = AdvancementEffectLoreRevealBonus.model_validate(
            {
                "type": "lore_reveal_bonus",
                "scope": "encounter_resolution",
            }
        )
        assert e.scope == LoreRevealScope.encounter_resolution


class TestAdvancementTier:
    def test_valid(self) -> None:
        t = AdvancementTier(
            id="iron_track_1",
            required_milestone="iron_track",
            effects=[],
        )
        assert t.id == "iron_track_1"

    def test_rejects_blank_id(self) -> None:
        with pytest.raises(ValidationError, match="must not be blank"):
            AdvancementTier(id="  ", required_milestone="iron_track", effects=[])

    def test_rejects_blank_milestone(self) -> None:
        with pytest.raises(ValidationError, match="must not be blank"):
            AdvancementTier(id="iron_1", required_milestone="", effects=[])

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            AdvancementTier.model_validate(
                {
                    "id": "t1",
                    "required_milestone": "m1",
                    "effects": [],
                    "bogus": True,
                }
            )

    def test_roundtrip(self) -> None:
        t = AdvancementTier(id="t1", required_milestone="m1", class_gates=["Fighter"])
        data = t.model_dump()
        t2 = AdvancementTier.model_validate(data)
        assert t2.id == "t1"
        assert t2.class_gates == ["Fighter"]


class TestAdvancementTree:
    def test_empty_tree(self) -> None:
        tree = AdvancementTree()
        assert tree.tiers == []

    def test_with_tiers(self) -> None:
        tree = AdvancementTree(
            tiers=[
                AdvancementTier(id="t1", required_milestone="m1"),
            ]
        )
        assert len(tree.tiers) == 1

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            AdvancementTree.model_validate({"tiers": [], "extra": True})
