import json

import pytest

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
        gm_player_id=None,
        player_id_to_character={"p1": "c1", "p2": "c2"},
    )
    payload = {"text": "Alice sneaks.", "_visibility": {"visible_to": ["p1"]}}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p2")
    assert result.decision.include is False


def test_includes_when_player_in_visible_to():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        gm_player_id=None,
        player_id_to_character={"p1": "c1", "p2": "c2"},
    )
    payload = {"text": "Alice sneaks.", "_visibility": {"visible_to": ["p1"]}}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p1")
    assert result.decision.include is True


def test_all_means_all():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        gm_player_id=None,
        player_id_to_character={"p1": "c1", "p2": "c2"},
    )
    payload = {"text": "Dawn breaks.", "_visibility": {"visible_to": "all"}}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p2")
    assert result.decision.include is True


def test_missing_visibility_falls_through_to_pass_through():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        gm_player_id=None,
        player_id_to_character={"p1": "c1"},
    )
    payload = {"text": "No viz key."}
    result = stage.evaluate(envelope=_env("NARRATION", payload), view=view, player_id="p1")
    assert result.decision.include is True


def test_fidelity_transform_strips_visual_spans_for_blinded():
    stage = GenreRuleStage(load_rules_from_yaml_str(YAML))
    view = SessionGameStateView(
        gm_player_id=None,
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


@pytest.mark.parametrize(
    "pack",
    [
        "caverns_and_claudes",
        "elemental_harmony",
        "heavy_metal",
        "mutant_wasteland",
        "space_opera",
        "spaghetti_western",
    ],
)
def test_every_shipping_pack_projection_has_visibility_tag_rule(pack):
    from sidequest.game.projection.rules import (
        VisibilityTagRule,
        load_rules_from_yaml_path,
    )
    from tests._helpers.genre_paths import find_pack_path

    path = find_pack_path(pack) / "projection.yaml"
    assert path.exists(), f"missing: {path}"
    rules = load_rules_from_yaml_path(path)
    narration_rules = [r for r in rules.rules if r.kind == "NARRATION"]
    assert any(isinstance(r, VisibilityTagRule) for r in narration_rules), (
        f"{pack}/projection.yaml must have a visibility_tag rule for NARRATION"
    )


@pytest.mark.parametrize(
    "pack",
    [
        "caverns_and_claudes",
        "elemental_harmony",
        "heavy_metal",
        "mutant_wasteland",
        "space_opera",
        "spaghetti_western",
    ],
)
def test_every_shipping_pack_projection_has_secret_note_rule(pack):
    """Group G Task 6 — every pack must route SECRET_NOTE through visibility_tag.

    SECRET_NOTE carries per-recipient dispatches redacted from the narrator
    prompt (Task 5). Without a visibility_tag rule for the kind, the
    ProjectionFilter would pass-through — defeating the whole structural-
    hiding pair. The rule has the same shape as the NARRATION one.
    """
    from sidequest.game.projection.rules import (
        VisibilityTagRule,
        load_rules_from_yaml_path,
    )
    from tests._helpers.genre_paths import find_pack_path

    path = find_pack_path(pack) / "projection.yaml"
    assert path.exists(), f"missing: {path}"
    rules = load_rules_from_yaml_path(path)
    secret_rules = [r for r in rules.rules if r.kind == "SECRET_NOTE"]
    assert any(isinstance(r, VisibilityTagRule) for r in secret_rules), (
        f"{pack}/projection.yaml must have a visibility_tag rule for SECRET_NOTE"
    )
