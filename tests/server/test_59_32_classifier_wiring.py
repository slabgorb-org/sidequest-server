"""RED tests — Story 59-32: classifier wiring + opponent_yield_outcome normalization.

Two contracts beyond the unit matrix (``tests/game/test_59_32_is_player_victory.py``):

1. **Wiring (non-vacuous): the classifier is the PRODUCER of the credit-victory
   label in ``_resolve_opponent_yield``.** Today that function hardcodes
   ``"outcome": "player_victory"`` in its ``component="confrontation"`` watcher
   emit (``narration_apply.py:4574``). After 59-32 that credit label must be
   DERIVED through ``is_player_victory()`` — proven here by patching the
   classifier seam to flip its verdict and asserting the emitted credit label
   flips with it (a hardcode would not). Per Keith's reframe, this REPLACES the
   vacuous ``award_turn_xp`` XP-parity test (award_turn_xp is a flat per-turn
   tick, never outcome-gated — TEA prep finding) and does NOT touch the
   kill/defeated-side tracker at ``websocket_session_handler.py:195`` (divergent
   semantics, out of scope).

2. **Step 3: ``opponent_yield_outcome()`` returns the mechanical-truth label
   ``"opponent_yielded"``**, not the credit label ``"player_victory"`` — so every
   producer emits its mechanical-truth label and the classifier owns the
   credit mapping (story §39). Production consumers are None-check-only
   (``narration_apply.py:2824,4615``), so this is consumer-safe.

   ⚠️ CONFLICT FLAGGED TO SM (see session Delivery Findings): four existing 59-31
   tests in ``tests/server/test_opponent_yield_resolution.py`` (lines 140,148,153,198)
   assert ``opponent_yield_outcome() == "player_victory"`` — landing step 3 makes
   them fail, tensioning the "existing 59-31 tests remain passing" AC. Those four
   must be updated to the normalized return (a deliberate contract change), or
   step 3's return-change dropped. Pending SM/White Queen ruling; this test
   encodes the requested step-3 behavior.

Wiring-seam note (mirrors 59-4 wiring-test convention): the producer-proof test
patches ``narration_apply.is_player_victory``, i.e. it expects Dev to bring the
classifier into the ``narration_apply`` module namespace via a module-level
``from ...encounter_classifier import is_player_victory`` and call the bound
name. If Dev calls it module-qualified instead, update the patch target + log a
Design Deviation.
"""

from __future__ import annotations

from typing import Any

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ── fixtures / builders (mirror tests/server/test_opponent_yield_resolution.py) ──


def _encounter(
    *,
    opponents: list[EncounterActor],
    players: list[EncounterActor] | None = None,
    opponents_disposition: str | None = None,
    win_condition: str = "dial_threshold",
    threshold: int = 8,
) -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="standoff",
        win_condition=win_condition,  # type: ignore[arg-type]
        player_metric=EncounterMetric(name="resolve", current=2, starting=0, threshold=threshold),
        opponent_metric=EncounterMetric(name="menace", current=1, starting=0, threshold=threshold),
        actors=[
            *(players or [EncounterActor(name="Dorothy", role="lead", side="player")]),
            *opponents,
        ],
    )
    if opponents_disposition is not None:
        enc.opponents_disposition = opponents_disposition
    return enc


def _lion(withdrawn: bool = True) -> EncounterActor:
    return EncounterActor(
        name="The Cowardly Lion", role="aggressor", side="opponent", withdrawn=withdrawn
    )


def _capture_watcher(monkeypatch) -> list[dict[str, Any]]:
    """Capture ``narration_apply._watcher_publish`` calls (mirrors the canonical
    confrontation-OTEL capture in test_opponent_yield_resolution.py)."""
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _capture)
    return captured


def _yield_events(captured: list[dict]) -> list[dict]:
    return [
        e
        for e in captured
        if e["component"] == "confrontation"
        and e["fields"].get("resolution_label") == "opponent_yielded"
    ]


