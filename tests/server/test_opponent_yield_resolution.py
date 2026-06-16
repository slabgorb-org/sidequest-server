"""Story 59-31 — Opponent-yield signal: record ``opponent_yielded`` /
player_victory when an OPPONENT backs down, NOT ``abandoned_on_location_change``
and NOT the player-side ``yielded`` (loss).

Playtest bug (sq-playtest wry_whimsy/oz, 2026-06-02-oz-3, turn 7): the player
stood their ground; the Cowardly Lion YIELDED (backed down — his canon
behavior). The confrontation resolved same-turn in prose with ZERO beats and no
dial threshold met. The recorded outcome was ``abandoned_on_location_change`` —
which reads as "the player walked away from an unfinished fight" when in fact the
opponent surrendered and the player PREVAILED. The label is wrong.

ASYMMETRY THE DESIGN MUST PRESERVE (ADR-116 "a confrontation requires an
Other"): a PLAYER yield is a withdrawal/LOSS (``handle_yield`` →
``outcome="yielded"``); an OPPONENT yield is a player VICTORY. Opponent-yield
maps to a DISTINCT outcome (``opponent_yielded``) that RESOLVES AS
``player_victory`` for reward/credit, and must NEVER reuse the player-side
``yielded`` label, nor the genuine walk-away ``abandoned_on_location_change``.

ENGINE-CHECKED CONFIRMATION (not pure-LLM-compliance — CLAUDE.md: the GM panel
is the lie detector): the narrator marks the opponent yielded via existing tool
state (``EncounterActor.withdrawn`` and/or ``opponents_disposition`` in
{``surrendered``, ``routed``}); the ENGINE then deterministically confirms a
yield iff there ARE opponent-side actors AND every one of them is withdrawn
(or ``opponents_disposition`` is a yield disposition). These tests construct that
post-narrator engine state directly and assert the engine — not prose —
records the victory.

RED-state expectation (before 59-31 ships):
* ``StructuredEncounter.opponent_yield_outcome`` does not exist → AttributeError.
* The post-turn sweep resolves an all-opponents-withdrawn encounter as
  ``opponent_withdrew`` (``_resolve_if_no_opponent_remains``) — NOT
  ``opponent_yielded``/player_victory, with no ``component="confrontation"``
  resolution event and no resolution-signal stamp.
* The location-change else-branch resolves it as
  ``abandoned_on_location_change``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ── fixtures / builders ─────────────────────────────────────────────────────


def _encounter(
    *,
    opponents: list[EncounterActor],
    players: list[EncounterActor] | None = None,
    opponents_disposition: str | None = None,
    win_condition: str = "dial_threshold",
    player_current: int = 2,
    opponent_current: int = 1,
    threshold: int = 8,
) -> StructuredEncounter:
    """A confrontation whose dials are BELOW threshold (no dial win) so the only
    possible resolution is the opponent-yield path — exactly the Lion case
    (zero beats, no threshold met)."""
    enc = StructuredEncounter(
        encounter_type="standoff",
        win_condition=win_condition,  # type: ignore[arg-type]
        player_metric=EncounterMetric(
            name="resolve", current=player_current, starting=0, threshold=threshold
        ),
        opponent_metric=EncounterMetric(
            name="menace", current=opponent_current, starting=0, threshold=threshold
        ),
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


@pytest.fixture
def captured_watcher_events(monkeypatch) -> Iterator[list[dict[str, Any]]]:
    """Capture every ``_watcher_publish`` call made on the narration-apply path,
    recording event_type, fields, component, and severity — the canonical
    confrontation-OTEL capture pattern (mirrors
    tests/magic/test_confrontation_evaluation_otel.py)."""
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    monkeypatch.setattr(narration_apply, "_watcher_publish", _capture)
    yield captured


def _yield_events(captured: list[dict]) -> list[dict]:
    """Confrontation-component events whose fields name the opponent-yield
    resolution. The Dev wires a ``component="confrontation"`` watcher event
    carrying ``resolution_label="opponent_yielded"`` (sibling of
    ``confrontation_resolved_on_location_change``)."""
    return [
        e
        for e in captured
        if e["component"] == "confrontation"
        and e["fields"].get("resolution_label") == "opponent_yielded"
    ]


# ── AC1 — opponent_yield_outcome() helper (Architect: "alongside ──────────────
#         dial_threshold_outcome()") ─────────────────────────────────────────


def test_all_opponents_withdrawn_returns_opponent_yielded() -> None:
    """Every side='opponent' actor withdrawn AND opponents exist → the helper
    returns the mechanical-truth label ``"opponent_yielded"``.

    Story 59-32 normalized this return from ``"player_victory"`` (the credit
    label) to ``"opponent_yielded"`` (the mechanical truth — the opponent
    yielded; it was never a kill). Credit still flows: the shared
    ``is_player_victory()`` classifier maps ``opponent_yielded`` → True. Behavior
    is preserved (production consumers None-check the return; ``enc.outcome`` is
    set independently by ``_resolve_opponent_yield``)."""
    enc = _encounter(opponents=[_lion(withdrawn=True)])
    assert enc.opponent_yield_outcome() == "opponent_yielded"


def test_opponents_disposition_surrendered_returns_opponent_yielded() -> None:
    """The B/X morale path sets ``opponents_disposition='surrendered'`` without
    necessarily flipping each actor's ``withdrawn``. A surrendered opponent is a
    yield regardless of the per-actor flag — mechanical-truth label
    ``"opponent_yielded"`` (Story 59-32 normalization; credit via classifier)."""
    enc = _encounter(opponents=[_lion(withdrawn=False)], opponents_disposition="surrendered")
    assert enc.opponent_yield_outcome() == "opponent_yielded"


def test_opponents_disposition_routed_returns_opponent_yielded() -> None:
    enc = _encounter(opponents=[_lion(withdrawn=False)], opponents_disposition="routed")
    assert enc.opponent_yield_outcome() == "opponent_yielded"


def test_active_opponent_remaining_returns_none() -> None:
    """One opponent still in the fight (not withdrawn, no yield disposition) →
    NOT a yield. Guards against firing while an Other is still standing."""
    enc = _encounter(
        opponents=[
            _lion(withdrawn=True),
            EncounterActor(name="The Wicked Witch", role="boss", side="opponent"),
        ]
    )
    assert enc.opponent_yield_outcome() is None


def test_no_opponent_actors_returns_none() -> None:
    """ADR-116 — a confrontation requires an Other. An encounter with no
    opponent-side actor at all cannot be an opponent-yield (there is nobody to
    yield). Must return None, never player_victory."""
    enc = _encounter(opponents=[])
    assert enc.opponent_yield_outcome() is None


def test_player_side_withdrawn_does_not_trigger_opponent_yield() -> None:
    """THE ASYMMETRY GUARD. The PLAYER withdrew (a loss/withdrawal) while the
    opponent stands — this is the player-side ``yielded`` path, never an
    opponent-yield. opponent_yield_outcome must return None so a player
    withdrawal is never miscredited as a victory."""
    enc = _encounter(
        players=[EncounterActor(name="Dorothy", role="lead", side="player", withdrawn=True)],
        opponents=[_lion(withdrawn=False)],
    )
    assert enc.opponent_yield_outcome() is None


def test_opponent_yield_outcome_is_win_condition_agnostic() -> None:
    """A monster surrendering mid-combat (hp_depletion win condition) is just as
    much an opponent yield as a yield in a dial confrontation. The opponent-yield
    check keys on actor/disposition state, not the dial win condition (unlike
    ``dial_threshold_outcome`` which is gated to dial_threshold).

    Story 59-32: returns the mechanical-truth label ``"opponent_yielded"`` (was
    ``"player_victory"``); credit still flows via ``is_player_victory()``."""
    enc = _encounter(
        opponents=[_lion(withdrawn=True)],
        win_condition="hp_depletion",
        threshold=1_000_000,
    )
    assert enc.opponent_yield_outcome() == "opponent_yielded"


# ── AC2 — same-turn resolution via the post-turn sweep (NO location change) ───


def test_opponent_yield_resolves_same_turn_without_location_change() -> None:
    """The Lion yielded same-turn in prose; the party did NOT move. The post-turn
    sweep inside ``_apply_narration_result_to_snapshot`` must resolve the
    encounter as ``opponent_yielded`` WITHOUT depending on a location change.

    RED: ``_resolve_if_no_opponent_remains`` currently records
    ``opponent_withdrew`` for this exact state."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=True)])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Lion's tail droops; he backs away whimpering.", beat_selections=[]
        ),
        player_name="Dorothy",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "opponent_yielded", (
        "an opponent backing down with no dial threshold met is a player VICTORY "
        "recorded as 'opponent_yielded' — not 'opponent_withdrew', not the "
        f"player-side 'yielded', not abandoned. got outcome={enc.outcome!r}"
    )


