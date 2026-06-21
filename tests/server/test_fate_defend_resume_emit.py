"""RED — re-emit pending DEFEND requests on reconnect (Story 153-7, ADR-151).

The DEFEND barrier (ADR-151) emits one ``FATE_DEFEND_REQUEST`` per incoming attack
*once*, when the round PARKS. A targeted player who disconnects mid-DEFEND never
sees the request again on reconnect: the connect/resume bootstrap re-emits
FATE_STATE / PARTY_STATUS / LOCATION_DESCRIPTION / MAP_UPDATE (see ``connect.py``)
but NOTHING re-emits the pending defend, so the defender can't throw, the ledger
never fills, and ``resume_fate_exchange`` never fires — the round wedges forever.

These pin the unit contract for the re-emit helper, mirroring
``test_fate_state_emit.py`` (the sibling resume re-emitter): a small pure function
that, for a Fate pack only, re-emits a ``FateDefendRequestMessage`` per UNFILLED
pending_defenses entry belonging to the reconnecting defender — read-only on the
ledger (no re-roll, ADR-128), addressed to the reconnecting player, and observable
via a ``fate.defend_phase`` lie-detector span (CLAUDE.md OTEL principle).

All FAIL today: ``sidequest.server.websocket_handlers.fate_defend_resume`` does
not exist (RED). The helper is imported inside each test so every gap reports
cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from tests._helpers.fate_fixtures import (
    parked_conflict,
    parked_conflict_filled,
    parked_conflict_two_defenders,
)


def _sd(ruleset: str = "fate"):
    """A session-data stand-in exposing the gate the emitter reads:
    ``sd.genre_pack.rules.ruleset`` (mirrors ``test_fate_state_emit._sd``)."""
    return SimpleNamespace(genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset=ruleset)))


class _Sink:
    """Captures broadcast messages + the kind tag (mirrors the fate_state sink)."""

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    def __call__(self, msg, kind):  # noqa: ANN001
        self.sent.append((msg, kind))


@pytest.fixture
def otel_capture() -> Iterator:
    """Yield an InMemorySpanExporter wired to the GLOBAL tracer the helper emits
    through (``fate_defend_phase_span`` is called without an explicit ``_tracer``).
    Mirrors ``tests/server/test_fate_concede_wire.py``'s fixture."""
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


# ---------------------------------------------------------------------------
# AC-1 — happy path: a parked, unfilled entry for the reconnecting defender is
# re-emitted as a FATE_DEFEND_REQUEST carrying the locked attack data.
# ---------------------------------------------------------------------------


def test_reemits_unfilled_request_for_reconnecting_defender():
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    from sidequest.protocol.messages import FateDefendRequestMessage

    snap, _enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1", attack_total=4)
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"),
        snapshot=snap,
        defender_name="Rux",
        player_id="p1",
        emit_fn=sink,
    )

    assert len(sink.sent) == 1
    msg, kind = sink.sent[0]
    assert kind == "FATE_DEFEND_REQUEST"
    assert isinstance(msg, FateDefendRequestMessage)
    assert msg.payload.request_id == "d1"
    assert msg.payload.defender == "Rux"
    assert msg.payload.attacker == "Bandit"
    assert msg.payload.attack_total == 4  # the LOCKED attack total, not re-derived
    assert msg.payload.attack_skill == "Fight"


def test_reemitted_message_is_addressed_to_the_reconnecting_player():
    """The re-emit lands on the reconnecting socket — the message must carry that
    player_id so the broadcast/queue layer routes it to them (MP correctness)."""
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, _enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert sink.sent[0][0].player_id == "p1"


# ---------------------------------------------------------------------------
# AC-2 — negatives: nothing spurious. Filled, conceded, or other-defender
# entries must NOT re-prompt the reconnecting player.
# ---------------------------------------------------------------------------


def test_skips_already_filled_entry():
    """A defender who already threw (defense_total recorded) must not be re-prompted
    — the barrier isn't waiting on them."""
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, _enc = parked_conflict_filled(defender="Rux", attacker="Bandit", request_id="d1")
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert sink.sent == []


def test_skips_conceded_entry():
    """A conceded entry is resolved (fold) — re-prompting it would let a folded
    defender throw a second time."""
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    enc.pending_defenses[0].conceded = True
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert sink.sent == []


