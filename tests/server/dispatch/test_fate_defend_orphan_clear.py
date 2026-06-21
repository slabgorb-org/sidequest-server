"""RED — clear orphaned pending_defenses so a wedged DEFEND barrier can resume
(Story 153-7, ADR-151).

The DEFEND barrier RESUMES only once EVERY ``pending_defenses`` entry is filled
(``defense_total`` set) or conceded — ``ledger_full = all(... or conceded)``. An
entry whose defender can never fill it (they withdrew from the conflict, or their
character left the table entirely) holds ``ledger_full`` False forever: the
exchange wedges, narration never fires.

``clear_orphaned_pending_defenses`` is the loud, bounded sweep that unwedges it: it
CONCEDES (never silently drops — the NPC's sealed attack still resolves against the
abandoned character) every UNFILLED entry whose defender is no longer a live seated
PC, emits a ``fate.defend_phase`` lie-detector span per clear, and returns the
cleared entries. Its load-bearing safety invariant: it must NEVER touch a
present-but-silent defender — a disconnected-yet-seated player is NOT orphaned
(re-emit reaches them on reconnect); only a withdrawn/absent defender is.

Behavioral fixtures + span capture, never a source grep (server CLAUDE.md). All
FAIL today: ``clear_orphaned_pending_defenses`` does not exist (RED).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from sidequest.game.ruleset import get_ruleset_module
from tests._helpers.fate_fixtures import (
    parked_conflict,
    parked_conflict_filled,
    parked_conflict_two_defenders,
)


@pytest.fixture
def otel_capture() -> Iterator:
    """InMemorySpanExporter on the GLOBAL tracer (the sweep emits with no explicit
    ``_tracer``). Mirrors ``tests/server/test_fate_concede_wire.py``."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _ledger_full(encounter) -> bool:  # noqa: ANN001
    """The exact RESUME gate from ``dispatch_fate_defense`` — every entry filled or
    conceded. The whole point of the sweep is to make this True."""
    return all((p.defense_total is not None or p.conceded) for p in encounter.pending_defenses)


# ---------------------------------------------------------------------------
# SAFETY INVARIANT — a present, unfilled defender is NEVER swept. This is the most
# load-bearing test: clearing a still-seated defender's entry would let an NPC hit
# a defending player undefended (No Silent Fallbacks / Agency).
# ---------------------------------------------------------------------------


def test_present_unfilled_defender_is_not_cleared():
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    assert enc.find_actor("Rux") is not None and not enc.find_actor("Rux").withdrawn

    cleared = clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    assert cleared == []  # nobody orphaned — Rux is present and merely silent
    entry = enc.pending_defenses[0]
    assert entry.conceded is False
    assert entry.defense_total is None


# ---------------------------------------------------------------------------
# AC-3 — a withdrawn defender's unfilled entry is conceded (the in-conflict orphan:
# they fled / were removed AFTER the barrier parked).
# ---------------------------------------------------------------------------


def test_withdrawn_defender_entry_is_conceded():
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    enc.find_actor("Rux").withdrawn = True  # left the conflict, entry now unfillable

    cleared = clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    assert [c.request_id for c in cleared] == ["d1"]
    assert enc.pending_defenses[0].conceded is True
    # Conceded, NOT deleted — the NPC's sealed attack still resolves at RESUME
    # against the abandoned character (no silent drop of a committed action).
    assert len(enc.pending_defenses) == 1


def test_absent_defender_entry_is_conceded():
    """A defender whose character left the table entirely (no longer in
    ``snapshot.characters``) is orphaned and swept."""
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    snap.characters = [c for c in snap.characters if c.core.name != "Rux"]  # Rux left

    cleared = clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    assert [c.request_id for c in cleared] == ["d1"]
    assert enc.pending_defenses[0].conceded is True


# ---------------------------------------------------------------------------
# AC-3 (mixed) — sweep is surgical: with one orphaned and one present defender,
# ONLY the orphan is conceded; the present defender's entry is untouched.
# ---------------------------------------------------------------------------