def test_opponent_surrender_disposition_resolves_same_turn() -> None:
    """opponents_disposition='surrendered' (morale path) likewise resolves
    opponent_yielded on the post-turn sweep, even though the per-actor
    ``withdrawn`` flag was never set."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(
        opponents=[_lion(withdrawn=False)], opponents_disposition="surrendered"
    )

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Lion throws up his paws in surrender.", beat_selections=[]
        ),
        player_name="Dorothy",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "opponent_yielded"


def test_genuinely_unfinished_encounter_not_resolved_as_yield_same_turn() -> None:
    """Over-fire guard. An opponent still standing (not withdrawn, no yield
    disposition), no dial threshold met → the post-turn sweep must NOT
    fabricate an opponent_yielded victory. The encounter stays unresolved."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=False)])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(narration="The Lion roars and stands his ground.", beat_selections=[]),
        player_name="Dorothy",
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.outcome is None, (
        "no opponent has yielded — the sweep must record NO outcome (not "
        "opponent_yielded, not opponent_withdrew, not any victory label); "
        f"got outcome={enc.outcome!r}"
    )
    assert enc.resolved is False


# ── AC3 — OTEL: a component='confrontation' resolution event (lie detector) ───


def test_opponent_yield_emits_confrontation_watcher_event(
    captured_watcher_events: list[dict],
) -> None:
    """CLAUDE.md OTEL principle. The opponent-yield resolution must emit a
    ``component='confrontation'`` watcher event so Keith can confirm on the GM
    panel that the ENGINE — not the narrator's prose — recorded the victory.
    Required attrs: outcome='player_victory', resolution_label='opponent_yielded',
    a trigger naming the sweep path, the yielded opponent names, and the
    opponents_disposition."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=True)])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(narration="The Lion yields.", beat_selections=[]),
        player_name="Dorothy",
        room=room_for(snap),
    )

    events = _yield_events(captured_watcher_events)
    assert len(events) == 1, (
        "exactly one confrontation opponent-yield resolution event must fire; "
        f"got {[(e['event_type'], e['fields']) for e in captured_watcher_events]}"
    )
    fields = events[0]["fields"]
    assert fields.get("outcome") == "player_victory", (
        "the credit-bearing outcome the engine records IS player_victory "
        f"(opponent_yielded resolves as a win); got {fields.get('outcome')!r}"
    )
    assert fields.get("resolution_label") == "opponent_yielded"
    assert fields.get("trigger") in {"opponent_yield_sweep", "opponent_yield_on_location_change"}
    assert "The Cowardly Lion" in (fields.get("yielded_opponents") or []), (
        f"yielded_opponents must name the actor(s) that yielded; got "
        f"{fields.get('yielded_opponents')!r}"
    )


def test_no_yield_event_when_opponent_still_active(
    captured_watcher_events: list[dict],
) -> None:
    """The lie detector must stay silent when nobody yielded — an active
    opponent produces zero opponent-yield resolution events."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=False)])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Lion paces, still blocking the road.", beat_selections=[]
        ),
        player_name="Dorothy",
        room=room_for(snap),
    )

    assert _yield_events(captured_watcher_events) == []


