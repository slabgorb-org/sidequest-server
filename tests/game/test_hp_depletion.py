"""Task 8 — shared check_hp_depletion helper (ADR-114).

Tests the extracted helper in isolation. The helper is unconditional —
the caller decides WHEN to invoke it; these tests verify outcomes, enc
mutation, and already-resolved guard.
"""

from __future__ import annotations

from sidequest.game.creature_core import HpPool
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.hp_depletion import check_hp_depletion


class _Core:
    def __init__(self, name: str, hp: int) -> None:
        self.name = name
        # Build the pool directly so hp=0 yields current=0 (hp_pool_from_hp floors at 1).
        max_hp = max(1, hp)
        self.hp = HpPool(current=hp, max=max_hp, base_max=max_hp)


def _enc() -> StructuredEncounter:
    # win_condition="hp_depletion" so the fixture is realistic; the helper itself
    # is unconditional — the gate lives in the beat-loop caller.
    return StructuredEncounter(
        encounter_type="dogfight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=1_000_000
        ),
        actors=[
            EncounterActor(name="PC", role="red", side="player"),
            EncounterActor(name="Ace", role="blue", side="opponent"),
        ],
    )


def test_opponent_down_resolves_player_victory():
    enc = _enc()
    cores = {"PC": _Core("PC", 8), "Ace": _Core("Ace", 0)}
    result = check_hp_depletion(enc, lambda n: cores.get(n))
    assert result is not None
    assert enc.resolved is True
    assert enc.outcome == "player_victory"


def test_player_down_resolves_opponent_victory():
    enc = _enc()
    cores = {"PC": _Core("PC", 0), "Ace": _Core("Ace", 8)}
    result = check_hp_depletion(enc, lambda n: cores.get(n))
    assert result is not None
    assert enc.resolved is True
    assert enc.outcome == "opponent_victory"


def test_mutual_down_resolves_mutual_destruction():
    enc = _enc()
    cores = {"PC": _Core("PC", 0), "Ace": _Core("Ace", 0)}
    result = check_hp_depletion(enc, lambda n: cores.get(n))
    assert enc.resolved is True
    assert enc.outcome == "mutual_destruction"
    assert result is not None


def test_nobody_down_no_resolution():
    enc = _enc()
    cores = {"PC": _Core("PC", 8), "Ace": _Core("Ace", 8)}
    result = check_hp_depletion(enc, lambda n: cores.get(n))
    assert result is None
    assert enc.resolved is False


def test_already_resolved_is_noop():
    enc = _enc()
    enc.resolved = True
    cores = {"PC": _Core("PC", 0), "Ace": _Core("Ace", 0)}
    assert check_hp_depletion(enc, lambda n: cores.get(n)) is None


# ---------------------------------------------------------------------------
# ADR-139 Invariant 1 — side-defeat liveness (sq-playtest 2026-06-07, perseus
# MP): one downed party-mate must NOT satisfy the opponents' win condition
# while another player-side combatant stands. Side is down only when ALL
# non-withdrawn seated actors with resolvable cores are at 0 HP.
# ---------------------------------------------------------------------------


def _mp_enc() -> StructuredEncounter:
    enc = _enc()
    enc.encounter_type = "combat"
    enc.actors = [
        EncounterActor(name="Groucho", role="pc", side="player"),
        EncounterActor(name="Chico", role="pc", side="player"),
        EncounterActor(name="Boarder", role="hostile", side="opponent"),
    ]
    return enc


def test_one_downed_pc_does_not_resolve_while_party_mate_stands(otel_capture):
    enc = _mp_enc()
    cores = {
        "Groucho": _Core("Groucho", 0),  # pre-existing 0 HP ("Downed — dying")
        "Chico": _Core("Chico", 7),  # actively fighting
        "Boarder": _Core("Boarder", 8),
    }
    result = check_hp_depletion(enc, lambda n: cores.get(n), beat_id="take_cover")
    assert result is None
    assert enc.resolved is False
    assert enc.outcome is None
    # The non-resolution decision must be observable (ADR-139 Invariant 1 span).
    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "confrontation.win_condition_evaluated"
    ]
    assert spans, "expected confrontation.win_condition_evaluated on the partial-down branch"
    attrs = spans[0].attributes or {}
    assert attrs.get("terminal_reached") is False
    assert "Groucho" in attrs.get("down_actors", "")
    assert "Chico" in attrs.get("standing_actors", "")
    assert attrs.get("beat_id") == "take_cover"


