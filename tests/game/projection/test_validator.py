"""Rule validator — 7 semantic checks."""

from __future__ import annotations

import pytest

from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.validator import (
    ValidationError,
    validate_projection_rules,
)


def _validate(yaml_text: str) -> None:
    rules = load_rules_from_yaml_str(yaml_text)
    validate_projection_rules(rules)


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown kind"):
        _validate(
            """
            rules:
              - kind: NOT_A_REAL_KIND
                target_only:
                  field: to
            """
        )


def test_unreachable_kind_is_rejected() -> None:
    # TURN_STATUS is a real MessageType but not yet in _KIND_TO_MESSAGE_CLS.
    with pytest.raises(ValidationError, match="not filter-reachable"):
        _validate(
            """
            rules:
              - kind: TURN_STATUS
                redact_fields:
                  - field: anything
                    unless: is_gm()
                    mask: null
            """
        )


def test_unknown_field_path_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown field"):
        _validate(
            """
            rules:
              - kind: NARRATION
                redact_fields:
                  - field: nonexistent_field
                    unless: is_gm()
                    mask: null
            """
        )


def test_unknown_predicate_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown predicate"):
        _validate(
            """
            rules:
              - kind: NARRATION
                redact_fields:
                  - field: text
                    unless: not_a_real_predicate(text)
                    mask: null
            """
        )


def test_type_mismatched_mask_is_rejected() -> None:
    with pytest.raises(ValidationError, match="type-incompatible mask"):
        _validate(
            """
            rules:
              - kind: NARRATION
                redact_fields:
                  - field: text
                    unless: is_gm()
                    mask: []
            """
        )


def test_conflicting_redactions_on_same_field_rejected() -> None:
    with pytest.raises(ValidationError, match="conflicting redactions"):
        _validate(
            """
            rules:
              - kind: NARRATION
                redact_fields:
                  - field: text
                    unless: is_gm()
                    mask: "**"
              - kind: NARRATION
                redact_fields:
                  - field: text
                    unless: visible_to(text)
                    mask: "??"
            """
        )


def test_predicate_arg_not_in_payload_is_rejected() -> None:
    with pytest.raises(ValidationError, match="predicate arg"):
        _validate(
            """
            rules:
              - kind: NARRATION
                redact_fields:
                  - field: text
                    unless: visible_to(some_invented_field)
                    mask: null
            """
        )


def test_valid_rules_pass() -> None:
    _validate(
        """
        rules:
          - kind: NARRATION
            redact_fields:
              - field: text
                unless: is_gm()
                mask: null
        """
    )