# ── AC4 — location-change boundary: yield wins, genuine walk-away abandons ────


def test_location_change_with_opponent_yield_resolves_victory_not_abandoned(
    snapshot_with_pack,
    character_named_sam,
) -> None:
    """The residual #576 punted: at a location change with NO dial threshold met,
    the else-branch falls to ``abandoned_on_location_change``. If the opponent
    has YIELDED, that branch must instead resolve ``opponent_yielded``
    (player_victory) — the party leaving an opponent who already backed down is
    a WIN, not a walk-away.

    RED: today this records ``abandoned_on_location_change``."""
    snap, pack = snapshot_with_pack
    snap.character_locations["Dorothy"] = "The Yellow Brick Road — The Crossing"
    snap.characters.append(character_named_sam)
    snap.encounter = _encounter(
        players=[EncounterActor(name="Dorothy", role="lead", side="player")],
        opponents=[_lion(withdrawn=True)],
    )
    assert snap.encounter.resolved is False

    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="With the Lion cowed, the party walks on down the road.",
            location="The Yellow Brick Road — Beyond the Crossing",
        ),
        pack=pack,
        player_name="Dorothy",
        room=room_for(snapshot=snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "opponent_yielded", (
        "a cowed opponent at a location change is a victory, not an abandonment; "
        f"got outcome={enc.outcome!r}"
    )


def test_location_change_emits_yield_trigger(
    snapshot_with_pack,
    character_named_sam,
    captured_watcher_events: list[dict],
) -> None:
    """The location-change opponent-yield resolution fires the confrontation
    event with trigger='opponent_yield_on_location_change' (distinct from the
    same-turn sweep trigger). This is the new ``elif yield_outcome`` branch
    introduced by 59-31 — distinct from (not a reuse of) the #576 dial-win
    ``confrontation_resolved_on_location_change`` emit."""
    snap, pack = snapshot_with_pack
    snap.character_locations["Dorothy"] = "The Yellow Brick Road — The Crossing"
    snap.characters.append(character_named_sam)
    snap.encounter = _encounter(
        players=[EncounterActor(name="Dorothy", role="lead", side="player")],
        opponents=[_lion(withdrawn=True)],
    )

    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="They leave the trembling Lion behind.",
            location="The Yellow Brick Road — Beyond the Crossing",
        ),
        pack=pack,
        player_name="Dorothy",
        room=room_for(snapshot=snap),
    )

    events = _yield_events(captured_watcher_events)
    assert len(events) == 1
    assert events[0]["fields"].get("trigger") == "opponent_yield_on_location_change"


