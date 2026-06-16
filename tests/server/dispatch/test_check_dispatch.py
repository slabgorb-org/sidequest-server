"""Tests for dispatch_check — non-beat SWN skill checks (2d6) and saves (d20)."""

from unittest.mock import MagicMock

from sidequest.genre.models.rules import RulesConfig, SwnConfig
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.check import dispatch_check

_ATTR_MAP = {
    "STRENGTH": "Physique",
    "CONSTITUTION": "Resolve",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Intellect",
    "WISDOM": "Cunning",
    "CHARISMA": "Influence",
}


def _swn_pack():
    rules = MagicMock(spec=RulesConfig)
    rules.ruleset = "swn"
    rules.swn = SwnConfig(attribute_map=_ATTR_MAP)
    rules.ruleset_config.return_value = SwnConfig(attribute_map=_ATTR_MAP)
    pack = MagicMock()
    pack.rules = rules
    return pack


def test_dispatch_skill_check_success():
    # 2d6 faces [4,5]=9 + mod(DEX +1 + skill 2 = 3) = 12 >= tricky(10) -> CritSuccess (margin=2 < 3) -> Success
    # Wait: 12 > 10 and 12 < 10+3=13, so outcome = Success (not CritSuccess by margin)
    # Actually: DECISIVE_MARGIN=3, so 12 >= 10+3=13 would be CritSuccess; 12 < 13 → Success
    sent = []
    outcome = dispatch_check(
        kind="skill_check",
        attribute="DEXTERITY",
        save=None,
        skill_level=2,
        difficulty_key="tricky",
        level=1,
        label="Notice",
        character_stats={"DEXTERITY": 14},
        faces=[4, 5],
        pack=_swn_pack(),
        rolling_player_id="p1",
        character_name="Bob",
        session_id="s1",
        room_broadcast=sent.append,
    )
    assert outcome.outcome is RollOutcome.Success
    assert outcome.result.total == 12
    assert outcome.result.difficulty == 10
    assert len(sent) == 2, (
        f"dispatch_check must broadcast exactly 2 messages (DiceRequest then DiceResult); "
        f"got {len(sent)}: {[type(m).__name__ for m in sent]}"
    )


def test_dispatch_save_against_target():
    # Mental save: WISDOM->Cunning(+1) / CHARISMA->Influence(0); best=+1 added to roll.
    # Target=save_base15-(level3-1)=13. face [13] + mod(1) = 14 > 13 → Success.
    outcome = dispatch_check(
        kind="save",
        attribute=None,
        save="mental",
        skill_level=0,
        difficulty_key=None,
        level=3,
        label="Mental save",
        character_stats={"Cunning": 14, "Influence": 8},
        faces=[13],
        pack=_swn_pack(),
        rolling_player_id="p1",
        character_name="Bob",
        session_id="s1",
        room_broadcast=None,
    )
    assert outcome.outcome is RollOutcome.Success
    assert outcome.result.difficulty == 13


def test_dispatch_save_tie():
    # Mental save: WISDOM->Cunning(14→+1), CHARISMA->Influence(8→0); best = +1. Target=13 at level 3.
    # face [12] + 1 = 13 == 13 → Tie
    outcome = dispatch_check(
        kind="save",
        attribute=None,
        save="mental",
        skill_level=0,
        difficulty_key=None,
        level=3,
        label="Mental save",
        character_stats={"Cunning": 14, "Influence": 8},
        faces=[12],
        pack=_swn_pack(),
        rolling_player_id="p1",
        character_name="Bob",
        session_id="s1",
        room_broadcast=None,
    )
    assert outcome.outcome is RollOutcome.Tie
    assert outcome.result.difficulty == 13


def test_dispatch_check_unknown_kind_raises():
    """dispatch_check raises ValueError for an unrecognised kind — fails loud
    per CLAUDE.md No Silent Fallbacks."""
    import pytest

    with pytest.raises(ValueError, match="unknown kind"):
        dispatch_check(
            kind="bogus",
            attribute=None,
            save=None,
            skill_level=0,
            difficulty_key=None,
            level=1,
            label="bad kind test",
            character_stats={},
            faces=[10],
            pack=_swn_pack(),
            rolling_player_id="p1",
            character_name="Bob",
            session_id="s1",
            room_broadcast=None,
        )


def test_check_emits_otel_span(monkeypatch):
    captured = {}
    import sidequest.server.dispatch.check as check_mod

    monkeypatch.setattr(check_mod, "check_resolved_span", lambda **kw: captured.update(kw))
    dispatch_check(
        kind="save",
        attribute=None,
        save="mental",
        skill_level=0,
        difficulty_key=None,
        level=3,
        label="Mental save",
        character_stats={"Cunning": 14, "Influence": 8},
        faces=[13],
        pack=_swn_pack(),
        rolling_player_id="p1",
        character_name="Bob",
        session_id="s1",
        room_broadcast=None,
    )
    assert captured["kind"] == "save"
    assert captured["actor"] == "Bob"
    assert captured["difficulty"] == 13
