import json

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.genre_stage import GenreRuleStage
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.game.projection.view import SessionGameStateView

YAML = """
rules:
  - kind: NARRATION
    visibility_tag: {}
"""


def test_visibility_tag_rule_parses():
    rules = load_rules_from_yaml_str(YAML)
    assert len(rules.rules) == 1
    assert rules.rules[0].kind == "NARRATION"


def _env(kind: str, payload: dict, seq: int = 1) -> MessageEnvelope:
    return MessageEnvelope(kind=kind, payload_json=json.dumps(payload), origin_seq=seq)


def test_excludes_when_player_not_in_visible_to():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        player_id_to_character={"p1": "c1", "p2": "c2"},
    )
    payload = {"text": "Alice sneaks.", "_visibility": {"visible_to": ["p1"]}}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p2")
    assert result.decision.include is False


def test_includes_when_player_in_visible_to():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        player_id_to_character={"p1": "c1", "p2": "c2"},
    )
    payload = {"text": "Alice sneaks.", "_visibility": {"visible_to": ["p1"]}}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p1")
    assert result.decision.include is True


def test_all_means_all():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        player_id_to_character={"p1": "c1", "p2": "c2"},
    )
    payload = {"text": "Dawn breaks.", "_visibility": {"visible_to": "all"}}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p2")
    assert result.decision.include is True


def test_missing_visibility_falls_through_to_pass_through():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        player_id_to_character={"p1": "c1"},
    )
    payload = {"text": "No viz key."}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p1")
    assert result.decision.include is True


def test_fidelity_transform_strips_visual_spans_for_blinded():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        player_id_to_character={"p1": "c1"},
    )
    payload = {
        "text": "You hear a crash.",
        "spans": [
            {"id": "s1", "kind": "visual_only", "text": "a glint of steel"},
            {"id": "s2", "kind": "audio_only", "text": "a wet thud"},
        ],
        "_visibility": {
            "visible_to": "all",
            "fidelity": {"p1": "audio_only"},
        },
    }
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p1")
    assert result.decision.include is True
    out = json.loads(result.decision.payload_json)
    span_ids = [s["id"] for s in out["spans"]]
    assert "s1" not in span_ids  # visual_only stripped
    assert "s2" in span_ids  # audio_only kept


# Story 96-1: the two per-shipping-pack sweeps that used to live here
# (``test_every_shipping_pack_projection_has_visibility_tag_rule`` and
# ``test_every_shipping_pack_projection_has_secret_note_rule``) were content
# validation wearing a server-test costume — they iterated LIVE packs and went
# red on content-only changes. The requirement (every pack routes NARRATION +
# SECRET_NOTE through visibility_tag) moved into the ``pf validate pack``
# content gate via ``validate_visibility_coverage`` — see
# ``tests/cli/validate/test_pack_validator_projection_visibility.py``.