def test_location_change_genuinely_unfinished_still_abandons(
    snapshot_with_pack,
    character_named_sam,
) -> None:
    """The #576 guard must survive: an encounter with an ACTIVE opponent, no dial
    threshold met, no yield → a location change is still a genuine walk-away
    recorded as ``abandoned_on_location_change``. The yield shortcut must not
    swallow real abandonments."""
    snap, pack = snapshot_with_pack
    snap.character_locations["Dorothy"] = "The Yellow Brick Road — The Crossing"
    snap.characters.append(character_named_sam)
    snap.encounter = _encounter(
        players=[EncounterActor(name="Dorothy", role="lead", side="player")],
        opponents=[_lion(withdrawn=False)],
    )

    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="Dorothy edges away down a side path, the Lion still snarling.",
            location="The Yellow Brick Road — A Side Path",
        ),
        pack=pack,
        player_name="Dorothy",
        room=room_for(snapshot=snap),
    )

    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "abandoned_on_location_change", (
        "an opponent still standing means the party genuinely walked away; "
        f"got outcome={enc.outcome!r}"
    )


# ── AC5 — pending_resolution_signal stamped (49-5 inherits it for free) ───────


def test_opponent_yield_stamps_pending_resolution_signal() -> None:
    """Per the 59-31 design: stamp ``snapshot.pending_resolution_signal`` on the
    opponent-yield resolution (cheap, correct, matches the dial-threshold sweep)
    so that WHEN the dormant 49-5 [ENCOUNTER RESOLVED] threading revives, the
    opponent-yield close narrates correctly for free. The signal carries the
    ``opponent_yielded`` outcome and names the yielded opponent(s).

    RED: ``_resolve_if_no_opponent_remains`` does not stamp the signal."""
    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=True)])

    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(narration="The Lion yields.", beat_selections=[]),
        player_name="Dorothy",
        room=room_for(snap),
    )

    sig = snap.pending_resolution_signal
    assert sig is not None, "opponent-yield resolution must stamp a resolution signal"
    assert sig.outcome == "opponent_yielded"
    assert "The Cowardly Lion" in sig.yielded_actors, (
        f"the signal must name the yielded opponent(s); got {sig.yielded_actors!r}"
    )
    # 49-5 reads the full signal — pin the metric snapshot so a transposed or
    # empty construction can't slip through (_encounter() defaults: standoff,
    # player_current=2, opponent_current=1).
    assert sig.encounter_type == "standoff"
    assert sig.final_player_metric == 2
    assert sig.final_opponent_metric == 1


