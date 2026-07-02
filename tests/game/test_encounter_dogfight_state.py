"""ADR-153 §3 state graph — ``dogfight_state`` on the encounter (158-40, AC-3).

The current relative-position state (merge / tail_chase / beam / ...) is
tracked ON the encounter so a mid-duel save/reload resumes in the correct
state (Plan 4 resume-safety constraint).

RED: ``StructuredEncounter`` is ``extra="forbid"`` and has no
``dogfight_state`` field — assignment and reads both fail.
"""

from __future__ import annotations

from sidequest.game.encounter import EncounterMetric, StructuredEncounter


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="dogfight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="hits", current=0, threshold=3),
        opponent_metric=EncounterMetric(name="hits", current=0, threshold=3),
    )


def test_dogfight_state_defaults_none() -> None:
    """AC-3: None until the sealed-letter instantiation stamps the entry
    state — non-dogfight encounters never carry a state."""
    assert _enc().dogfight_state is None


def test_dogfight_state_roundtrips_on_serialize() -> None:
    """AC-3 resume-safety: the state survives a model_dump/model_validate
    round trip with its exact value (a reload mid-duel resumes the graph
    where it left off, not back at merge)."""
    enc = _enc()
    enc.dogfight_state = "tail_chase"
    restored = StructuredEncounter.model_validate(enc.model_dump())
    assert restored.dogfight_state == "tail_chase"


def test_legacy_dump_without_field_still_validates() -> None:
    """AC-3 back-compat: a pre-158-40 save dump has no ``dogfight_state`` key
    — it must validate and land on None, not raise (old saves stay loadable)."""
    dump = _enc().model_dump()
    dump.pop("dogfight_state", None)
    restored = StructuredEncounter.model_validate(dump)
    assert restored.dogfight_state is None
