"""Edge-debit wiring for ``apply_beat`` (Step 1 of numerical-advantage design).

Closes the dead-field gap: ``BeatDef.edge_delta`` and
``BeatDef.target_edge_delta`` were declared in the schema but never read by
``apply_beat``. Per ADR-078 §3-4, beats that declare an edge debit must
mutate the resolved actor's ``CreatureCore.edge`` and trip
``encounter.resolved`` on composure break.

These tests do NOT exercise numerical-advantage scaling (Step 3) or
target-select modes (Step 2). They cover only the foundational wiring:
single-target debits, self debits, composure break, and the no-silent-
fallback contract.
"""

from __future__ import annotations

import pytest

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.dice import RollOutcome


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )


def _core(name: str, *, current: int = 10, max_: int = 10) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="x",
        personality="x",
        hp=HpPool(current=current, max=max_, base_max=max_),
    )


def _strike_with_edge(
    *,
    base: int = 2,
    edge_delta: int | None = None,
    target_edge_delta: int | None = None,
) -> BeatDef:
    payload: dict = {
        "id": "attack",
        "label": "attack",
        "kind": "strike",
        "base": base,
        "stat_check": "STR",
    }
    if edge_delta is not None:
        payload["edge_delta"] = edge_delta
    if target_edge_delta is not None:
        payload["target_edge_delta"] = target_edge_delta
    return BeatDef.model_validate(payload)


# ---------------------------------------------------------------------------
# Foundational wiring
# ---------------------------------------------------------------------------


def test_target_edge_delta_debits_opposing_first_actor_core():
    """A strike with target_edge_delta=3 drops the opponent's edge by 3."""
    enc = _enc()
    sam = enc.find_actor("Sam")
    sam_core = _core("Sam")
    promo_core = _core("Promo", current=10)
    cores = {"Sam": sam_core, "Promo": promo_core}

    apply_beat(
        enc,
        sam,
        _strike_with_edge(target_edge_delta=3),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert promo_core.hp.current == 7
    assert sam_core.hp.current == 10  # actor untouched when only target_edge_delta set


def test_edge_delta_debits_acting_actor_core():
    """A push with edge_delta=2 (positive = cost) drops the actor's own edge by 2."""
    enc = _enc()
    sam = enc.find_actor("Sam")
    sam_core = _core("Sam")
    cores = {"Sam": sam_core}

    apply_beat(
        enc,
        sam,
        _strike_with_edge(edge_delta=2),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert sam_core.hp.current == 8


def test_target_edge_delta_drives_composure_break_resolves_encounter():
    """Per ADR-078 §4: edge to 0 sets enc.resolved=True (target side break)."""
    enc = _enc()
    sam = enc.find_actor("Sam")
    promo_core = _core("Promo", current=2)
    cores = {"Sam": _core("Sam"), "Promo": promo_core}

    apply_beat(
        enc,
        sam,
        _strike_with_edge(target_edge_delta=2),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert promo_core.hp.current == 0
    assert enc.resolved is True


def test_edge_delta_self_break_resolves_encounter():
    """Per ADR-078 §4: actor self-break also resolves the encounter."""
    enc = _enc()
    sam = enc.find_actor("Sam")
    sam_core = _core("Sam", current=1)
    cores = {"Sam": sam_core}

    apply_beat(
        enc,
        sam,
        _strike_with_edge(edge_delta=1),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert sam_core.hp.current == 0
    assert enc.resolved is True


def test_no_resolver_raises_when_beat_declares_edge_delta():
    """No silent fallback (CLAUDE.md): missing resolver + edge debit is an error."""
    enc = _enc()
    sam = enc.find_actor("Sam")

    with pytest.raises(ValueError, match="edge_resolver"):
        apply_beat(
            enc,
            sam,
            _strike_with_edge(target_edge_delta=1),
            RollOutcome.Success,
        )


def test_no_resolver_ok_when_beat_omits_edge_fields():
    """Back-compat: beats without edge_delta / target_edge_delta still work
    when no resolver is provided. Existing test fixtures must not break."""
    enc = _enc()
    sam = enc.find_actor("Sam")

    result = apply_beat(enc, sam, _strike_with_edge(base=2), RollOutcome.Success)

    assert result.skipped_reason is None
    assert enc.player_metric.current == 2  # dial advance still fires


def test_unknown_target_name_raises_no_silent_skip():
    """If resolver returns None for the resolved target, fail loud rather
    than silently skipping the debit."""
    enc = _enc()
    sam = enc.find_actor("Sam")
    cores = {"Sam": _core("Sam")}  # Promo missing

    with pytest.raises(ValueError, match="Promo"):
        apply_beat(
            enc,
            sam,
            _strike_with_edge(target_edge_delta=1),
            RollOutcome.Success,
            edge_resolver=cores.get,
        )


def test_target_edge_delta_skipped_when_no_opposing_actor():
    """If the encounter has no live opposing actor, target_edge_delta is a
    no-op (legitimate state — e.g. last opponent already withdrew). The own
    debit (if any) still applies."""
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="Sam", role="combatant", side="player")],
    )
    sam = enc.find_actor("Sam")
    sam_core = _core("Sam")
    cores = {"Sam": sam_core}

    # Should not raise — target absence is structurally legitimate.
    apply_beat(
        enc,
        sam,
        _strike_with_edge(target_edge_delta=3, edge_delta=1),
        RollOutcome.Success,
        edge_resolver=cores.get,
    )

    assert sam_core.hp.current == 9  # self debit still applied
