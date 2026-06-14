from __future__ import annotations

from types import SimpleNamespace

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot
from sidequest.server.intent_router_pass import _build_fate_summary, _build_state_summary


def _pc(name: str, skills: dict[str, int], fate_points: int = 3) -> Character:
    sheet = FateSheet(skills=skills, fate_points=fate_points)
    sheet.aspects.append(Aspect(text="Last Honest Cop in Vega", kind="high_concept"))
    return Character(
        core=CreatureCore(name=name, description="d", personality="p", fate_sheet=sheet),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


def _conflict_snapshot() -> GameSnapshot:
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[EncounterActor(name="Vance", role="lead", side="player")],
    )
    enc.situation_aspects.append(Aspect(text="Overturned Table", kind="situation", free_invokes=1))
    return GameSnapshot(
        genre_slug="pulp_noir", characters=[_pc("Vance", {"Fight": 3, "Notice": 2})], encounter=enc
    )


def _fate_pack():
    return SimpleNamespace(
        rules=SimpleNamespace(ruleset="fate", confrontations=[]), worlds=None, witnessed_acts=None
    )


def _native_pack():
    return SimpleNamespace(
        rules=SimpleNamespace(ruleset="native", confrontations=[]), worlds=None, witnessed_acts=None
    )


def test_build_fate_summary_shape():
    summary = _build_fate_summary(_conflict_snapshot())
    assert summary["skills"]["Vance"] == {"Fight": 3, "Notice": 2}
    assert summary["fate_points"]["Vance"] == 3
    assert "Last Honest Cop in Vega" in summary["character_aspects"]["Vance"]
    assert summary["scene_aspects"] == ["Overturned Table"]
    assert summary["active_conflict"] is True


def test_state_summary_carries_fate_block_for_fate_pack():
    summary = _build_state_summary(_conflict_snapshot(), pack=_fate_pack())
    assert "fate" in summary
    assert summary["fate"]["active_conflict"] is True


def test_state_summary_omits_fate_block_for_non_fate_pack():
    summary = _build_state_summary(_conflict_snapshot(), pack=_native_pack())
    assert "fate" not in summary


def test_fate_routing_rules_spliced_into_system_prompt():
    # AC3 second half: the routing rules must be DEFINED *and* spliced into the
    # static router prompt. The plan's reliance on the existing router suite
    # cannot catch a forgotten splice (the addition is purely additive), so the
    # wiring is pinned here directly.
    from sidequest.agents.intent_router import _SYSTEM_PROMPT, FATE_ROUTING_RULES

    assert FATE_ROUTING_RULES.strip()  # non-empty rules text
    assert FATE_ROUTING_RULES in _SYSTEM_PROMPT
