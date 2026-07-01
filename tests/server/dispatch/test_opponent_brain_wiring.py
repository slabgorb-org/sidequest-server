"""RED wiring + OTEL tests — Story 158-39 — dogfight opponent brain at the seam (ADR-153 §4).

The pure policy is proven in ``tests/game/test_dogfight_brain.py``. This module
proves the brain is WIRED into the real sealed-letter resolution path and is
observable on the GM panel (the lie-detector), driving the production
``_apply_narration_result_to_snapshot`` seam — never a source grep (CLAUDE.md
"No Source-Text Wiring Tests").

Contract under test (TEA-defined for Dev):

* **Fallback fires (AC-3/AC-5):** when the narrator commits ONLY the player's
  (red) maneuver and omits the opponent's (blue), the seam must substitute a
  legal disposition-weighted maneuver instead of raising the current
  ``ValueError("committed maneuvers missing 'blue' key ...")`` (sealed_letter.py).
  The duel resolves; it never wedges (ADR-006 graceful degradation — the floor).
* **The pick is loud (AC-4):** a ``dogfight.maneuver_committed`` span fires for
  the blue role carrying ``source="fallback"`` and the motivating ``attitude`` —
  the GM panel can tell the engine chose the maneuver, not the narrator.
* **Narrator path stays sourced "narrator" (AC-4):** when the narrator DOES commit
  a legal blue maneuver, the committed span is sourced ``"narrator"`` — the two
  stances are distinguishable on the lie-detector.
* **Never wedges (AC-5):** across several blue-omitted turns the opponent commits
  every turn and energy never goes negative — a hostile ace stays in the fight.

RED shape: today a missing blue commit raises before any fallback, and the
committed span carries no ``source``/``attitude``. Dev (158-39 GREEN) wires the
gate + fallback at the seam and stamps the span.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition
from sidequest.game.session import Npc
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.telemetry.spans import SPAN_ROUTES
from sidequest.telemetry.spans.dogfight import SPAN_DOGFIGHT_MANEUVER_COMMITTED
from tests._helpers.session_room import room_for
from tests.fixtures.dogfight_playtest_encounter import (
    drive_dogfight_turn,
    make_dogfight_playtest_state,
)

_OPPONENT = "Vulture"


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Per-test in-memory OTEL exporter. Mirrors the established pattern
    (tests/telemetry/spans/test_dogfight_shot_spans.py): monkeypatch the module
    tracer so the global provider is untouched and every span the seam emits via
    the global ``tracer()`` is captured."""
    from sidequest.telemetry import spans as spans_module

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    test_tracer = provider.get_tracer("test")
    monkeypatch.setattr(spans_module, "tracer", lambda: test_tracer)
    return exp


def _seat_hostile_opponent(snap, name: str = _OPPONENT) -> None:
    """Ensure a hostile opponent NPC named ``name`` is on the snapshot, so the
    seam resolves ``attitude == "hostile"`` for the blue actor's disposition."""
    snap.npcs = [n for n in snap.npcs if n.core.name != name]
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name=name,
                description="enemy strike-fighter ace",
                personality="ruthless",
            ),
            disposition=Disposition(-40),  # value < -10 → Attitude.HOSTILE
        )
    )


def _commit_red_only(snap, pack, red_maneuver: str):
    """Drive one turn where the narrator committed ONLY the red (player) maneuver
    — blue omitted. Calls the REAL apply seam (``drive_dogfight_turn`` refuses a
    missing blue by design, so we build the NarrationTurnResult directly)."""
    red = next(a for a in snap.encounter.actors if a.role == "red")
    return _apply_narration_result_to_snapshot(
        snap,
        NarrationTurnResult(
            narration="Guns hot — a hard pass.",
            beat_selections=[BeatSelection(actor=red.name, beat_id=red_maneuver)],
        ),
        player_name=red.name,
        room=room_for(snap),
        pack=pack,
    )


def _blue_committed_spans(exporter: InMemorySpanExporter) -> list:
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name.endswith("maneuver_committed")
        and (s.attributes or {}).get("role") == "blue"
    ]


# ---------------------------------------------------------------------------
# Fallback fires + is loud (AC-3 / AC-4 / AC-5)
# ---------------------------------------------------------------------------


