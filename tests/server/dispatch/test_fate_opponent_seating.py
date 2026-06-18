"""ADR-144 F2d INTEGRATION — opponent attacks are seated at exchange open.

The keystone wiring: ``run_fate_exchange`` now seats deterministic opponent
commits (``decide_opponent_action`` + ``seal_fate_commit``) BEFORE the committed
span fires, so the existing reactive walk lands opponent stress on a PC. These
tests drive the REAL registered ``FateRulesetModule`` through ``dispatch_fate_action``
with a real ``InMemorySpanExporter`` — no source-text assertions.
"""

from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import (
    _seat_opponent_commits,
    dispatch_fate_action,
    run_fate_exchange,
    seal_fate_commit,
)
from tests._helpers.fate_fixtures import resolve_parked_defenses


def _fate_module() -> FateRulesetModule:
    module = get_ruleset_module("fate")
    assert isinstance(module, FateRulesetModule)
    return module


def _actor(encounter: StructuredEncounter, name: str) -> EncounterActor:
    actor = encounter.find_actor(name)
    assert actor is not None
    return actor


#: A seed that makes the opponent's seated attack land >0 shifts on the PC.
#: (Picked by a scratch sweep; see the module docstring / story report.)
_HIT_SEED = 2

#: A seed where BOTH seated opponents' attacks land on the lone PC (Goon ladder 5,
#: Bruiser ladder 4 vs Hero's Athletics-0 defense). Picked by a scratch sweep over
#: the real dispatch path; yields exactly two ``fate.stress.applied`` spans on Hero
#: (one absorbed box per hit) with the PC surviving both.
_DOUBLE_HIT_SEED = 0


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _npc(name: str, skills: dict[str, int]) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
        )
    )


def _enc(actors: list[EncounterActor], *, category: str = "combat") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category=category,
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _pc_wounded_physical(snapshot: GameSnapshot, name: str) -> bool:
    core = snapshot.find_creature_core(name)
    assert core is not None and core.fate_sheet is not None
    sheet = core.fate_sheet
    stress_hit = any(b.checked for b in sheet.stress["physical"].boxes)
    consequence_hit = any(c.aspect is not None for c in sheet.consequences)
    return stress_hit or consequence_hit


def test_seated_opponent_attack_lands_on_the_pc():
    """One PC + one opponent. The PC seals the barrier-closing attack; the engine
    seats the opponent's attack at exchange open and it lands on the PC."""
    module = _fate_module()
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    # Hero: weak defense (Athletics 0) so the opponent's swing can land.
    hero = _pc("Hero", {"Fight": 1, "Athletics": 0, "Notice": 1})
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(_npc("Thug", {"Fight": 4, "Athletics": 2, "Notice": 3}))

    exporter, tracer = _otel()
    rng = random.Random(_HIT_SEED)
    result = dispatch_fate_action(
        payload=FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug"),
        actor_name="Hero",
        encounter=enc,
        ruleset=module,
        snapshot=snap,
        rng=rng,
        _tracer=tracer,
    )

    # Story 126-8: the PC's action closed the barrier → REVEAL seated the opponent
    # attacking the PC, so the round PARKS at the DEFEND barrier — the inline
    # server-rolled PC defense is gone (ADR-148/149). Resolution is now two-phase.
    assert result.commitment_pending is False
    assert result.awaiting_defense is True
    assert result.exchange is None
    assert len(result.defend_requests) == 1

    # The PC throws a defense just under the incoming attack (exactly one shift
    # lands), then the exchange RESUMEs and resolves.
    resolve_parked_defenses(
        encounter=enc,
        snapshot=snap,
        ruleset=module,
        target_shift=1,
        rng=random.Random(_HIT_SEED),
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    # (i) the opponent's decision span fired (at REVEAL).
    assert "fate.opponent.decided" in names
    # (ii) the opponent appears in the committed span's committed_actors.
    committed = next(s for s in spans if s.name == "fate.exchange.committed")
    committed_actors = str((committed.attributes or {}).get("committed_actors", ""))
    assert "Thug" in committed_actors
    # (iii) the targeted PC took a hit (stress/consequence) OR was taken out.
    hero_withdrawn = _actor(enc, "Hero").withdrawn
    assert _pc_wounded_physical(snap, "Hero") or hero_withdrawn


def test_two_seated_opponents_both_threaten_the_pc():
    """One PC + TWO live, uncommitted opponents. The PC closes the barrier; the
    engine seats BOTH opponents' attacks and the full walk lands BOTH on the PC."""
    module = _fate_module()
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Goon", role="foe", side="opponent"),
            EncounterActor(name="Bruiser", role="foe", side="opponent"),
        ]
    )
    # Hero: weak defense (Athletics 0) so both opponent swings can land; ample
    # stress+consequence capacity so the first hit does NOT take Hero out (which
    # would short-circuit the second opponent's attack via side-cleared).
    hero = _pc("Hero", {"Fight": 1, "Athletics": 0, "Notice": 1})
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(_npc("Goon", {"Fight": 4, "Athletics": 2, "Notice": 2}))
    snap.npcs.append(_npc("Bruiser", {"Fight": 4, "Athletics": 2, "Notice": 3}))

    exporter, tracer = _otel()
    result = dispatch_fate_action(
        payload=FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Goon"),
        actor_name="Hero",
        encounter=enc,
        ruleset=module,
        snapshot=snap,
        rng=random.Random(_DOUBLE_HIT_SEED),
        _tracer=tracer,
    )
    # Story 126-8: both opponents are seated attacking the PC at REVEAL, so the
    # round PARKS with one pending defense per incoming attack (the explicit proof
    # both threaten the PC) before any inline resolution.
    assert result.commitment_pending is False
    assert result.awaiting_defense is True
    assert result.exchange is None
    assert len(result.defend_requests) == 2

    # The PC defends BOTH incoming attacks (each landing exactly one shift), then
    # the exchange RESUMEs and the full walk lands both on the PC.
    resolve_parked_defenses(
        encounter=enc,
        snapshot=snap,
        ruleset=module,
        target_shift=1,
        rng=random.Random(_DOUBLE_HIT_SEED),
        _tracer=tracer,
    )

    spans = exporter.get_finished_spans()
    # BOTH opponents emitted a decision span.
    decided = {
        str((s.attributes or {}).get("actor")) for s in spans if s.name == "fate.opponent.decided"
    }
    assert decided == {"Goon", "Bruiser"}
    # BOTH opponents appear in the committed span.
    committed = next(s for s in spans if s.name == "fate.exchange.committed")
    committed_actors = str((committed.attributes or {}).get("committed_actors", ""))
    assert "Goon" in committed_actors and "Bruiser" in committed_actors
    # The PC accumulated stress from BOTH hits: each absorbed hit checks exactly one
    # stress box (SRD), so two distinct landed attacks ⇒ two stress.applied spans on
    # Hero — the rigorous proof both opponents threatened, not just one.
    hero_stress_marks = sum(
        1
        for s in spans
        if s.name == "fate.stress.applied" and str((s.attributes or {}).get("actor")) == "Hero"
    )
    assert hero_stress_marks == 2
    assert _pc_wounded_physical(snap, "Hero")  # the PC really took damage through the walk


