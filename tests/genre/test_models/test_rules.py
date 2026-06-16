"""Tests for rules model types."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    ResolutionMode,
    ResourceDeclaration,
    RulesConfig,
)


class TestResourceDeclaration:
    def test_valid(self) -> None:
        r = ResourceDeclaration(
            name="luck",
            label="Luck",
            min=0.0,
            max=10.0,
            starting=5.0,
            voluntary=True,
            decay_per_turn=0.0,
        )
        assert r.name == "luck"

    def test_rejects_max_less_than_min(self) -> None:
        with pytest.raises(ValidationError, match="max.*must be.*min"):
            ResourceDeclaration(
                name="bad",
                label="Bad",
                min=10.0,
                max=5.0,
                starting=7.0,
                voluntary=True,
                decay_per_turn=0.0,
            )

    def test_rejects_starting_out_of_range(self) -> None:
        with pytest.raises(ValidationError, match="starting"):
            ResourceDeclaration(
                name="bad",
                label="Bad",
                min=0.0,
                max=10.0,
                starting=15.0,
                voluntary=True,
                decay_per_turn=0.0,
            )

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ResourceDeclaration.model_validate(
                {
                    "name": "x",
                    "label": "X",
                    "min": 0,
                    "max": 10,
                    "starting": 5,
                    "voluntary": True,
                    "decay_per_turn": 0.0,
                    "bogus": True,
                }
            )


class TestMetricDef:
    def test_valid_ascending(self) -> None:
        # Two-dial schema: threshold must be > starting
        m = MetricDef(name="momentum", starting=0, threshold=10)
        assert m.name == "momentum"
        assert m.threshold == 10

    def test_rejects_threshold_not_greater_than_starting(self) -> None:
        with pytest.raises(ValidationError, match="threshold"):
            MetricDef(name="hp", starting=10, threshold=5)

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MetricDef.model_validate({"name": "hp", "starting": 0, "threshold": 10, "extra": True})


def _beat(extra: dict | None = None) -> dict:
    """Minimal valid BeatDef for the new two-dial schema."""
    b = {"id": "attack", "label": "Attack", "kind": "strike", "base": 2, "stat_check": "STR"}
    if extra:
        b.update(extra)
    return b


def _confrontation(beats: list | None = None, **kwargs: object) -> dict:
    """Minimal valid ConfrontationDef for the new two-dial schema."""
    base = {
        "type": "combat",
        "label": "Combat",
        "category": "combat",
        "player_metric": {"name": "momentum", "starting": 0, "threshold": 10},
        "opponent_metric": {"name": "momentum", "starting": 0, "threshold": 10},
        "beats": beats if beats is not None else [_beat()],
    }
    base.update(kwargs)
    return base


class TestBeatDef:
    def test_valid(self) -> None:
        b = BeatDef.model_validate(_beat())
        assert b.id == "attack"
        assert b.kind.value == "strike"

    def test_rejects_empty_id(self) -> None:
        with pytest.raises(ValidationError):
            BeatDef.model_validate(_beat({"id": ""}))

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            BeatDef.model_validate({**_beat(), "unknown_field": True})


class TestConfrontationDef:
    def test_valid(self) -> None:
        c = ConfrontationDef.model_validate(_confrontation())
        assert c.confrontation_type == "combat"

    def test_rejects_invalid_category(self) -> None:
        with pytest.raises(ValidationError, match="invalid confrontation category"):
            ConfrontationDef.model_validate(_confrontation(category="invalid"))

    def test_rejects_empty_beats(self) -> None:
        with pytest.raises(ValidationError, match="at least one beat"):
            ConfrontationDef.model_validate(_confrontation(beats=[]))

    def test_rejects_duplicate_beat_ids(self) -> None:
        beats = [_beat(), {**_beat(), "label": "Attack2"}]
        with pytest.raises(ValidationError, match="duplicate beat id"):
            ConfrontationDef.model_validate(_confrontation(beats=beats))

    def test_resolution_mode_default(self) -> None:
        c = ConfrontationDef.model_validate(_confrontation())
        assert c.resolution_mode == ResolutionMode.beat_selection


class TestBeatDefClassFilter:
    def test_beat_class_filter_defaults_none(self) -> None:
        b = BeatDef.model_validate(_beat())
        assert b.class_filter is None

    def test_beat_class_filter_accepts_nonempty_list(self) -> None:
        b = BeatDef.model_validate(_beat({"class_filter": ["Fighter"]}))
        assert b.class_filter == ["Fighter"]

    def test_beat_class_filter_rejects_empty_list(self) -> None:
        with pytest.raises(ValidationError) as exc:
            BeatDef.model_validate(_beat({"class_filter": []}))
        assert "class_filter" in str(exc.value).lower()


class TestRulesConfig:
    def test_empty_defaults(self) -> None:
        r = RulesConfig()
        assert r.tone == ""
        assert r.confrontations == []
        assert r.resources == []

    def test_roundtrip(self) -> None:
        r = RulesConfig(tone="gritty", magic_level="none", ability_score_names=["STR", "DEX"])
        data = r.model_dump()
        r2 = RulesConfig.model_validate(data)
        assert r2.tone == "gritty"
        assert r2.ability_score_names == ["STR", "DEX"]