def test_only_orphaned_entry_cleared_present_one_untouched():
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict_two_defenders(
        defenders=("Rux", "Vala"), attackers=("Bandit", "Brigand"), request_ids=("d1", "d2")
    )
    enc.find_actor("Vala").withdrawn = True  # only Vala is orphaned

    cleared = clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    assert [c.request_id for c in cleared] == ["d2"]
    by_id = {p.request_id: p for p in enc.pending_defenses}
    assert by_id["d2"].conceded is True  # orphan swept
    assert by_id["d1"].conceded is False  # present defender untouched
    assert by_id["d1"].defense_total is None


# ---------------------------------------------------------------------------
# AC-3 — an already-filled entry is left alone even if its (former) defender is now
# orphaned: it is resolved, not blocking. Re-conceding it would corrupt a recorded
# defense.
# ---------------------------------------------------------------------------


def test_filled_entry_for_orphaned_defender_is_not_recleared():
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict_filled(
        defender="Rux", attacker="Bandit", request_id="d1", recorded_defense_total=2
    )
    enc.find_actor("Rux").withdrawn = True

    cleared = clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    assert cleared == []  # filled ⇒ not blocking ⇒ not swept
    entry = enc.pending_defenses[0]
    assert entry.defense_total == 2  # recorded defense preserved
    assert entry.conceded is False


# ---------------------------------------------------------------------------
# AC-3 (the payoff) — after sweeping the lone orphan, the RESUME gate is satisfied,
# so the exchange can resume instead of wedging.
# ---------------------------------------------------------------------------


def test_sweep_makes_ledger_resumable():
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    enc.find_actor("Rux").withdrawn = True
    assert _ledger_full(enc) is False  # wedged before the sweep

    clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    assert _ledger_full(enc) is True  # unwedged — resume_fate_exchange can now fire


def test_swept_orphan_resumes_and_folds_without_rerolling(monkeypatch):
    """End-to-end through the real RESUME: a swept (conceded) orphan resolves as a
    fold — the abandoned defender is taken out of the conflict — WITHOUT rolling any
    dice for them (ADR-128: a conceded defender is never rolled)."""
    import sidequest.game.ruleset.fate_resolution as fate_resolution
    from sidequest.server.dispatch.fate_conflict import (
        clear_orphaned_pending_defenses,
        resume_fate_exchange,
    )

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    enc.find_actor("Rux").withdrawn = True
    ruleset = get_ruleset_module("fate")

    clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)
    assert _ledger_full(enc) is True

    calls = {"roll": 0}
    real = fate_resolution.roll_4df
    monkeypatch.setattr(
        fate_resolution,
        "roll_4df",
        lambda rng: calls.__setitem__("roll", calls["roll"] + 1) or real(rng),
    )
    resume_fate_exchange(encounter=enc, snapshot=snap, ruleset=ruleset, round_number=0)

    assert enc.pending_defenses == []  # ledger cleared at resume
    # The conceded orphan was never rolled for (its defense is a fold, not a throw).
    assert calls["roll"] == 0


# ---------------------------------------------------------------------------
# AC-4 — OTEL lie detector: each orphan clear fires a fate.defend_phase span tagged
# orphaned so the GM panel sees the sweep happen (not a silent ledger edit).
# ---------------------------------------------------------------------------


def test_sweep_emits_orphaned_defend_phase_span(otel_capture):
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    enc.find_actor("Rux").withdrawn = True

    clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    spans = otel_capture.get_finished_spans()
    orphan = [
        s
        for s in spans
        if s.name == "fate.defend_phase"
        and s.attributes.get("reason") == "orphaned"
        and s.attributes.get("conceded") is True
    ]
    assert orphan, (
        "expected a fate.defend_phase span tagged reason=orphaned "
        f"(the GM-panel lie detector); got {[s.name for s in spans]}"
    )
    assert orphan[0].attributes["defender"] == "Rux"
    assert orphan[0].attributes["request_id"] == "d1"


def test_sweep_with_no_orphans_emits_no_span(otel_capture):
    """A clean sweep (everyone present) is silent — no orphan span, no noise on the
    GM panel for the common reconnect where nobody actually left."""
    from sidequest.server.dispatch.fate_conflict import clear_orphaned_pending_defenses

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    clear_orphaned_pending_defenses(encounter=enc, snapshot=snap)

    orphan = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "fate.defend_phase" and s.attributes.get("reason") == "orphaned"
    ]
    assert orphan == []