def test_respects_a_pre_committed_opponent():
    """A pre-sealed opponent commit is never double-committed; seating skips it."""
    module = _fate_module()
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    hero = _pc("Hero", {"Fight": 2, "Athletics": 1, "Notice": 1})
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(_npc("Thug", {"Fight": 3, "Athletics": 1, "Notice": 2}))

    # Pre-seal the opponent's own commit, then seat: seating must skip it (no
    # double, no FateConflictError) and leave exactly one Thug-originated commit.
    seal_fate_commit(
        encounter=enc,
        actor=_actor(enc, "Thug"),
        action="attack",
        skill="Fight",
        target="Hero",
        ladder_total=3,
    )

    _seat_opponent_commits(
        encounter=enc,
        snapshot=snap,
        ruleset=module,
        rng=random.Random(_HIT_SEED),
        mental=False,
    )
    thug_commits = [c for c in enc.fate_commits if c.actor == "Thug"]
    assert len(thug_commits) == 1  # exactly one — the pre-seal, not double-committed
    assert thug_commits[0].ladder_total == 3  # untouched: the pre-sealed roll, not a reroll


def test_cleared_player_side_seats_no_opponent():
    """Every player-side actor withdrawn → no opponent is seated, no decision span."""
    module = _fate_module()
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player", withdrawn=True),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    hero = _pc("Hero", {"Fight": 2, "Athletics": 1})
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(_npc("Thug", {"Fight": 4, "Athletics": 2, "Notice": 3}))

    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc,
        snapshot=snap,
        ruleset=module,
        rng=random.Random(_HIT_SEED),
        _tracer=tracer,
    )

    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.opponent.decided" not in names
    # No opponent commit was seated (it had no live PC to target).
    assert not any(c.actor == "Thug" for c in enc.fate_commits)
    assert "fate.exchange.resolved" in names


def test_seating_is_deterministic_for_a_fixed_seed():
    """Same seed ⇒ identical opponent target, skill, and ladder_total."""
    module = _fate_module()

    def _seat_once():
        enc = _enc(
            [
                EncounterActor(name="Ada", role="lead", side="player"),
                EncounterActor(name="Boris", role="muscle", side="player"),
                EncounterActor(name="Thug", role="foe", side="opponent"),
            ]
        )
        snap = GameSnapshot(
            genre_slug="fate_test",
            characters=[
                _pc("Ada", {"Fight": 4, "Athletics": 1}),
                _pc("Boris", {"Fight": 2, "Athletics": 1}),
            ],
            encounter=enc,
        )
        snap.npcs.append(_npc("Thug", {"Fight": 3, "Shoot": 1, "Athletics": 1, "Notice": 2}))
        _seat_opponent_commits(
            encounter=enc,
            snapshot=snap,
            ruleset=module,
            rng=random.Random(_HIT_SEED),
            mental=False,
        )
        commit = next(c for c in enc.fate_commits if c.actor == "Thug")
        return commit.target, commit.skill, commit.ladder_total

    first = _seat_once()
    second = _seat_once()
    assert first == second
