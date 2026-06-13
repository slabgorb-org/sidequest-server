"""Task 5: net_run dispatch branch — 2d6 Program check vs security DC.

spec 2026-05-29 — net_run resolves as 2d6 + INT mod + Program skill vs a
security-tier DC, NOT d20 vs AC. The dispatch fork is gated on (cwn ruleset AND
hacking category). After resolution, cwn.hacking.security_check is emitted via
resolve_hacking — the GM lie-detector span for the hacking subsystem.
"""

from __future__ import annotations

import uuid

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status, StatusSeverity
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    CwnConfig,
    HackingConfig,
    MetricDef,
    RulesConfig,
)
from sidequest.protocol.dice import DiceThrowPayload, DieSides, RollOutcome, ThrowParams
from sidequest.protocol.messages import DiceRequestMessage
from sidequest.server.dispatch.dice import dispatch_dice_throw

_THROW = ThrowParams(velocity=(0, 0, 0), angular=(0, 0, 0), position=(0, 0))


def _pack() -> GenrePack:
    cwn = CwnConfig(
        attribute_map={
            "STRENGTH": "Brawn",
            "DEXTERITY": "Reflex",
            "CONSTITUTION": "Body",
            "INTELLIGENCE": "Tech",
            "WISDOM": "Instinct",
            "CHARISMA": "Cool",
        },
        hacking=HackingConfig(
            default_tier="office",
            security_tiers={"home": 7, "office": 9, "facility": 11, "black_site": 12},
        ),
    )
    rules = RulesConfig(
        ruleset="cwn",
        ability_score_names=["Brawn", "Reflex", "Body", "Tech", "Instinct", "Cool"],
        cwn=cwn,
        confrontations=[
            ConfrontationDef(
                type="net_run",
                label="Net Run",
                category="hacking",
                player_metric=MetricDef(name="data", starting=0, threshold=10),
                opponent_metric=MetricDef(name="alert", starting=0, threshold=10),
                beats=[
                    BeatDef(
                        id="run_program",
                        label="Run Program",
                        kind="strike",
                        base=2,
                        stat_check="Tech",
                        combat_skill=1,
                    ),
                ],
                mood="tension",
            )
        ],
    )
    return GenrePack.model_construct(rules=rules)


def _encounter(alert_current: int = 0, security_tier: str = "black_site") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="net_run",
        player_metric=EncounterMetric(name="data", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(
            name="alert", current=alert_current, starting=0, threshold=10
        ),
        actors=[EncounterActor(name="Rux", role="runner", side="player")],
        security_tier=security_tier,
    )


def _runner_core(roll_modifier: int) -> CreatureCore:
    core = CreatureCore(name="Rux", description="a netrunner", personality="cool")
    if roll_modifier:
        core.statuses.append(
            Status(
                text="jacked-in penalty", severity=StatusSeverity.Wound, roll_modifier=roll_modifier
            )
        )
    return core


def _drive(*, faces, alert_current, security_tier="black_site", runner_roll_modifier=0):
    captured: list = []
    snap = GameSnapshot()
    snap.genre_slug = "test_neon"
    snap.characters = [
        Character(
            core=_runner_core(runner_roll_modifier),
            backstory="A netrunner.",
            char_class="Runner",
            race="Human",
        )
    ]
    payload = DiceThrowPayload(
        request_id=str(uuid.uuid4()),
        throw_params=_THROW,
        face=faces,
        beat_id="run_program",
    )
    outcome = dispatch_dice_throw(
        payload=payload,
        rolling_player_id="p1",
        character_name="Rux",
        character_stats={"Tech": 14},  # SWN curve: score 14 → +1 mod
        encounter=_encounter(alert_current, security_tier=security_tier),
        pack=_pack(),
        genre_slug="test_neon",
        session_id="s1",
        round_number=1,
        room_broadcast=captured.append,
        snapshot=snap,
    )
    return outcome, captured


def test_net_run_builds_2d6_request_at_effective_dc():
    # black_site base 12, +2 alert escalation → effective DC 14.
    outcome, captured = _drive(faces=[6, 6], alert_current=2)
    req_msgs = [m for m in captured if isinstance(m, DiceRequestMessage)]
    assert req_msgs, "expected a DiceRequest broadcast"
    req = req_msgs[0].payload
    # 2d6 pool — NOT a d20 attack.
    assert len(req.dice) == 1
    assert req.dice[0].sides == DieSides.D6
    assert req.dice[0].count == 2
    assert req.difficulty == 14  # base_dc(12) + alert_modifier(2)


def test_net_run_controlled_faces_resolve_tier():
    # office=9, alert 0 → DC 9.
    # Faces 2+3=5, +modifier(INT +1 + Program 1 = 2) → 7 < 9 → Fail.
    # Use faces well clear of the DC so the tier (Fail) is unambiguous.
    outcome, _ = _drive(faces=[2, 3], alert_current=0, security_tier="office")
    assert outcome.outcome == RollOutcome.Fail


def test_net_run_status_roll_modifier_drops_modifier():
    """A dark/penalized runner's net-run modifier is 2 lower than a clean one.

    Phase 2 of the light & darkness survival-clock spec: the hacking roll site
    must read the actor's aggregate status roll_modifier, so a -2 status (e.g.
    running in the dark) drags the Program check down by 2.
    """

    def _modifier(runner_roll_modifier: int) -> int:
        _, captured = _drive(
            faces=[3, 3], alert_current=0, runner_roll_modifier=runner_roll_modifier
        )
        req_msgs = [m for m in captured if isinstance(m, DiceRequestMessage)]
        assert req_msgs, "expected a DiceRequest broadcast"
        return req_msgs[0].payload.modifier

    lit_modifier = _modifier(0)
    dark_modifier = _modifier(-2)
    assert dark_modifier == lit_modifier - 2


def test_net_run_fires_security_check_span(otel_capture):
    """Verifies cwn.hacking.security_check span is emitted for every net_run resolution.

    Uses the ``otel_capture`` conftest fixture which adds a processor to the
    existing global TracerProvider (rather than replacing it) so module-level
    cached tracers route to the in-memory exporter.
    """
    _drive(faces=[5, 5], alert_current=0)
    names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.hacking.security_check" in names
