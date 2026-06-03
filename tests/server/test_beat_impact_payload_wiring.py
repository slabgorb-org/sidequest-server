"""RED wiring tests — Story 73-4 (AC3).

The semantic descriptor (test_beat_impact.py) is worthless if it never reaches the
player. AC3 demands the *player-bound* CONFRONTATION payload carry the beat-kind
impact, proven through the REAL production assembly function
``build_confrontation_payload`` (sidequest/server/dispatch/confrontation.py) — the
same builder ``websocket_session_handler.py`` calls — not a hand-rolled dict.

Pattern mirrors the existing ``win_condition`` / ``player_hp`` precedent in that
builder (added "so the player-facing overlay can render the math (Sebastien/Jade
legibility goal)") — this is the same legibility lane, one beat-kind deeper.

No source-text assertions (CLAUDE.md "No Source-Text Wiring Tests"): we drive the
real ``apply_beat`` to stamp the encounter, then call the real builder and assert
the emitted dict.
"""

from __future__ import annotations

from sidequest.game.beat_kinds import apply_beat
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef
from sidequest.protocol.dice import RollOutcome
from sidequest.protocol.messages import ConfrontationPayload
from sidequest.server.dispatch.confrontation import build_confrontation_payload


def _cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="social_duel",
        label="Duel of Wits",
        category="social",
        player_metric=MetricDef(name="barbs", starting=0, threshold=7),
        opponent_metric=MetricDef(name="barbs", starting=0, threshold=7),
        beats=[
            BeatDef.model_validate(
                {"id": "concede", "label": "Concede Gracefully", "kind": "push", "base": 1, "stat_check": "Humour"}
            ),
            BeatDef.model_validate(
                {"id": "barb", "label": "Sharp Barb", "kind": "strike", "base": 2, "stat_check": "Wit"}
            ),
        ],
    )


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="social_duel",
        player_metric=EncounterMetric(name="barbs", current=4, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="barbs", current=1, starting=0, threshold=7),
        actors=[
            EncounterActor(name="Pryce", role="duelist", side="player"),
            EncounterActor(name="Hamish", role="duelist", side="opponent"),
        ],
    )


def _push_beat() -> BeatDef:
    return BeatDef.model_validate(
        {"id": "concede", "label": "Concede Gracefully", "kind": "push", "base": 1, "stat_check": "Humour"}
    )


def _strike_beat(base: int = 2) -> BeatDef:
    return BeatDef.model_validate(
        {"id": "barb", "label": "Sharp Barb", "kind": "strike", "base": base, "stat_check": "Wit"}
    )


def test_payload_omits_impact_before_any_beat_applied():
    # Additive field: a fresh encounter (no beat yet) keeps the legacy shape.
    enc = _enc()
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")
    assert payload.get("last_beat_impact") is None


def test_payload_surfaces_player_clean_exit_impact():
    # The exact repro: Pryce commits 'Concede Gracefully' (push) at CritSuccess.
    enc = _enc()
    apply_beat(enc, enc.find_actor("Pryce"), _push_beat(), RollOutcome.CritSuccess)
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    impact = payload["last_beat_impact"]
    assert impact is not None
    assert impact["effect"] == "resolution"
    assert impact["dial_moved"] is False
    assert impact["tag"] == "Clean Exit"
    assert impact["summary"]
    assert "resolv" in impact["summary"].lower()


def test_payload_impact_is_player_side_not_opponent():
    # Player angles (no resolution → live), then opponent strikes. The
    # player-facing readout must reflect the PLAYER's beat, not the opponent's,
    # even though the opponent acted later this turn.
    enc = _enc()
    apply_beat(
        enc,
        enc.find_actor("Pryce"),
        BeatDef.model_validate(
            {"id": "set_up", "label": "Set Up", "kind": "angle", "target_tag": "Off-Balance", "stat_check": "Cunning"}
        ),
        RollOutcome.Success,
    )
    apply_beat(enc, enc.find_actor("Hamish"), _strike_beat(base=2), RollOutcome.Success)
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    impact = payload["last_beat_impact"]
    assert impact["effect"] == "tag"  # the player's angle, not Hamish's strike
    assert impact["tag"] == "Off-Balance"


def test_dial_moving_beat_still_advances_metric_and_carries_impact():
    # AC4 (no regression): an advancing beat moves the dial exactly as today AND
    # the impact rides alongside — the descriptor is additive, not a replacement.
    enc = _enc()
    before = enc.player_metric.current
    apply_beat(enc, enc.find_actor("Pryce"), _strike_beat(base=2), RollOutcome.Success)
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    assert payload["player_metric"]["current"] == before + 2  # dial still moves
    assert payload["last_beat_impact"]["effect"] == "advance"
    assert payload["last_beat_impact"]["dial_moved"] is True


def test_impact_survives_the_protocol_boundary():
    # CRITICAL wiring (spec-check, the White Queen): the production broadcast does
    # ConfrontationPayload(**build_confrontation_payload(...)). That model is
    # extra="forbid", so last_beat_impact MUST be a declared field or the wrap
    # raises ValidationError and crashes the confrontation broadcast on every
    # beat-resolution turn. Prove the descriptor round-trips through the real
    # protocol model, not just that the builder dict carries the key.
    enc = _enc()
    apply_beat(enc, enc.find_actor("Pryce"), _push_beat(), RollOutcome.CritSuccess)
    payload_dict = build_confrontation_payload(
        encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder"
    )
    model = ConfrontationPayload(**payload_dict)  # must NOT raise
    assert model.last_beat_impact is not None
    assert model.last_beat_impact["effect"] == "resolution"
    assert model.last_beat_impact["tag"] == "Clean Exit"


def test_protocol_boundary_clean_when_no_impact():
    # The additive field must also round-trip as None on a fresh encounter.
    enc = _enc()
    payload_dict = build_confrontation_payload(
        encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder"
    )
    model = ConfrontationPayload(**payload_dict)
    assert model.last_beat_impact is None
