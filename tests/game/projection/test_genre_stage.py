"""GenreRuleStage — applies genre-configured rules."""

from __future__ import annotations

import json
import textwrap

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.genre_stage import GenreRuleStage
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView


def _stage(yaml_text: str) -> GenreRuleStage:
    return GenreRuleStage(load_rules_from_yaml_str(yaml_text))


def _view() -> SessionGameStateView:
    return SessionGameStateView(
        player_id_to_character={"alice": "alice_char", "bob": "bob_char"},
    )


def test_no_rules_passes_through() -> None:
    stage = _stage("rules: []")
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=1)
    result = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert result.decision.include is True
    assert result.decision.payload_json == '{"text":"hi"}'
    assert result.matched_rule_index is None


def test_target_only_omits_non_recipients() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: NARRATION
            target_only:
              field: text
        """
    )
    stage = _stage(yaml)
    payload = json.dumps({"text": "alice"})
    env = MessageEnvelope(kind="NARRATION", payload_json=payload, origin_seq=2)

    out_alice = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert out_alice.decision.include is True

    out_bob = stage.evaluate(envelope=env, view=_view(), player_id="bob")
    assert out_bob.decision.include is False
    # Drop attributed to the target_only rule at index 0.
    assert out_bob.matched_rule_index == 0


def test_include_if_dispatches_on_predicate() -> None:
    """include_if both omits (predicate false) and includes (predicate
    true). Uses is_self(text) against a real field so the predicate
    actually evaluates — not a degenerate always-false no-arg call."""
    yaml = textwrap.dedent(
        """
        rules:
          - kind: NARRATION
            include_if: is_self(text)
        """
    )
    stage = _stage(yaml)
    # Predicate false — text != viewer alice's character ("alice_char") → omit.
    env_omit = MessageEnvelope(kind="NARRATION", payload_json='{"text":"bob_char"}', origin_seq=3)
    omit = stage.evaluate(envelope=env_omit, view=_view(), player_id="alice")
    assert omit.decision.include is False
    assert omit.matched_rule_index == 0
    # Predicate true — text == viewer alice's character → include.
    env_keep = MessageEnvelope(kind="NARRATION", payload_json='{"text":"alice_char"}', origin_seq=4)
    keep = stage.evaluate(envelope=env_keep, view=_view(), player_id="alice")
    assert keep.decision.include is True


def test_redact_fields_masks_unless_predicate_holds() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: NARRATION
            redact_fields:
              - field: text
                unless: is_self(text)
                mask: "**"
        """
    )
    stage = _stage(yaml)
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"secret"}', origin_seq=4)
    result = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert result.decision.include is True
    assert json.loads(result.decision.payload_json) == {"text": "**"}
    # Mask fired — attributed to the redact rule at index 0.
    assert result.matched_rule_index == 0


def test_redact_fields_leaves_unmasked_when_predicate_holds() -> None:
    yaml = textwrap.dedent(
        """
        rules:
          - kind: NARRATION
            redact_fields:
              - field: text
                unless: is_self(text)
                mask: "**"
        """
    )
    stage = _stage(yaml)
    # text == the viewer's own character id => is_self(text) holds => unmasked.
    env = MessageEnvelope(
        kind="NARRATION", payload_json='{"text":"alice_char"}', origin_seq=5
    )
    result = stage.evaluate(envelope=env, view=_view(), player_id="alice")
    assert json.loads(result.decision.payload_json) == {"text": "alice_char"}
    # No mask applied (predicate held) — no matched rule.
    assert result.matched_rule_index is None
