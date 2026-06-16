"""ComposedFilter — invariant stage + genre stage + default pass-through."""

from __future__ import annotations

import json
import textwrap

from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView


def _view() -> SessionGameStateView:
    return SessionGameStateView(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char", "bob": "bob_char"},
    )


def test_gm_invariant_short_circuits_genre_rules() -> None:
    rules = load_rules_from_yaml_str(
        """
        rules:
          - kind: NARRATION
            redact_fields:
              - field: text
                unless: is_self(text)
                mask: "**"
        """
    )
    filt = ComposedFilter(rules=rules)
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=1)
    dec = filt.project(envelope=env, view=_view(), player_id="gm")
    assert dec.include is True
    assert json.loads(dec.payload_json) == {"text": "hi"}


def test_unknown_kind_falls_through_to_pass_through() -> None:
    filt = ComposedFilter(rules=load_rules_from_yaml_str("rules: []"))
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"hi"}', origin_seq=2)
    dec = filt.project(envelope=env, view=_view(), player_id="alice")
    assert dec.include is True
    assert dec.payload_json == '{"text":"hi"}'


def test_genre_rule_applies_when_no_invariant_fires() -> None:
    rules = load_rules_from_yaml_str(
        textwrap.dedent(
            """
            rules:
              - kind: NARRATION
                redact_fields:
                  - field: text
                    unless: is_gm()
                    mask: "**"
            """
        )
    )
    filt = ComposedFilter(rules=rules)
    env = MessageEnvelope(kind="NARRATION", payload_json='{"text":"secret"}', origin_seq=3)
    dec = filt.project(envelope=env, view=_view(), player_id="alice")
    assert dec.include is True
    assert json.loads(dec.payload_json) == {"text": "**"}


def test_secret_note_visibility_gated_invariant_routes_to_recipient_only() -> None:
    """ADR-105 B1: end-to-end through ComposedFilter, the structural
    visibility gate excludes a non-recipient even with no genre rules
    configured (the firewall does not depend on projection.yaml).
    """
    filt = ComposedFilter(rules=load_rules_from_yaml_str("rules: []"))
    env = MessageEnvelope(
        kind="SECRET_NOTE",
        payload_json=json.dumps({"subsystem": "probe", "_visibility": {"visible_to": ["alice"]}}),
        origin_seq=4,
    )
    assert filt.project(envelope=env, view=_view(), player_id="alice").include is True
    bob = filt.project(envelope=env, view=_view(), player_id="bob")
    assert bob.include is False
    assert bob.payload_json == ""
