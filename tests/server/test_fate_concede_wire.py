"""WIRE the defend-CONCESSION through the REAL FateThrowHandler (story 126-14).

The 126-8 DEFEND barrier built the whole concede MACHINERY at the dispatch layer
(``dispatch_fate_defense(conceded=...)``, the ``_resolve_attack`` fold branch, the
``ledger_full ... or p.conceded`` clause, ``FatePendingDefense.conceded``, and the
``fate.defend_phase`` span carrying ``conceded``) — but NOTHING in production ever
passes ``conceded=True``: ``_handle_defend`` neither reads a concede signal nor
forwards one, and ``FateThrowPayload`` has no concede field. These tests drive a
concede FATE_THROW through ``HANDLER.handle`` so the dead branch becomes live.

They deliberately drive the REAL handler + runtime exchange (server CLAUDE.md:
behavioral fixtures + span capture, never a source grep), and do NOT duplicate the
dispatch-layer concede / authorization tests in
``tests/server/dispatch/test_fate_defense_record.py`` (those already pass) — the
RED here is purely the missing protocol field + handler plumbing.

RED until 126-14 lands: ``_make_concede_throw`` raises at construction
(``FateThrowPayload`` has no ``concede`` field and ``face`` is still required), and
``_handle_defend`` never forwards ``conceded=True``.

Span test note: run serially if a parallel span-count flake appears
(``uv run pytest tests/server/test_fate_concede_wire.py -n0``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import sidequest.game.ruleset.fate_resolution as fate_resolution
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot
from sidequest.handlers.fate_throw import HANDLER as FATE_THROW_HANDLER
from sidequest.protocol.dice import ThrowParams
from sidequest.protocol.fate import FateThrowPayload
from sidequest.protocol.messages import (
    FateDefendRequestMessage,
    FateRollMessage,
    FateThrowMessage,
)
from tests._helpers.fate_session import playing_session_with_fate_conflict


def _throw_params() -> ThrowParams:
    return ThrowParams(velocity=(0.0, 4.0, -1.0), angular=(0.5, 0.5, 0.5), position=(0.5, 0.5))


def _drain(q) -> list:  # noqa: ANN001
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _make_concede_throw(session, *, request_id: str) -> FateThrowMessage:  # noqa: ANN001
    """A FATE_THROW that CONCEDES a defend request: action="defend" + concede=True,
    echoing the request_id, with NO faces (a concession does not roll).

    During RED this RAISES at construction — ``FateThrowPayload`` has no ``concede``
    field and ``face`` is still required — which is the intended missing-production
    failure for the concede-wire tests (mirrors ``_make_defend_throw``)."""
    pid = session._session_data.player_id
    return FateThrowMessage(
        payload=FateThrowPayload(
            request_id=request_id,
            action="defend",
            concede=True,
            throw_params=_throw_params(),
        ),
        player_id=pid,
    )


@pytest.fixture
def otel_capture() -> Iterator:
    """Yield an InMemorySpanExporter wired to the GLOBAL tracer the handler emits
    through (``dispatch_fate_defense`` is called without an explicit ``_tracer``)."""
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


def test_concede_through_handler_fills_ledger_and_resumes():
    # AC-2 + AC-6 e2e: NPC-attacks-PC -> FATE_DEFEND_REQUEST -> player CONCEDES
    # through the REAL handler -> ledger fills -> RESUME -> narrate exactly once.
    import asyncio

    session, proactive, q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", action="attack", target="Bandit"
    )
    sd = session._session_data
    asyncio.run(FATE_THROW_HANDLER.handle(session, proactive))
    req = next(m for m in _drain(q) if isinstance(m, FateDefendRequestMessage))
    enc = sd.snapshot.encounter
    assert len(enc.pending_defenses) == 1
    assert sd.orchestrator.calls == 0  # parked: no narration yet

    concede = _make_concede_throw(session, request_id=req.payload.request_id)
    asyncio.run(FATE_THROW_HANDLER.handle(session, concede))
    after = _drain(q)

    # Ledger cleared (resumed), narrator invoked EXACTLY once at RESOLVE.
    assert enc.pending_defenses == []
    assert sd.orchestrator.calls == 1
    # The conceding defender folded (the dead _resolve_attack concede branch fired).
    folded = enc.find_actor("Rux")
    assert folded is not None and folded.withdrawn is True
    # A concession is NOT a throw: no defender FATE_ROLL is broadcast for it.
    assert not [m for m in after if isinstance(m, FateRollMessage)]


def test_concede_through_handler_never_resolves_a_player_defense(monkeypatch):
    # AC-1/AC-2: a concession folds — the PLAYER-defense resolver
    # (``resolve_action_from_faces``, the physics-is-the-roll path) must NEVER run
    # for a conceding defender. NB: counting ``roll_4df`` would be WRONG here — at
    # RESUME the conceding PC's own proactive attack still resolves, so the NPC
    # legitimately server-rolls ITS defense via ``resolve_action`` (a different
    # path). We spy the player-faces resolver specifically.
    import asyncio

    from sidequest.game.ruleset.fate import FateRulesetModule

    session, proactive, q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", action="attack", target="Bandit"
    )
    asyncio.run(FATE_THROW_HANDLER.handle(session, proactive))
    req = next(m for m in _drain(q) if isinstance(m, FateDefendRequestMessage))

    calls = {"from_faces": 0}
    real = FateRulesetModule.resolve_action_from_faces

    def _spy(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
        calls["from_faces"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(FateRulesetModule, "resolve_action_from_faces", _spy)
    concede = _make_concede_throw(session, request_id=req.payload.request_id)
    asyncio.run(FATE_THROW_HANDLER.handle(session, concede))
    # The conceding player's defense is never resolved from faces (no player roll).
    assert calls["from_faces"] == 0


def test_concede_for_another_defenders_request_is_rejected():
    # AC-3: a concession is authorization-gated exactly like a defend throw — only
    # the entry's OWN defender may concede it. The seated PC (Rux) conceding a
    # DIFFERENT defender's request must fail loud (FateConflictError -> error msg),
    # leave that entry unfilled AND unconceded, and NOT resume.
    import asyncio

    from sidequest.game.encounter import FatePendingDefense

    session, proactive, q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", action="attack", target="Bandit"
    )
    sd = session._session_data
    asyncio.run(FATE_THROW_HANDLER.handle(session, proactive))
    _drain(q)
    enc = sd.snapshot.encounter
    # A second incoming attack targets a DIFFERENT PC (Vesper), not the seated actor.
    enc.pending_defenses.append(
        FatePendingDefense(
            request_id="d-vesper",
            attacker="Bandit",
            defender="Vesper",
            attack_skill="Fight",
            attack_total=4,
        )
    )

    concede = _make_concede_throw(session, request_id="d-vesper")
    out = asyncio.run(FATE_THROW_HANDLER.handle(session, concede))

    assert out, "a concede from the wrong defender must return an error, not silently pass"
    vesper = next(p for p in enc.pending_defenses if p.request_id == "d-vesper")
    assert vesper.conceded is False  # untouched — Rux cannot fold Vesper's defense
    assert vesper.defense_total is None
    assert sd.orchestrator.calls == 0  # no resume on a rejected concede


def test_concede_through_handler_emits_conceded_defend_phase_and_no_player_roll_span(otel_capture):
    # AC-5 (OTEL lie detector): a concession through the wire records a
    # fate.defend_phase span with conceded=True for the answered entry, and emits NO
    # player-thrown fate.action_resolved (defense) span — nothing was rolled.
    import asyncio

    session, proactive, q = playing_session_with_fate_conflict(
        actor="Rux", attacker_npc="Bandit", action="attack", target="Bandit"
    )
    asyncio.run(FATE_THROW_HANDLER.handle(session, proactive))
    req = next(m for m in _drain(q) if isinstance(m, FateDefendRequestMessage))
    concede = _make_concede_throw(session, request_id=req.payload.request_id)
    asyncio.run(FATE_THROW_HANDLER.handle(session, concede))

    spans = otel_capture.get_finished_spans()
    conceded_phase = [
        s
        for s in spans
        if s.name == "fate.defend_phase"
        and s.attributes.get("conceded") is True
        and s.attributes.get("responded") is True
    ]
    assert conceded_phase, f"expected a conceded defend_phase span; got {[s.name for s in spans]}"
    assert conceded_phase[0].attributes["defender"] == "Rux"

    player_defense_rolls = [
        s
        for s in spans
        if s.name == "fate.action_resolved"
        and s.attributes.get("role") == "defense"
        and s.attributes.get("source") == "player_thrown"
    ]
    assert not player_defense_rolls, (
        f"concede must not roll a player defense; got {player_defense_rolls}"
    )


def test_conceded_entry_survives_reload_and_resume_folds_defender(monkeypatch):
    # AC-4 (resume-safety, ADR-128): a conceded pending_defenses entry rides
    # snapshot.encounter across a restart, and RESUME reads the concession and folds
    # the defender WITHOUT re-rolling. (Engine pre-exists from 126-8; this pins the
    # concede path specifically across a model round-trip — a safety net for the
    # wire, complementing the dispatch-layer concede test.)
    from sidequest.server.dispatch.fate_conflict import (
        dispatch_fate_defense,
        resume_fate_exchange,
    )
    from tests._helpers.fate_fixtures import parked_conflict

    snap, enc = parked_conflict(defender="Rux", attacker="Bandit", request_id="d1", attack_total=4)
    ruleset = get_ruleset_module("fate")
    dispatch_fate_defense(
        encounter=enc,
        snapshot=snap,
        ruleset=ruleset,
        actor_name="Rux",
        request_id="d1",
        skill="",
        thrown_faces=(0, 0, 0, 0),
        conceded=True,
    )

    reloaded = GameSnapshot.model_validate_json(snap.model_dump_json())
    rentry = next(p for p in reloaded.encounter.pending_defenses if p.request_id == "d1")
    assert rentry.conceded is True  # the concession survives a restart

    calls = {"roll": 0}
    real = fate_resolution.roll_4df
    monkeypatch.setattr(
        fate_resolution,
        "roll_4df",
        lambda rng: calls.__setitem__("roll", calls["roll"] + 1) or real(rng),
    )
    resume_fate_exchange(
        encounter=reloaded.encounter,
        snapshot=reloaded,
        ruleset=ruleset,
        round_number=0,
    )
    folded = reloaded.encounter.find_actor("Rux")
    assert folded is not None and folded.withdrawn is True  # conceded -> folded at resume
    assert calls["roll"] == 0  # a conceded defender is never (re-)rolled at resume
