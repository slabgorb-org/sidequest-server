"""RED tests — Story 59-33: ResolutionSignal ``yield_side`` + ``yield_side_for()``.

Adds a ``yield_side`` field to ``ResolutionSignal`` (``sidequest/game/resolution_signal.py``
— NOTE the story's stated path ``sidequest/protocol/resolution.py`` is wrong; TEA
prep correction) and a co-located derive helper ``yield_side_for(outcome)`` in
``encounter_classifier.py`` (alongside 59-32's ``is_player_victory``).

Contract (Keith's Option-C ruling, relayed via SM):
  ``yield_side_for(outcome) -> Literal["player","opponent"] | None``:
    - ``"yielded"``                          → ``"player"``  (the PLAYER side yielded — a LOSS)
    - ``"opponent_yielded"``/``"surrender"``/``"rout"`` → ``"opponent"`` (the opponent yielded — a WIN)
    - everything else (dial wins, mutual_destruction, dynamic-prefix, unknown) → ``None``
  The signal field: ``yield_side: Literal["player","opponent"] | None = None``
  (DERIVED from outcome at construction, never hand-set — the White Queen's catch:
  a hand-set None default in the factory would mislabel surrender/rout).

⚠️ ASYMMETRY (do NOT conflate with ``is_player_victory``): ``yield_side`` answers
"which side yielded", NOT "did the player win". ``"yielded"`` → side ``"player"``
but ``is_player_victory("yielded")`` is ``False`` (a player yield is a LOSS).
``"opponent_yielded"`` → side ``"opponent"`` AND ``is_player_victory`` True. The two
helpers are orthogonal.
"""

from __future__ import annotations

from typing import Any

import pytest

# outcome → expected yield_side
_SIDE_MATRIX: list[tuple[str, str | None]] = [
    # player-side yield (a LOSS) → "player"
    ("yielded", "player"),
    # opponent-side yields (a WIN) → "opponent"
    ("opponent_yielded", "opponent"),
    ("surrender", "opponent"),
    ("rout", "opponent"),
    # non-yield resolutions → None
    ("player_victory", None),
    ("opponent_victory", None),
    ("abandoned_on_location_change", None),
    ("mutual_destruction", None),
    # dynamic-prefix + unknown → None
    ("resolution_beat:press", None),
    ("composure_break:rattled", None),
    ("table_winner:Dorothy", None),
    ("resolved_by_trope:the_cavalry_arrives", None),
    ("", None),
    ("banana", None),
]


def _yield_side_for() -> Any:
    from sidequest.game.encounter_classifier import yield_side_for

    return yield_side_for


@pytest.mark.parametrize("outcome,expected", _SIDE_MATRIX)
def test_yield_side_for_matrix(outcome: str, expected: str | None) -> None:
    yield_side_for = _yield_side_for()
    assert yield_side_for(outcome) == expected, f"yield_side_for({outcome!r}) must be {expected!r}"


def test_yield_side_for_is_orthogonal_to_is_player_victory() -> None:
    """The asymmetry guard: a PLAYER yield is a player LOSS — side 'player' but
    NOT a player_victory. An OPPONENT yield is a player WIN — side 'opponent' AND
    a player_victory. Pins that the two helpers are not conflated."""
    from sidequest.game.encounter_classifier import is_player_victory, yield_side_for

    # player yield: side=player, but it's a loss
    assert yield_side_for("yielded") == "player"
    assert is_player_victory("yielded") is False

    # opponent yield: side=opponent, and it's a win
    assert yield_side_for("opponent_yielded") == "opponent"
    assert is_player_victory("opponent_yielded") is True


# ── ResolutionSignal.yield_side field (schema) ────────────────────────────────


def _minimal_signal_kwargs() -> dict[str, Any]:
    return {
        "encounter_type": "standoff",
        "outcome": "opponent_yielded",
        "final_player_metric": 2,
        "final_opponent_metric": 1,
    }


def test_yield_side_defaults_none_when_omitted() -> None:
    """AC1/AC6: the field is optional with default None — existing constructors
    that omit it keep working (non-breaking)."""
    from sidequest.game.resolution_signal import ResolutionSignal

    sig = ResolutionSignal(**_minimal_signal_kwargs())
    assert sig.yield_side is None


def test_yield_side_accepts_player_and_opponent_literals() -> None:
    """The field accepts the two valid literal values."""
    from sidequest.game.resolution_signal import ResolutionSignal

    sig_p = ResolutionSignal(**_minimal_signal_kwargs(), yield_side="player")
    sig_o = ResolutionSignal(**_minimal_signal_kwargs(), yield_side="opponent")
    assert sig_p.yield_side == "player"
    assert sig_o.yield_side == "opponent"


def test_yield_side_rejects_invalid_literal_fails_loud() -> None:
    """Literal typing fails loud on a bogus value (contract: Literal, not free
    str). The error must name the permitted literal options — distinguishing a
    real Literal-validation failure from the RED-state extra-forbid rejection
    (the field doesn't exist yet)."""
    from pydantic import ValidationError

    from sidequest.game.resolution_signal import ResolutionSignal

    with pytest.raises(ValidationError) as exc:
        ResolutionSignal(**_minimal_signal_kwargs(), yield_side="sideways")
    msg = str(exc.value)
    assert "player" in msg or "opponent" in msg, (
        "a real Literal['player','opponent'] violation names the permitted "
        f"options; got {msg!r} (RED: the field isn't a Literal yet — it's an "
        "extra-forbidden unknown key)"
    )
