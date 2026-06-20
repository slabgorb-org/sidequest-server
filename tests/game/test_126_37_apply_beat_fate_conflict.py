"""RED (story 126-37): de-nativize Fate confrontation RESOLUTION — apply_beat guard.

126-30 de-nativized Fate confrontation SEATING: a Fate standoff now seats with
``win_condition == "fate_conflict"`` (the native ``tension`` dial REMOVED, the win
signal moved to the opponent's FateSheet stress). 126-37 is the downstream half —
the RESOLUTION dial guards that 126-30 left native.

This module pins the FIRST of the three downstream guards: the module-level
``apply_beat`` (``beat_kinds.py``), the belt that catches ANY caller (the narration
pipeline's legacy beat loop calls it at narration_apply.py:6061). Under
``win_condition == "fate_conflict"`` a Fate conflict resolves through the 4dF engine
(fate_conflict.py) reading the Other's stress — it must NEVER apply a native beat to
the vestigial dial. Per ADR-143/144 ("Bind the Ruleset, Don't Balance It") the native
beat is REMOVED from the Fate path, not tuned to coexist: ``apply_beat`` short-circuits
and returns early (no dial mutation, no tag, no native resolution), emitting a
suppression event so the GM panel sees the engine actively stood down (OTEL
Observability Principle / No Silent Fallbacks).

Pattern mirrors the existing ``win_condition == "hp_depletion"`` dial suppression in the
same function (``dial_suppressed_hp_depletion``, tests/game/test_apply_beat_hp_depletion.py)
and the SEATING sibling test (tests/server/dispatch/test_fate_seating_denativized_126_30.py).

HOW-agnostic where it matters: the hard RED assertions are behavioral — the dial does
NOT move and the beat is NOT applied (``skipped_reason`` set, no deltas) for a Fate
conflict, while a native (dial_threshold) pack applies the same beat unchanged. The
suppression-event ``op`` name is the chosen GM-panel marker (``beat_suppressed_fate_conflict``,
sibling of ``dial_suppressed_hp_depletion``); Dev may finalize the exact string as long as
it is a distinct, fate_conflict-gated suppression op (not a reused ``metric_advance`` /
``beat_applied``).
"""

from __future__ import annotations

import pytest

from sidequest.game import beat_kinds
from sidequest.game.beat_kinds import apply_beat
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.genre.models.rules import BeatDef
from sidequest.protocol.dice import RollOutcome


def _enc(win_condition: str) -> StructuredEncounter:
    """A two-dial standoff. The dials are placeholders here; the point of the test
    is whether ``apply_beat`` moves them under ``win_condition``."""
    return StructuredEncounter(
        encounter_type="standoff",
        win_condition=win_condition,
        player_metric=EncounterMetric(name="tension", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="tension", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Reb", role="player", side="player"),
            EncounterActor(name="Foe", role="opponent", side="opponent"),
        ],
    )


def _strike_beat() -> BeatDef:
    """A strike beat (no edge_delta → no HP channel): Success tier yields a clean
    own-side dial delta of ``base`` (DEFAULT_DELTAS[strike][Success] == {"own_expr": "b"}).
    On a native pack this advances player_metric to 2; under Fate it must not move."""
    return BeatDef.model_validate(
        {"id": "draw", "label": "Draw", "kind": "strike", "base": 2, "stat_check": "DRAW"}
    )