def test_missing_blue_maneuver_falls_back_to_disposition_pick(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Narrator omits blue → the seam substitutes a legal disposition-weighted
    maneuver and the duel resolves (no ``missing 'blue' key`` ValueError), with a
    blue committed span sourced ``fallback`` carrying the hostile attitude."""
    snap, _cdef, pack = make_dogfight_playtest_state(opponent_pilot_name=_OPPONENT)
    _seat_hostile_opponent(snap)

    outcome = _commit_red_only(snap, pack, red_maneuver="straight")

    # Reaching here at all means the seam did NOT raise the missing-blue error:
    # the opponent brain supplied a legal blue maneuver (ADR-153 §4 floor).
    assert outcome.sealed_letter is not None, (
        "sealed-letter resolution did not run — the blue fallback was not supplied "
        "before resolve_sealed_letter_lookup"
    )

    blue_spans = _blue_committed_spans(span_exporter)
    assert blue_spans, "no blue maneuver_committed span — the fallback pick was silent"
    assert any((s.attributes or {}).get("source") == "fallback" for s in blue_spans), (
        "no blue committed span with source='fallback' — the GM panel cannot tell "
        "the engine (not the narrator) chose the opponent's maneuver"
    )
    assert any((s.attributes or {}).get("attitude") == "hostile" for s in blue_spans), (
        "the fallback span did not carry the motivating attitude='hostile'"
    )


def test_narrator_committed_blue_is_sourced_narrator(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When the narrator DOES commit a legal blue maneuver, the committed span is
    sourced ``narrator`` — distinguishable from the engine fallback on the
    lie-detector (AC-4). This is the common path and must not regress."""
    snap, _cdef, pack = make_dogfight_playtest_state(opponent_pilot_name=_OPPONENT)

    drive_dogfight_turn(snap, red_maneuver="straight", blue_maneuver="bank", pack=pack)

    blue_spans = _blue_committed_spans(span_exporter)
    assert blue_spans, "no blue maneuver_committed span on the narrator path"
    assert any((s.attributes or {}).get("source") == "narrator" for s in blue_spans), (
        "a narrator-committed blue maneuver must be sourced 'narrator' so the GM "
        "panel can distinguish it from the disposition fallback"
    )


def test_opponent_never_wedges_across_blue_omitted_turns(
    span_exporter: InMemorySpanExporter,
) -> None:
    """AC-5: over several turns where the narrator omits blue every time, the
    opponent commits a legal maneuver each turn, the duel keeps resolving (never a
    wedge), and energy never goes negative (the affordability gate holds)."""
    snap, _cdef, pack = make_dogfight_playtest_state(opponent_pilot_name=_OPPONENT)
    _seat_hostile_opponent(snap)

    turns_driven = 0
    for red in ("straight", "bank", "loop"):
        if snap.encounter is None or snap.encounter.resolved:
            break
        outcome = _commit_red_only(snap, pack, red_maneuver=red)
        assert outcome.sealed_letter is not None, f"turn {turns_driven + 1} did not resolve"
        turns_driven += 1

        blue = next(
            (a for a in snap.encounter.actors if a.role == "blue"), None
        ) if snap.encounter else None
        if blue is not None:
            for key, val in blue.per_actor_state.items():
                if "energy" in key and isinstance(val, (int, float)):
                    assert val >= 0, f"opponent {key} went negative ({val}) — gate breached"

    assert turns_driven >= 1, "no blue-omitted turn resolved — the duel wedged immediately"
    blue_fallbacks = [
        s for s in _blue_committed_spans(span_exporter)
        if (s.attributes or {}).get("source") == "fallback"
    ]
    assert len(blue_fallbacks) >= turns_driven, (
        f"expected a fallback committed span for each of {turns_driven} blue-omitted "
        f"turns, saw {len(blue_fallbacks)}"
    )


# ---------------------------------------------------------------------------
# Lie-detector surface — the committed span reaches the GM panel
# ---------------------------------------------------------------------------


def test_maneuver_committed_span_is_routed_for_gm_panel() -> None:
    """The committed span must be routed under component=dogfight so the GM panel
    receives a typed event — otherwise ``source``/``attitude`` never reach the
    lie-detector even when stamped."""
    assert SPAN_DOGFIGHT_MANEUVER_COMMITTED in SPAN_ROUTES, (
        f"{SPAN_DOGFIGHT_MANEUVER_COMMITTED} missing from SPAN_ROUTES — the GM panel "
        "will never see the opponent's committed maneuver"
    )
    assert SPAN_ROUTES[SPAN_DOGFIGHT_MANEUVER_COMMITTED].component == "dogfight"