def _drive_yield(snap: GameSnapshot) -> None:
    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(narration="The Lion backs away whimpering.", beat_selections=[]),
        player_name="Dorothy",
        room=room_for(snap),
    )


# ── Wiring: classifier is the PRODUCER of the credit-victory label ────────────


def test_credit_label_consistent_with_classifier_on_real_path(monkeypatch) -> None:
    """Positive/regression anchor: on the real opponent-yield path the emitted
    credit ``outcome`` is ``player_victory`` AND that agrees with the classifier
    applied to the mechanical label the engine set (``enc.outcome``)."""
    from sidequest.game.encounter_classifier import is_player_victory  # noqa: PLC0415

    captured = _capture_watcher(monkeypatch)
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=True)])

    _drive_yield(snap)

    events = _yield_events(captured)
    assert len(events) == 1, f"expected one opponent-yield event; got {captured!r}"
    credit = events[0]["fields"].get("outcome")
    assert credit == "player_victory"
    # The emitted credit label agrees with the classifier's verdict on the
    # mechanical-truth label the engine recorded.
    assert is_player_victory(snap.encounter.outcome) is True
    assert (credit == "player_victory") == is_player_victory(snap.encounter.outcome)


def test_credit_label_derives_through_classifier_not_hardcoded(monkeypatch) -> None:
    """Producer proof (non-vacuous): patch the classifier seam in narration_apply
    to return False; the emitted credit ``outcome`` must flip away from
    ``player_victory``. A hardcoded ``"outcome": "player_victory"`` (the current
    code) does NOT flip → fails RED. This proves the credit label is DERIVED
    through ``is_player_victory()``.

    ``raising=False``: in RED the symbol isn't in narration_apply's namespace, so
    the patch is inert and the hardcoded emit stays ``player_victory`` → the
    assertion fails (correct RED reason: classifier not wired as producer)."""
    monkeypatch.setattr(narration_apply, "is_player_victory", lambda _outcome: False, raising=False)
    captured = _capture_watcher(monkeypatch)
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=True)])

    _drive_yield(snap)

    events = _yield_events(captured)
    assert len(events) == 1, f"expected one opponent-yield event; got {captured!r}"
    credit = events[0]["fields"].get("outcome")
    assert credit != "player_victory", (
        "the credit-victory label must be DERIVED through is_player_victory() — "
        "with the classifier patched to return False, the emit must no longer be "
        f"'player_victory' (got {credit!r}). A hardcoded mapping fails this."
    )


# ── Step 3: opponent_yield_outcome() returns the mechanical-truth label ───────


def test_opponent_yield_outcome_returns_mechanical_truth_label() -> None:
    """Step 3: ``opponent_yield_outcome()`` returns ``"opponent_yielded"`` (the
    mechanical-truth label) — NOT the credit label ``"player_victory"``. The
    classifier owns the credit mapping; producers emit mechanical truth.

    RED today: the method returns ``"player_victory"`` (``encounter.py:301``).
    Consumer-safe (production consumers None-check only) — but see the flagged
    4-test conflict in this module's docstring."""
    enc = _encounter(opponents=[_lion(withdrawn=True)])
    assert enc.opponent_yield_outcome() == "opponent_yielded", (
        "opponent_yield_outcome() must return the mechanical-truth label "
        "'opponent_yielded'; the player_victory credit mapping now lives in "
        f"is_player_victory(). got {enc.opponent_yield_outcome()!r}"
    )


def test_opponent_yield_label_still_credits_victory_via_classifier() -> None:
    """The normalized return remains a victory FOR CREDIT — the classifier maps
    ``opponent_yielded`` → True, so step 3 changes the label, not the credit."""
    from sidequest.game.encounter_classifier import is_player_victory  # noqa: PLC0415

    enc = _encounter(opponents=[_lion(withdrawn=True)])
    label = enc.opponent_yield_outcome()
    assert label is not None
    assert is_player_victory(label) is True, (
        "normalizing the label to 'opponent_yielded' must NOT lose victory credit "
        "— the classifier maps it True"
    )
