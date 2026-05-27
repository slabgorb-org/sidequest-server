"""Task 6 (space_opera → SWN binding): the CONFRONTATION payload surfaces
``win_condition`` (always) and primary-combatant HP (``player_hp`` /
``opponent_hp``, only under ``hp_depletion``) so the player-facing overlay can
read the math (Sebastien/Jade legibility goal).

The payload is a plain ``dict`` (built by ``build_confrontation_payload`` and
later fed to ``ConfrontationPayload(**dict)``), so we assert on dict keys. The
new keys are ADDITIVE — dial-threshold packs keep their existing shape.
"""

from __future__ import annotations

from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef, WinCondition
from sidequest.server.dispatch.confrontation import build_confrontation_payload


def _beat() -> BeatDef:
    return BeatDef(id="strike", label="Strike", kind="strike", stat_check="STR")


def _cdef(win_condition: WinCondition) -> ConfrontationDef:
    # hp_depletion confrontations carry no dial metrics; dial_threshold ones
    # require both. Build the matching shape so the def validates.
    if win_condition == WinCondition.dial_threshold:
        return ConfrontationDef(
            type="combat",
            label="Combat",
            category="combat",
            win_condition=win_condition,
            player_metric=MetricDef(name="momentum", threshold=10),
            opponent_metric=MetricDef(name="threat", threshold=10),
            beats=[_beat()],
        )
    return ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        win_condition=win_condition,
        beats=[_beat()],
    )


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


def _cores() -> dict[str, CreatureCore]:
    return {
        "Hero": CreatureCore(
            name="Hero",
            description="d",
            personality="p",
            hp=HpPool(current=8, max=10, base_max=10),
        ),
        "Pirate": CreatureCore(
            name="Pirate",
            description="d",
            personality="p",
            hp=HpPool(current=3, max=10, base_max=10),
        ),
    }


def test_payload_includes_win_condition_and_hp():
    cores = _cores()
    payload = build_confrontation_payload(
        encounter=_enc("hp_depletion"),
        cdef=_cdef(WinCondition.hp_depletion),
        genre_slug="space_opera",
        core_resolver=lambda n: cores.get(n),
    )
    assert payload["win_condition"] == "hp_depletion"
    assert payload["player_hp"] == {"current": 8, "max": 10}
    assert payload["opponent_hp"] == {"current": 3, "max": 10}


def test_dial_threshold_payload_has_win_condition_but_no_hp():
    # Additive contract: dial packs still emit win_condition, but NOT hp keys.
    payload = build_confrontation_payload(
        encounter=_enc("dial_threshold"),
        cdef=_cdef(WinCondition.dial_threshold),
        genre_slug="space_opera",
        core_resolver=lambda n: _cores().get(n),
    )
    assert payload["win_condition"] == "dial_threshold"
    assert "player_hp" not in payload
    assert "opponent_hp" not in payload


def test_hp_depletion_without_resolver_omits_hp():
    # Defensive: no resolver threaded (legacy fixture) -> win_condition still
    # present, hp keys absent rather than crashing.
    payload = build_confrontation_payload(
        encounter=_enc("hp_depletion"),
        cdef=_cdef(WinCondition.hp_depletion),
        genre_slug="space_opera",
    )
    assert payload["win_condition"] == "hp_depletion"
    assert "player_hp" not in payload
    assert "opponent_hp" not in payload
