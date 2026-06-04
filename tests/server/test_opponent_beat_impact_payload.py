"""RED wiring tests — Story 73-7 (opponent-side beat-impact legibility).

73-4 surfaced only the PLAYER's ``last_beat_impact`` on the CONFRONTATION payload.
A mechanics-first player (Sebastien / Jade) then sees their own critical move land
0 dials while the opponent's damage IS visible and moving the dials — reading as an
unfair outcome. The engine ALREADY records both sides in
``enc.last_beat_impacts`` (a dict keyed by side: ``"player"`` / ``"opponent"``);
73-4 just never threaded the opponent entry into the player-bound payload.

This story (UI + payload, NO engine change) demands ``build_confrontation_payload``
ALSO surface the opponent's serialized ``BeatImpact`` so the overlay can render
"you: clean exit · them: +2 pressure".

CONTRACT PINNED HERE:
  * NEW payload key ``opponent_last_beat_impact`` (additive; mirrors the existing
    ``last_beat_impact`` player key). Sourced from
    ``enc.last_beat_impacts.get("opponent")`` — the SAME serialized-BeatImpact
    shape {effect, dial_moved, summary, own, opponent, resolution, tag}.
  * Absent (None) when the opponent has not yet acted — additive, legacy-safe.
  * The player key ``last_beat_impact`` is UNCHANGED (regression guard) — we add
    a sibling, we do not rename.
  * The ``ConfrontationPayload`` protocol model (extra="forbid") MUST declare the
    new field, or wrapping the dict crashes the broadcast.

Pattern mirrors test_beat_impact_payload_wiring.py: drive the REAL ``apply_beat``
to stamp the encounter, call the REAL builder, assert the emitted dict + protocol
round-trip. No source-text assertions (CLAUDE.md "No Source-Text Wiring Tests").
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


def _angle_beat() -> BeatDef:
    return BeatDef.model_validate(
        {"id": "set_up", "label": "Set Up", "kind": "angle", "target_tag": "Off-Balance", "stat_check": "Cunning"}
    )


def _strike_beat(base: int = 2) -> BeatDef:
    return BeatDef.model_validate(
        {"id": "barb", "label": "Sharp Barb", "kind": "strike", "base": base, "stat_check": "Wit"}
    )


def _drive_both_sides() -> StructuredEncounter:
    """Player angles (tag, no resolution → live), then opponent strikes.

    After this both ``enc.last_beat_impacts["player"]`` (the angle) and
    ``["opponent"]`` (the strike) are populated — the exact repro the overlay
    must render as "you: <angle> · them: +N pressure".
    """
    enc = _enc()
    apply_beat(enc, enc.find_actor("Pryce"), _angle_beat(), RollOutcome.Success)
    apply_beat(enc, enc.find_actor("Hamish"), _strike_beat(base=2), RollOutcome.Success)
    return enc


def test_payload_surfaces_opponent_beat_impact_alongside_player():
    # The headline contract: when BOTH sides have acted, the player-bound payload
    # carries the opponent's serialized BeatImpact under opponent_last_beat_impact,
    # sourced from enc.last_beat_impacts["opponent"] — verbatim, numbers intact.
    enc = _drive_both_sides()
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    opp = payload.get("opponent_last_beat_impact")
    assert opp is not None, "opponent_last_beat_impact must be present once the opponent has acted"
    # Source of truth: it IS the opponent-side entry the engine already stamped.
    assert opp == enc.last_beat_impacts["opponent"]

    # Numeric-delta legibility (Sebastien/Jade): the dial numbers ride along, typed.
    assert isinstance(opp["own"], int)
    assert isinstance(opp["opponent"], int)
    assert isinstance(opp["effect"], str) and opp["effect"]
    # The opponent's base-2 strike actually moved a dial — a real "+N pressure".
    assert opp["dial_moved"] is True
    assert opp["own"] != 0 or opp["opponent"] != 0


def test_payload_keeps_player_and_opponent_impacts_distinct():
    # Both sides surfaced, and each reflects ITS OWN actor's beat — the player's
    # angle (tag) must not be clobbered by, nor confused with, the opponent strike.
    enc = _drive_both_sides()
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    # Player side unchanged from 73-4 behavior (regression guard).
    assert payload["last_beat_impact"]["effect"] == "tag"
    assert payload["last_beat_impact"]["tag"] == "Off-Balance"
    assert payload["last_beat_impact"] == enc.last_beat_impacts["player"]

    # Two distinct readouts — not the same object echoed twice.
    assert payload["opponent_last_beat_impact"] != payload["last_beat_impact"]


def test_payload_omits_opponent_impact_before_opponent_acts():
    # Additive / legacy-safe: player has acted, opponent has NOT → opponent key
    # absent (None), while the player key is present exactly as in 73-4.
    enc = _enc()
    apply_beat(enc, enc.find_actor("Pryce"), _push_beat(), RollOutcome.CritSuccess)
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    assert payload.get("opponent_last_beat_impact") is None
    assert payload.get("last_beat_impact") is not None  # player side still there


def test_payload_omits_both_impacts_on_fresh_encounter():
    # No beat applied at all → neither side present. Keeps the legacy shape.
    enc = _enc()
    payload = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    assert payload.get("last_beat_impact") is None
    assert payload.get("opponent_last_beat_impact") is None


def test_opponent_impact_survives_the_protocol_boundary():
    # CRITICAL wiring: the production broadcast does
    # ConfrontationPayload(**build_confrontation_payload(...)). That model is
    # extra="forbid", so opponent_last_beat_impact MUST be a declared field or the
    # wrap raises ValidationError and crashes the confrontation broadcast on every
    # opposed-beat turn. Prove the opponent descriptor round-trips through the REAL
    # protocol model, not merely that the builder dict carries the key.
    enc = _drive_both_sides()
    payload_dict = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    model = ConfrontationPayload(**payload_dict)  # must NOT raise
    assert model.opponent_last_beat_impact is not None
    assert model.opponent_last_beat_impact == enc.last_beat_impacts["opponent"]


def test_protocol_boundary_opponent_none_when_opponent_has_not_acted():
    # The additive field must also round-trip as None when only the player acted —
    # forces the model field to default None, not be required.
    enc = _enc()
    apply_beat(enc, enc.find_actor("Pryce"), _push_beat(), RollOutcome.CritSuccess)
    payload_dict = build_confrontation_payload(encounter=enc, cdef=_cdef(), genre_slug="tea_and_murder")

    model = ConfrontationPayload(**payload_dict)
    assert model.opponent_last_beat_impact is None
    assert model.last_beat_impact is not None  # player side intact
