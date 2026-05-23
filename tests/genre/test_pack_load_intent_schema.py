"""Schema tests for ConfrontationDef.intent_verbs and .on_intent_mismatch."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import ConfrontationDef


def _minimal_def(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "type": "negotiation",
        "label": "Tense Negotiation",
        "category": "social",
        "player_metric": {"name": "leverage", "threshold": 10},
        "opponent_metric": {"name": "patience", "threshold": 10},
        "beats": [{"id": "haggle", "label": "Haggle", "kind": "push", "stat_check": "cha"}],
    }
    base.update(extra)
    return base


def test_on_intent_mismatch_defaults_to_warn() -> None:
    cdef = ConfrontationDef.model_validate(_minimal_def())
    assert cdef.on_intent_mismatch == "warn"


@pytest.mark.parametrize("value", ["warn", "soft_suggest", "reprompt"])
def test_on_intent_mismatch_accepts_three_values(value: str) -> None:
    cdef = ConfrontationDef.model_validate(_minimal_def(on_intent_mismatch=value))
    assert cdef.on_intent_mismatch == value


def test_on_intent_mismatch_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError) as exc:
        ConfrontationDef.model_validate(_minimal_def(on_intent_mismatch="ignore"))
    assert "on_intent_mismatch" in str(exc.value)


def test_intent_verbs_defaults_to_none() -> None:
    cdef = ConfrontationDef.model_validate(_minimal_def())
    assert cdef.intent_verbs is None


def test_intent_verbs_accepts_list_of_strings() -> None:
    cdef = ConfrontationDef.model_validate(
        _minimal_def(intent_verbs=["haggle", "bargain", "barter"])
    )
    assert cdef.intent_verbs == ["haggle", "bargain", "barter"]


def test_intent_verbs_rejects_non_string_elements() -> None:
    with pytest.raises(ValidationError) as exc:
        ConfrontationDef.model_validate(_minimal_def(intent_verbs=["bargain", 5]))
    assert "intent_verbs" in str(exc.value)


def test_intent_verb_set_derived_from_label_and_beats() -> None:
    cdef = ConfrontationDef.model_validate(
        {
            "type": "negotiation",
            "label": "Tense Negotiation",
            "category": "social",
            "player_metric": {"name": "leverage", "threshold": 10},
            "opponent_metric": {"name": "patience", "threshold": 10},
            "beats": [
                {"id": "haggle", "label": "Haggle the Price", "stat_check": "cha", "kind": "push"},
                {"id": "offer", "label": "Make an Offer", "stat_check": "cha", "kind": "push"},
            ],
        }
    )
    verbs = cdef.intent_verb_set
    assert "negotiation" in verbs  # 'negotiation' does not end in -ing/-ed/-s
    assert "tense" in verbs
    assert "haggle" in verbs
    assert "offer" in verbs
    assert "price" in verbs
    # Stopwords dropped.
    assert "the" not in verbs
    assert "an" not in verbs


def test_intent_verb_set_unions_optional_intent_verbs() -> None:
    cdef = ConfrontationDef.model_validate(
        {
            "type": "negotiation",
            "label": "Tense Negotiation",
            "category": "social",
            "player_metric": {"name": "leverage", "threshold": 10},
            "opponent_metric": {"name": "patience", "threshold": 10},
            "beats": [{"id": "haggle", "label": "Haggle", "stat_check": "cha", "kind": "push"}],
            "intent_verbs": ["bargain", "barter", "deal"],
        }
    )
    assert {"bargain", "barter", "deal"} <= cdef.intent_verb_set
    assert "haggle" in cdef.intent_verb_set


def test_intent_verb_set_empty_when_label_is_only_stopwords() -> None:
    cdef = ConfrontationDef.model_validate(
        {
            "type": "x",
            "label": "the a",
            "category": "social",
            "player_metric": {"name": "m", "threshold": 1},
            "opponent_metric": {"name": "m", "threshold": 1},
            "beats": [{"id": "b", "label": "to", "stat_check": "cha", "kind": "push"}],
        }
    )
    # 'x' from type is NOT included — only label + beats[].label + intent_verbs.
    assert cdef.intent_verb_set == frozenset()


def test_rules_intent_verbs_by_type_accessor() -> None:
    """RulesConfig.intent_verbs_by_type maps confrontation_type -> verb set."""
    from sidequest.genre.models.rules import RulesConfig

    rules = RulesConfig.model_validate(
        {
            "confrontations": [
                {
                    "type": "negotiation",
                    "label": "Haggle",
                    "category": "social",
                    "player_metric": {"name": "a", "threshold": 1},
                    "opponent_metric": {"name": "b", "threshold": 1},
                    "beats": [
                        {"id": "b1", "label": "Bargain", "stat_check": "cha", "kind": "push"}
                    ],
                },
                {
                    "type": "combat",
                    "label": "Fight",
                    "category": "combat",
                    "player_metric": {"name": "a", "threshold": 1},
                    "opponent_metric": {"name": "b", "threshold": 1},
                    "beats": [{"id": "b1", "label": "Strike", "stat_check": "str", "kind": "push"}],
                },
            ],
        }
    )
    mapping = rules.intent_verbs_by_type
    assert "negotiation" in mapping
    assert "combat" in mapping
    assert "haggle" in mapping["negotiation"]
    assert "fight" in mapping["combat"]
    assert "strike" in mapping["combat"]