# ── WIRING TEST (CLAUDE.md "Every Test Suite Needs a Wiring Test") ────────────
# Every other test in this file monkeypatches `_watcher_publish` and/or hand-
# builds the snapshot. This one proves the opponent-yield resolution is wired
# end-to-end: driven from the real per-turn production entry
# (`_apply_narration_result_to_snapshot` with a real SessionRoom) and emitting
# through the REAL telemetry bridge (`publish_event` → OTEL synthetic span under
# SIDEQUEST_WATCHER_AS_SPANS=1, captured by the global-provider `otel_capture`
# exporter) — NO `_watcher_publish` stub. If the sweep call site were removed
# from the pipeline, or the watcher→OTEL bridge broke, this test fails where the
# stubbed tests would not. Uses CLAUDE.md "No Source-Text Wiring Tests" path 1
# (OTEL span assertion driven through the real flow).


def test_opponent_yield_resolution_wired_through_real_telemetry_pipeline(
    monkeypatch,
    otel_capture,
) -> None:
    monkeypatch.setenv("SIDEQUEST_WATCHER_AS_SPANS", "1")

    snap = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz")
    snap.encounter = _encounter(opponents=[_lion(withdrawn=True)])

    # Real per-turn production entry + real SessionRoom — no stubbed watcher.
    _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="The Lion backs away, tail between his legs.", beat_selections=[]
        ),
        player_name="Dorothy",
        room=room_for(snap),
    )

    # 1) The engine resolved through the real pipeline.
    enc = snap.encounter
    assert enc is not None
    assert enc.resolved is True
    assert enc.outcome == "opponent_yielded"

    # 2) The lie-detector span reached the REAL OTEL pipeline (not a monkeypatch).
    yield_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "watcher.confrontation_resolved_on_opponent_yield"
    ]
    assert len(yield_spans) == 1, (
        "the opponent-yield resolution must emit exactly one "
        "watcher.confrontation_resolved_on_opponent_yield span through the real "
        f"publish_event→OTEL bridge; got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(yield_spans[0].attributes or {})
    assert attrs.get("watcher.component") == "confrontation"
    assert attrs.get("field.resolution_label") == "opponent_yielded"
    assert attrs.get("field.outcome") == "player_victory"
    assert attrs.get("field.trigger") == "opponent_yield_sweep"
