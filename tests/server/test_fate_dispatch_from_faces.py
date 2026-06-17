"""Story 126-7 (ADR-148): dispatch_fate_action resolves the PLAYER's proactive
action from the reported faces — never roll_4df on the player path.

Determinism is asserted on an UNCLOSED multi-PC barrier on purpose: a *solo*
encounter's barrier closes immediately and ``run_fate_exchange`` then seats the
opponent and rolls NPC defenses (``_seat_opponent_commits`` / ``_roll_defense``,
both server-side ``roll_4df`` — fate_conflict.py:259,321). With the barrier held
open (a second PC has not committed) the ONLY roll that fires is the acting
player's proactive action, so the ``roll_4df`` spy cleanly isolates the player
path. The positive control proves the spy is not vacuous: the legacy/None path
(internal callers, removed when 126-8 lands) still server-rolls.
"""

from __future__ import annotations

import random

import sidequest.game.ruleset.fate_resolution as fr
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import dispatch_fate_action


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _depleted_thug() -> Npc:
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def _open_barrier_combat():
    """Two PCs (one uncommitted) + one opponent → the barrier stays open after a
    single PC acts, so no exchange (and no NPC roll) fires."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Ally", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Hero", {"Fight": 4}), _pc("Ally", {"Fight": 2})],
        encounter=enc,
    )
    snap.npcs.append(_depleted_thug())
    return snap, enc


def _spy_roll_4df(monkeypatch):
    calls = {"n": 0}
    real = fr.roll_4df

    def spy(rng):
        calls["n"] += 1
        return real(rng)

    monkeypatch.setattr(fr, "roll_4df", spy)
    return calls


def test_player_action_uses_faces_not_roll_4df(monkeypatch):
    snap, enc = _open_barrier_combat()
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug")
    calls = _spy_roll_4df(monkeypatch)

    result = dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=random.Random(0),
        thrown_faces=(1, 1, 0, 0),
    )

    assert result.commitment_pending is True  # barrier open — no exchange ran
    assert result.action_roll is not None
    assert result.action_roll.dice == (1, 1, 0, 0)  # the reported faces ARE the roll
    assert result.action_roll.roll_total == 2
    assert result.action_roll.ladder_total == 6  # 2 + Fight 4 + 0 invoke
    assert calls["n"] == 0, "player proactive path must NEVER call roll_4df"


def test_legacy_none_path_still_server_rolls(monkeypatch):
    # Positive control: without thrown_faces (internal/legacy caller), the server
    # rolls — proving the spy actually observes roll_4df, so the 0-count above is
    # meaningful, not a dead assertion.
    snap, enc = _open_barrier_combat()
    ruleset = get_ruleset_module("fate")
    payload = FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug")
    calls = _spy_roll_4df(monkeypatch)

    result = dispatch_fate_action(
        payload=payload,
        actor_name="Hero",
        encounter=enc,
        ruleset=ruleset,
        snapshot=snap,
        rng=random.Random(0),
    )

    assert result.action_roll is not None
    assert calls["n"] >= 1, "the legacy/None path server-rolls the player's action"
