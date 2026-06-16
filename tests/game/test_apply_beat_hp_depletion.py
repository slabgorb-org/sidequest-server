"""Task 5 — apply_beat hp_depletion resolution branch + dial gating.

Verifies that for ``win_condition="hp_depletion"`` confrontations apply_beat
ends the encounter when a side's primary combatant reaches 0 HP (read via the
``edge_resolver``), and that the legacy dial-threshold resolution branches are
gated OFF for hp_depletion while remaining intact for dial_threshold packs.
"""

from __future__ import annotations

import pytest

from sidequest.game import beat_kinds
from sidequest.game.beat_kinds import apply_beat
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.protocol.dice import RollOutcome


def _enc(win_condition: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        win_condition=win_condition,
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        actors=[
            EncounterActor(name="Hero", role="player", side="player"),
            EncounterActor(name="Pirate", role="opponent", side="opponent"),
        ],
    )


def _cores(pirate_hp: int, hero_hp: int = 10):
    cores = {
        "Hero": CreatureCore(
            name="Hero",
            description="a hero",
            personality="brave",
            hp=HpPool(current=hero_hp, max=10, base_max=10),
        ),
        "Pirate": CreatureCore(
            name="Pirate",
            description="a pirate",
            personality="greedy",
            hp=HpPool(current=pirate_hp, max=10, base_max=10),
        ),
    }
    return lambda name: cores.get(name)


class _StrikeBeat:
    id = "shoot"
    kind = "strike"
    base = 2  # strike Success -> own == base, so a non-zero dial delta to suppress
    stat_check = "Physique"
    damage_channel = "strike"


class _RetreatBeat:
    """Exit beat — push kind with the explicit resolution flag (mirrors the
    space_opera combat::retreat content fix). The beat-level flag ends the
    encounter on ANY outcome, including Fail (the player chose to bail)."""

    id = "retreat"
    kind = "push"
    base = 1
    stat_check = "Reflex"
    resolution = True


def test_hp_depletion_resolves_player_victory_when_opponent_drops():
    enc = _enc("hp_depletion")
    result = apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=0),
        damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
    assert result.resolved is True


def test_hp_depletion_resolves_opponent_victory_when_player_drops():
    enc = _enc("hp_depletion")
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=10, hero_hp=0),
        damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "opponent_victory"


def test_hp_depletion_does_not_resolve_while_both_alive():
    enc = _enc("hp_depletion")
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=4),
        damage_resolver=lambda: 3,
    )
    assert enc.resolved is False


def test_hp_depletion_ignores_dial_threshold():
    # Even if a metric were at threshold, hp_depletion must NOT resolve on the dial.
    enc = _enc("hp_depletion")
    enc.player_metric.current = enc.player_metric.threshold
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=5),
        damage_resolver=lambda: 1,
    )
    assert enc.resolved is False


def test_dial_threshold_still_resolves_for_non_hp_packs():
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 2
    enc.player_metric.current = 2
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=10),
        damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "player_victory"


# --- 59-26 (playtest 67-10): inert-dial suppression + voluntary exit ----------


def test_hp_depletion_suppresses_dial_advance():
    # A strike (base 2) on Success yields own delta == 2. For hp_depletion the
    # dial is an inert placeholder and must NOT move (it was being rendered as
    # the "0/1000000" bar and broadcast as phantom momentum). HP damage still
    # applies through the strike channel — only the dial is suppressed.
    enc = _enc("hp_depletion")
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=7),
        damage_resolver=lambda: 3,
    )
    assert enc.player_metric.current == 0
    assert enc.opponent_metric.current == 0


def test_dial_threshold_still_advances_the_dial():
    # The same strike on a dial pack MUST advance the dial — proves the
    # suppression is gated on hp_depletion only, not a blanket no-op.
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 99  # avoid resolving so we can read the dial
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=7),
        damage_resolver=lambda: 0,
    )
    assert enc.player_metric.current == 2


def test_resolution_beat_exits_hp_depletion_combat_on_any_outcome():
    # The unexitable-combat fix: a resolution-flagged exit beat (retreat) ends
    # an hp_depletion fight even on a Fail, while both combatants are still
    # alive — no kill required.
    enc = _enc("hp_depletion")
    result = apply_beat(
        enc,
        enc.actors[0],
        _RetreatBeat(),
        RollOutcome.Fail,
        turn=1,
        edge_resolver=_cores(pirate_hp=7, hero_hp=10),
        damage_resolver=None,
    )
    assert enc.resolved is True
    assert enc.outcome == "resolution_beat:retreat"
    assert result.resolved is True


def _capture_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture state_transition events published from apply_beat (the GM-panel
    lie-detector feed) so we can assert the suppression span actually fires."""
    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(beat_kinds, "_watcher_publish", _spy)
    return events


def test_dial_suppression_emits_otel_span(monkeypatch: pytest.MonkeyPatch):
    # OTEL Observability Principle: suppressing the dial is a subsystem decision
    # and MUST surface to the GM panel — not silently dropped.
    events = _capture_watcher(monkeypatch)
    enc = _enc("hp_depletion")
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=7),
        damage_resolver=lambda: 3,
    )
    suppressed = [e for e in events if e["fields"].get("op") == "dial_suppressed_hp_depletion"]
    assert len(suppressed) == 1
    assert suppressed[0]["fields"]["suppressed_own"] == 2
    # And the spurious metric_advance must NOT fire under hp_depletion.
    assert not [e for e in events if e["fields"].get("op") == "metric_advance"]


def test_dial_pack_emits_metric_advance_not_suppression(monkeypatch: pytest.MonkeyPatch):
    events = _capture_watcher(monkeypatch)
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 99
    apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=_cores(pirate_hp=7),
        damage_resolver=lambda: 0,
    )
    assert [e for e in events if e["fields"].get("op") == "metric_advance"]
    assert not [e for e in events if e["fields"].get("op") == "dial_suppressed_hp_depletion"]
