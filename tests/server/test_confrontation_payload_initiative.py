"""SWN P4: the confrontation payload surfaces the initiative order so the table
sees it as a plain list (no 3D cup) — Sebastien/Jade legibility."""

from __future__ import annotations

from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.genre.models.rules import ConfrontationDef, WinCondition
from sidequest.protocol.models import InitiativeEntry
from sidequest.server.dispatch.confrontation import build_confrontation_payload


def _cdef() -> ConfrontationDef:
    return ConfrontationDef(
        confrontation_type="firefight",
        label="Firefight",
        category="combat",
        win_condition=WinCondition.hp_depletion,
        opponent_default_stats={"hp": 7, "armor_class": 12, "dexterity": 13},
        beats=[{"id": "shoot", "label": "Shoot", "stat_check": "Physique", "base": 1, "kind": "strike"}],
    )


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="firefight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1),
        actors=[
            EncounterActor(name="Rux", role="attacker", side="player"),
            EncounterActor(name="Raider", role="attacker", side="opponent"),
        ],
        initiative=[
            InitiativeEntry(token_id="Rux", value=9),
            InitiativeEntry(token_id="Raider", value=5),
        ],
    )


def test_payload_carries_initiative_order():
    payload = build_confrontation_payload(encounter=_enc(), cdef=_cdef(), genre_slug="space_opera")
    assert payload["initiative_order"] == [
        {"name": "Rux", "roll": 9},
        {"name": "Raider", "roll": 5},
    ]


def test_payload_omits_initiative_when_empty():
    enc = _enc()
    enc.initiative = []
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="space_opera")
    assert payload.get("initiative_order") in (None, [])


def test_confrontation_payload_model_accepts_initiative_order():
    from sidequest.protocol.messages import ConfrontationPayload

    p = ConfrontationPayload(
        type="firefight", label="Firefight", category="combat", genre_slug="space_opera",
        initiative_order=[{"name": "Rux", "roll": 9}],
    )
    assert p.initiative_order == [{"name": "Rux", "roll": 9}]
