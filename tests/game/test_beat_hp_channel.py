"""Unit + wiring tests for apply_beat_hp_channel (ADR-114 §2, §6).

Step 1: pure-helper unit tests + OTEL span wiring test.
The pure helper is imported from beat_kinds; the span fires on every real
HP delta and must carry field="hp" on the state_patch route.
"""

from __future__ import annotations

from sidequest.game.beat_kinds import apply_beat_hp_channel
from sidequest.game.creature_core import CreatureCore, HpPool


def _core(hp: int) -> CreatureCore:
    return CreatureCore(
        name="t",
        description="x",
        personality="x",
        hp=HpPool(current=hp, max=hp, base_max=hp),
    )


# ---------------------------------------------------------------------------
# Pure-helper unit tests
# ---------------------------------------------------------------------------


def test_strike_subtracts_damage_total_minus_mitigation():
    target = _core(10)
    applied = apply_beat_hp_channel(
        target=target, channel="strike", damage_total=6, target_mitigation=2
    )
    assert applied == 4
    assert target.hp.current == 6


def test_strike_floors_at_zero():
    target = _core(3)
    apply_beat_hp_channel(
        target=target, channel="strike", damage_total=99, target_mitigation=0
    )
    assert target.hp.current == 0


def test_mitigation_never_makes_a_strike_heal():
    target = _core(10)
    applied = apply_beat_hp_channel(
        target=target, channel="strike", damage_total=1, target_mitigation=5
    )
    assert applied == 0
    assert target.hp.current == 10


def test_brace_is_noop_on_hp():
    target = _core(10)
    assert (
        apply_beat_hp_channel(
            target=target, channel="brace", damage_total=6, target_mitigation=0
        )
        == 0
    )
    assert target.hp.current == 10


# ---------------------------------------------------------------------------
# OTEL span wiring test (ADR-114 §6 lie-detector: Step 6)
# ---------------------------------------------------------------------------


def test_strike_emits_state_patch_hp_span(otel_capture):
    """apply_beat_hp_channel must emit a span with name ``state_patch.hp``
    (routed through state_patch route) carrying field="hp" when a real
    HP delta fires.

    Uses the ``otel_capture`` fixture (InMemorySpanExporter) from
    tests/game/conftest.py — same pattern as test_tension_tracker_otel_wiring.py.
    """
    target = _core(10)
    apply_beat_hp_channel(
        target=target,
        channel="strike",
        damage_total=5,
        target_mitigation=0,
        source_beat_id="test_beat",
    )

    spans = otel_capture.get_finished_spans()
    hp_spans = [s for s in spans if s.name == "state_patch.hp"]
    assert hp_spans, (
        f"expected a state_patch.hp span; got span names: {[s.name for s in spans]}"
    )
    attrs = hp_spans[0].attributes or {}
    assert attrs.get("field") == "hp", f"expected field='hp', got {attrs.get('field')!r}"
    assert attrs.get("actor") == "t"
    assert attrs.get("delta") == -5
    assert attrs.get("current") == 5
    assert attrs.get("maximum") == 10
    assert attrs.get("source") == "test_beat"
