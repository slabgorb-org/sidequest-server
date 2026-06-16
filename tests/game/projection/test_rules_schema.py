"""Rule schema parsing (structural — semantic validation is Task 10)."""

from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from sidequest.game.projection.rules import (
    IncludeIfRule,
    ProjectionRules,
    RedactFieldsRule,
    RedactSpec,
    TargetOnlyRule,
    load_rules_from_yaml_str,
)


def test_parse_target_only_rule() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: DICE_RESULT
            target_only:
              field: to
        """
    )
    rules = load_rules_from_yaml_str(yaml)
    assert isinstance(rules, ProjectionRules)
    assert len(rules.rules) == 1
    r = rules.rules[0]
    assert isinstance(r, TargetOnlyRule)
    assert r.kind == "DICE_RESULT"
    assert r.target_only.field == "to"


def test_parse_redact_fields_rule() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: STATE_UPDATE
            redact_fields:
              - field: target.hp
                unless: visible_to(target)
                mask: "??"
              - field: target.conditions
                unless: visible_to(target)
                mask: []
        """
    )
    rules = load_rules_from_yaml_str(yaml)
    r = rules.rules[0]
    assert isinstance(r, RedactFieldsRule)
    assert len(r.redact_fields) == 2
    first: RedactSpec = r.redact_fields[0]
    assert first.field == "target.hp"
    assert first.unless.predicate == "visible_to"
    assert first.unless.arg == "target"
    assert first.mask == "??"


def test_parse_include_if_rule() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: ACTION_REVEAL
            include_if: in_same_party(revealer)
        """
    )
    rules = load_rules_from_yaml_str(yaml)
    r = rules.rules[0]
    assert isinstance(r, IncludeIfRule)
    assert r.include_if.predicate == "in_same_party"
    assert r.include_if.arg == "revealer"


def test_rejects_rule_with_both_target_only_and_redact_fields() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: STATE_UPDATE
            target_only:
              field: to
            redact_fields:
              - field: hp
                unless: is_gm()
                mask: "??"
        """
    )
    with pytest.raises(ValidationError):
        load_rules_from_yaml_str(yaml)


def test_predicate_with_no_args_parses() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: STATE_UPDATE
            redact_fields:
              - field: enemy.intent
                unless: is_gm()
                mask: null
        """
    )
    rules = load_rules_from_yaml_str(yaml)
    r = rules.rules[0]
    assert isinstance(r, RedactFieldsRule)
    assert r.redact_fields[0].unless.predicate == "is_gm"
    assert r.redact_fields[0].unless.arg is None


def test_empty_rules_list_is_valid() -> None:
    rules = load_rules_from_yaml_str("rules: []")
    assert rules.rules == []