def _capture_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture state_transition events published from apply_beat (the GM-panel
    lie-detector feed). Patches the name as USED in beat_kinds (check #6: patch
    where used, not where defined)."""
    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(beat_kinds, "_watcher_publish", _spy)
    return events


# ---------------------------------------------------------------------------
# 1 — RED (AC-1, AC-2): apply_beat must suppress the native beat for a Fate conflict.
# ---------------------------------------------------------------------------


def test_apply_beat_suppressed_for_fate_conflict() -> None:
    """AC-1/AC-2: under ``win_condition == "fate_conflict"`` apply_beat must NOT apply
    the native beat — the dials stay frozen and the beat is reported skipped (no deltas).
    Today apply_beat only special-cases ``hp_depletion``, so a Fate conflict falls through
    and the strike's own delta (2) advances player_metric to 2 — this fails (RED)."""
    enc = _enc("fate_conflict")
    result = apply_beat(enc, enc.actors[0], _strike_beat(), RollOutcome.Success, turn=1)

    assert enc.player_metric.current == 0, (
        "a Fate conflict resolves through the 4dF engine (opponent stress), NOT the "
        "native tension dial — apply_beat advanced the suppressed dial to "
        f"{enc.player_metric.current} (ADR-143/144: the native beat is REMOVED from the "
        "Fate path, not applied)"
    )
    assert enc.opponent_metric.current == 0, (
        f"opponent dial moved to {enc.opponent_metric.current} on a Fate conflict; the "
        "native dial is removed under win_condition=fate_conflict"
    )
    # The beat was short-circuited, not applied: skipped_reason is set, no deltas routed.
    assert result.skipped_reason is not None, (
        "apply_beat must short-circuit a Fate-conflict beat with a skipped_reason "
        "(mirrors the encounter_resolved / neutral_actor / withdrawn_actor early returns); "
        f"got skipped_reason={result.skipped_reason!r}"
    )
    assert result.deltas is None, (
        "a suppressed Fate-conflict beat applies no deltas; "
        f"got deltas={result.deltas!r}"
    )
    # No native tag was created either — removal is total, not dial-only.
    assert enc.tags == [], (
        f"a suppressed Fate-conflict beat must not create native encounter tags; got {enc.tags!r}"
    )


def test_apply_beat_emits_fate_conflict_suppression_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4 (OTEL Observability Principle): suppressing the native beat is a real
    subsystem decision (the bound ruleset replaced the native engine here) and MUST
    surface to the GM panel — not a silent early return. Sibling of
    ``dial_suppressed_hp_depletion``. Today no such event fires and the spurious
    ``metric_advance`` DOES fire — both halves fail (RED)."""
    events = _capture_watcher(monkeypatch)
    enc = _enc("fate_conflict")
    apply_beat(enc, enc.actors[0], _strike_beat(), RollOutcome.Success, turn=1)

    suppressed = [e for e in events if e["fields"].get("op") == "beat_suppressed_fate_conflict"]
    assert len(suppressed) == 1, (
        "apply_beat must emit exactly one beat_suppressed_fate_conflict state_transition "
        "for a Fate conflict (the GM-panel lie-detector that the native engine stood "
        f"down); got ops={[e['fields'].get('op') for e in events]}"
    )
    # The spurious native dial advance must NOT fire under a Fate conflict.
    assert not [e for e in events if e["fields"].get("op") == "metric_advance"], (
        "metric_advance must NOT fire for a Fate conflict — the native dial is removed"
    )


# ---------------------------------------------------------------------------
# 2 — PIN (AC-4, cross-ruleset): a native (dial_threshold) pack keeps native beats.
# ---------------------------------------------------------------------------


def test_dial_threshold_still_applies_beat() -> None:
    """Cross-ruleset guard: the suppression MUST be ``win_condition``-gated, never a
    blanket removal. A native dial pack's standoff still applies the strike's own delta
    to the dial. Passes before AND after the fix — pins that native resolution is
    untouched (the de-nativization is Fate-only)."""
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 99  # avoid resolving so we can read the dial
    result = apply_beat(enc, enc.actors[0], _strike_beat(), RollOutcome.Success, turn=1)

    assert enc.player_metric.current == 2, (
        "a native dial pack must still apply the strike's own delta (2) to the dial; "
        f"got {enc.player_metric.current}"
    )
    assert result.skipped_reason is None, (
        f"a native beat is applied, not skipped; got skipped_reason={result.skipped_reason!r}"
    )


def test_dial_threshold_emits_metric_advance_not_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-ruleset PIN on the telemetry: a native pack emits ``metric_advance`` and
    NEVER the Fate suppression op — proves the suppression event is Fate-only, not a
    blanket rename of the dial-advance feed."""
    events = _capture_watcher(monkeypatch)
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 99
    apply_beat(enc, enc.actors[0], _strike_beat(), RollOutcome.Success, turn=1)

    assert [e for e in events if e["fields"].get("op") == "metric_advance"], (
        "a native dial pack must emit metric_advance for an applied beat"
    )
    assert not [
        e for e in events if e["fields"].get("op") == "beat_suppressed_fate_conflict"
    ], "beat_suppressed_fate_conflict must be Fate-only — it must never fire for a native pack"
