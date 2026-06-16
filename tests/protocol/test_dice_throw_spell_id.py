"""Story 102-2 — ``DiceThrowPayload`` gains optional ``spell_id``.

The AC5b spellcast blocker: the UI's primary path (beat tile → DICE_THROW →
``dispatch_dice_throw``) has no way to say *which spell* a cast_spell beat
casts, so the dice path can never reach the WN cast spine
(``_resolve_wwn_cast_for_beat`` gets ``spell_id`` from the narrator's
BeatSelection sidecar — a path the UI does not drive).

Contract pinned here (epic 102 context, Technical Guardrails):

  - ``spell_id`` is OPTIONAL with default ``None`` — every existing non-cast
    beat throw must be wire-compatible unchanged.
  - The field round-trips through pydantic validate/dump so the handler can
    read it off the wire.

Companion suites:
  - tests/integration/test_dice_path_spell_cast_102_2.py — dispatch routing
  - tests/server/test_dice_throw_spell_cast_wiring_102_2.py — handler wiring
"""

from __future__ import annotations

from sidequest.protocol.dice import DiceThrowPayload, ThrowParams

_THROW_PARAMS = ThrowParams(
    velocity=(0.0, 5.0, -2.0),
    angular=(1.0, 1.0, 1.0),
    position=(0.5, 0.5),
)


def _payload(**overrides: object) -> DiceThrowPayload:
    kwargs: dict[str, object] = {
        "request_id": "req-102-2",
        "throw_params": _THROW_PARAMS,
        "face": [11],
        "beat_id": "cast_spell",
    }
    kwargs.update(overrides)
    return DiceThrowPayload(**kwargs)  # type: ignore[arg-type]


def test_payload_accepts_spell_id() -> None:
    """A cast-beat commit can name the spell being cast."""
    payload = _payload(spell_id="wracking_bolt")
    assert payload.spell_id == "wracking_bolt"


def test_spell_id_defaults_to_none() -> None:
    """Omitting spell_id must default to None — non-cast beats never carry it."""
    payload = _payload()
    assert payload.spell_id is None


def test_spell_id_survives_validate_roundtrip() -> None:
    """The field must survive a dump → validate cycle (wire round-trip)."""
    dumped = _payload(spell_id="wracking_bolt").model_dump(mode="json")
    revived = DiceThrowPayload.model_validate(dumped)
    assert revived.spell_id == "wracking_bolt"


def test_legacy_wire_shape_without_spell_id_still_validates() -> None:
    """A pre-102-2 client frame (no spell_id key at all) must parse unchanged
    — the regression direction that matters for every non-cast beat."""
    legacy = {
        "request_id": "req-legacy",
        "throw_params": {
            "velocity": (0.0, 5.0, -2.0),
            "angular": (1.0, 1.0, 1.0),
            "position": (0.5, 0.5),
        },
        "face": [17],
        "beat_id": "strike",
        "player_action": "I drive the point home",
    }
    payload = DiceThrowPayload.model_validate(legacy)
    assert payload.spell_id is None
    assert payload.beat_id == "strike"
    assert payload.player_action == "I drive the point home"
