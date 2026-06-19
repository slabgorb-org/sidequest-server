"""The pending_defenses ledger (spec 2026-06-18 §5, story 126-8): one entry per
incoming attack on a PC; an unfilled entry IS the "we are in DEFEND" signal; it
survives a snapshot round-trip (resume-safety, ADR-128) and legacy saves without
the field load as an empty ledger.

RED: FatePendingDefense and StructuredEncounter.pending_defenses do not exist yet
(plan Task 2).
"""

from __future__ import annotations

from sidequest.game.encounter import (
    EncounterMetric,
    FatePendingDefense,
    StructuredEncounter,
)


def _encounter() -> StructuredEncounter:
    # player_metric / opponent_metric are REQUIRED on StructuredEncounter — the
    # minimal valid construction (mirrors tests/_helpers/fate_fixtures.py).
    return StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
    )


def test_pending_defense_unfilled_by_default():
    e = FatePendingDefense(
        request_id="d1",
        attacker="Bandit",
        defender="Rux",
        attack_skill="Fight",
        attack_total=5,
    )
    assert e.defense_total is None
    assert e.conceded is False


def test_encounter_defaults_to_empty_ledger():
    assert _encounter().pending_defenses == []


def test_pending_defenses_survive_model_round_trip():
    e = _encounter()
    e.pending_defenses.append(
        FatePendingDefense(
            request_id="d1",
            attacker="Bandit",
            defender="Rux",
            attack_skill="Fight",
            attack_total=5,
        )
    )
    restored = StructuredEncounter.model_validate_json(e.model_dump_json())
    assert len(restored.pending_defenses) == 1
    assert restored.pending_defenses[0].request_id == "d1"
    assert restored.pending_defenses[0].defense_total is None
    assert restored.pending_defenses[0].attack_total == 5


def test_legacy_encounter_without_ledger_loads_empty():
    # A snapshot written before this field existed has no key for it.
    legacy = _encounter().model_dump()
    legacy.pop("pending_defenses", None)
    restored = StructuredEncounter.model_validate(legacy)
    assert restored.pending_defenses == []