def test_skips_other_defenders_entries():
    """Only the reconnecting player's own pending defends re-emit — a different
    seat's unfilled entry must not leak onto this socket (ADR-105 perception)."""
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    # Two unfilled entries: Rux (d1) and Vala (d2). Only Rux reconnects.
    snap, _enc = parked_conflict_two_defenders(
        defenders=("Rux", "Vala"), attackers=("Bandit", "Brigand"), request_ids=("d1", "d2")
    )
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert len(sink.sent) == 1
    assert sink.sent[0][0].payload.defender == "Rux"
    assert sink.sent[0][0].payload.request_id == "d1"


# ---------------------------------------------------------------------------
# AC-2 (the gate) — never collide with the WN/native overlay. A non-Fate pack
# must NEVER re-emit a Fate defend request, even with pending_defenses present.
# ---------------------------------------------------------------------------


def test_gated_off_for_without_number_pack():
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, _enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("wwn"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert sink.sent == []


def test_gated_off_for_dial_pack():
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, _enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("dial"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert sink.sent == []


# ---------------------------------------------------------------------------
# AC-1 (resume-safety, ADR-128) — re-emit is READ-ONLY on the ledger: it never
# re-rolls, re-adds, fills, or concedes an entry. Reconnect must be idempotent.
# ---------------------------------------------------------------------------


def test_reemit_does_not_mutate_the_ledger():
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1", attack_total=4)
    before = enc.pending_defenses[0].model_copy(deep=True)
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    # Same single entry, still unfilled and unconceded — a re-emit informs, it
    # never advances the exchange state.
    assert len(enc.pending_defenses) == 1
    after = enc.pending_defenses[0]
    assert after.defense_total is None
    assert after.conceded is False
    assert after.attack_total == before.attack_total


def test_idempotent_across_repeated_reconnects():
    """Two reconnects (a reconnect-of-a-reconnect) each re-prompt once — the ledger
    never grows, so the defender is informed but the barrier state is stable."""
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    a, b = _Sink(), _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=a
    )
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=b
    )
    assert len(a.sent) == 1 and len(b.sent) == 1
    assert len(enc.pending_defenses) == 1


# ---------------------------------------------------------------------------
# AC — empty / absent encounter is a silent no-op (the blessed default, ADR-151:
# a legacy encounter with no ledger ⇒ nothing parked ⇒ nothing to re-emit).
# ---------------------------------------------------------------------------


def test_no_encounter_is_silent_noop():
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    from sidequest.game.session import GameSnapshot

    snap = GameSnapshot(genre_slug="pulp_noir")  # no encounter at all
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert sink.sent == []


def test_empty_ledger_is_silent_noop():
    """An encounter with an empty pending_defenses ledger (nothing parked) emits
    nothing — no spurious prompt on a routine reconnect mid-conflict."""
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    enc.pending_defenses.clear()
    sink = _Sink()
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=sink
    )
    assert sink.sent == []


# ---------------------------------------------------------------------------
# AC-4 — OTEL lie detector: the re-emit fires a fate.defend_phase span tagged as a
# reconnect re-emit so the GM panel can verify the wedge unblocked (not improvised).
# ---------------------------------------------------------------------------


def test_reemit_emits_reconnect_defend_phase_span(otel_capture):
    from sidequest.server.websocket_handlers.fate_defend_resume import (
        _maybe_reemit_pending_defenses,
    )

    snap, _enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1")
    _maybe_reemit_pending_defenses(
        sd=_sd("fate"), snapshot=snap, defender_name="Rux", player_id="p1", emit_fn=_Sink()
    )

    spans = otel_capture.get_finished_spans()
    reemit = [
        s
        for s in spans
        if s.name == "fate.defend_phase"
        and s.attributes.get("reason") == "reconnect_reemit"
        and s.attributes.get("responded") is False
    ]
    assert reemit, (
        "expected a fate.defend_phase span tagged reason=reconnect_reemit "
        f"(the GM-panel lie detector); got {[s.name for s in spans]}"
    )
    assert reemit[0].attributes["defender"] == "Rux"
    assert reemit[0].attributes["request_id"] == "d1"