def test_all_pcs_down_resolves_opponent_victory(otel_capture):
    enc = _mp_enc()
    cores = {
        "Groucho": _Core("Groucho", 0),
        "Chico": _Core("Chico", 0),
        "Boarder": _Core("Boarder", 8),
    }
    result = check_hp_depletion(enc, lambda n: cores.get(n))
    assert result is not None
    assert enc.outcome == "opponent_victory"
    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "confrontation.win_condition_evaluated"
    ]
    assert spans
    assert (spans[0].attributes or {}).get("terminal_reached") is True
    assert (spans[0].attributes or {}).get("outcome") == "opponent_victory"


def test_withdrawn_zero_hp_actor_excluded_from_liveness():
    # A withdrawn 0-HP seat neither blocks nor triggers side-defeat.
    enc = _mp_enc()
    enc.actors[0].withdrawn = True  # Groucho yielded
    cores = {
        "Groucho": _Core("Groucho", 0),
        "Chico": _Core("Chico", 7),
        "Boarder": _Core("Boarder", 0),
    }
    result = check_hp_depletion(enc, lambda n: cores.get(n))
    assert result is not None
    assert enc.outcome == "player_victory"


def test_multi_opponent_one_down_does_not_resolve():
    enc = _mp_enc()
    enc.actors.append(EncounterActor(name="Boarder Two", role="hostile", side="opponent"))
    cores = {
        "Groucho": _Core("Groucho", 5),
        "Chico": _Core("Chico", 7),
        "Boarder": _Core("Boarder", 0),
        "Boarder Two": _Core("Boarder Two", 6),
    }
    result = check_hp_depletion(enc, lambda n: cores.get(n))
    assert result is None
    assert enc.resolved is False


# ---------------------------------------------------------------------------
# Wiring test — proves the helper is reached from the PRODUCTION beat path
# (apply_beat), not just when called directly. Fixture shape copied from
# tests/game/test_apply_beat_hp_depletion.py; span capture via the otel_capture
# fixture (InMemorySpanExporter) from tests/game/conftest.py.
# ---------------------------------------------------------------------------


class _StrikeBeat:
    id = "shoot"
    kind = "strike"
    stat_check = "Physique"
    damage_channel = "strike"


def test_apply_beat_reaches_helper_and_emits_resolved_span(otel_capture):
    from sidequest.game.beat_kinds import apply_beat
    from sidequest.game.creature_core import CreatureCore
    from sidequest.protocol.dice import RollOutcome

    enc = _enc()
    cores = {
        "PC": CreatureCore(
            name="PC",
            description="a pilot",
            personality="bold",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        # Opponent already at 0 HP — the production beat loop must reach
        # check_hp_depletion and resolve player_victory.
        "Ace": CreatureCore(
            name="Ace",
            description="an ace",
            personality="cold",
            hp=HpPool(current=0, max=10, base_max=10),
        ),
    }

    result = apply_beat(
        enc,
        enc.actors[0],
        _StrikeBeat(),
        RollOutcome.Success,
        turn=1,
        edge_resolver=lambda n: cores.get(n),
        damage_resolver=lambda: 0,
    )

    assert enc.resolved is True
    assert enc.outcome == "player_victory"
    assert result.resolved is True

    spans = otel_capture.get_finished_spans()
    resolved_spans = [s for s in spans if s.name == "encounter.resolved"]
    assert resolved_spans, (
        f"expected an encounter.resolved span; got span names: {[s.name for s in spans]}"
    )
    attrs = resolved_spans[0].attributes or {}
    assert attrs.get("source") == "hp_depletion", (
        f"expected source='hp_depletion', got {attrs.get('source')!r}"
    )
    assert attrs.get("down_side") == "opponent"
    # beat_id from the beat-loop call site must survive onto the span.
    assert attrs.get("beat_id") == "shoot"
