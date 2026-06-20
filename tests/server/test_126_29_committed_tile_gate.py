"""RED tests (Story 126-29) — project a per-participant ``committed`` bool into
FATE_STATE so the UI can pre-disable the proactive tiles on a RESUMED mid-exchange
Fate conflict, instead of offering actions the server's sealed-commit guard then
rejects (#403).

The bug: ``FateConflictParticipant`` carries only the opponent track today — it does
NOT say whether a participant has already sealed an action this exchange. The client's
"already committed" signal (``sealedWaiting``) is transient and does NOT survive a
reconnect, so after a resume the UI re-offers Overcome/Create-Advantage/Attack/Concede
and the server correctly rejects the throw (``seal_fate_commit`` raises
``FateConflictError``). The fix surfaces the server-authoritative commit ledger
(``encounter.fate_commits``) onto the wire as ``participant.committed`` so the surface
gates on a resume-safe datum.

Per ADR-129/151 the server sealed-commit guard is the AUTHORITATIVE enforcement and
MUST stay fail-loud — this story only adds a legibility projection in front of it, it
does NOT relax it (``test_seal_fate_commit_guard_still_rejects_double_commit`` pins
that, and is GREEN by design — a regression tripwire, not a RED-new-behavior test).

New production symbols are imported INSIDE each test so every gap reports cleanly
(mirrors tests/server/test_fate_opponent_projection.py).
"""

from __future__ import annotations

import pytest

from sidequest.game.encounter import FateSealedCommit, StructuredEncounter
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.game.session import GameSnapshot
from tests._helpers.fate_fixtures import conflict_with_pc_and_npc

# ---------------------------------------------------------------------------
# Helpers — mirror test_fate_opponent_projection.py so the emit path is exercised
# through the real ``_maybe_emit_fate_state`` rather than a stub.
# ---------------------------------------------------------------------------


def _sd(ruleset: str = "fate"):
    from types import SimpleNamespace

    return SimpleNamespace(genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset=ruleset)))


class _Sink:
    def __init__(self):
        self.sent: list[tuple[object, str]] = []

    def __call__(self, msg, kind):
        self.sent.append((msg, kind))


class _Handler:
    pass


def _participant(conflict, name: str):
    return next(p for p in conflict.participants if p.name == name)


def _commit(actor: str, *, action: str = "overcome", skill: str = "Athletics") -> FateSealedCommit:
    """One sealed proactive action on the exchange ledger — the server-authoritative
    'this actor has already acted this exchange' record the projection reads."""
    return FateSealedCommit(actor=actor, action=action, skill=skill)


# ---------------------------------------------------------------------------
# AC1 / AC4 — the participant carries a ``committed`` bool sourced from fate_commits.
# ---------------------------------------------------------------------------


def test_fresh_conflict_projects_all_participants_uncommitted():
    """AC4(a): a freshly seated conflict (empty ``fate_commits``) projects every
    participant with ``committed=False`` — nobody has sealed an action yet."""
    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    assert enc.fate_commits == []  # precondition: nobody committed

    payload = build_fate_state_payload(snap)

    assert _participant(payload.conflict, "Rux").committed is False
    assert _participant(payload.conflict, "Bandit").committed is False


def test_committed_pc_participant_projects_true():
    """AC1 / AC4(b): after a PC's action is recorded in ``fate_commits``, THAT
    participant projects ``committed=True`` while the un-acted opponent stays False —
    the bool is per-actor, read straight off the sealed-commit ledger."""
    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    enc.fate_commits.append(_commit("Rux"))

    payload = build_fate_state_payload(snap)

    assert _participant(payload.conflict, "Rux").committed is True
    assert _participant(payload.conflict, "Bandit").committed is False


def test_committed_is_per_actor_not_player_only():
    """The committed bool reads the ledger for ANY seated actor, not just PCs — an
    opponent the F2 narrator has sealed projects committed=True too (guards against a
    fix that only flags player-side participants)."""
    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    enc.fate_commits.append(_commit("Bandit", action="attack", skill="Fight"))

    payload = build_fate_state_payload(snap)

    assert _participant(payload.conflict, "Bandit").committed is True
    assert _participant(payload.conflict, "Rux").committed is False


def test_committed_survives_encounter_resume_roundtrip():
    """AC4(d): the committed bool is resume-safe. The sealed-commit ledger rides
    ``snapshot.encounter`` (ADR-128), so a model dump/validate round-trip (the
    persistence path a reconnect replays through) preserves it — the projection of
    the REHYDRATED encounter still reports committed=True. This is the actual #403
    bug: the commit survives the resume; only its projection to the wire was missing."""
    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    enc.fate_commits.append(_commit("Rux"))

    # Reconstruct the encounter exactly as a resume would (dump -> validate), then
    # rebuild a snapshot around the rehydrated encounter (same PCs/NPCs).
    rehydrated = StructuredEncounter.model_validate(enc.model_dump())
    assert any(c.actor == "Rux" for c in rehydrated.fate_commits)  # ledger survived
    resumed = GameSnapshot(
        genre_slug=snap.genre_slug,
        characters=snap.characters,
        encounter=rehydrated,
    )
    resumed.npcs.extend(snap.npcs)

    payload = build_fate_state_payload(resumed)

    assert _participant(payload.conflict, "Rux").committed is True


# ---------------------------------------------------------------------------
# AC2 — the emitter broadcasts the committed status to the seats.
# ---------------------------------------------------------------------------


def test_emit_broadcasts_committed_status_on_the_wire():
    """AC2: the reactive FATE_STATE broadcast carries ``committed`` per participant so
    every seat can read it. Drive the real ``_maybe_emit_fate_state`` and inspect the
    sent payload (behavioral wiring, not source-text)."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    enc.fate_commits.append(_commit("Rux"))
    sink = _Sink()

    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=snap, emit_fn=sink)

    assert len(sink.sent) == 1
    msg, kind = sink.sent[0]
    assert kind == "FATE_STATE"
    assert _participant(msg.payload.conflict, "Rux").committed is True
    assert _participant(msg.payload.conflict, "Bandit").committed is False


# ---------------------------------------------------------------------------
# AC6 — OTEL: the projection span confirms committed status reached the wire.
# ---------------------------------------------------------------------------


def test_conflict_projected_span_carries_committed_status(otel_capture):
    """AC6: the GM-panel lie detector. When a conflict with a committed participant is
    broadcast, ``fate.conflict.projected`` carries the committed count (and the actor
    names) so the GM panel can verify the gate is backed by real ledger state, not
    narrator improvisation. The span exists today (opponent-track only); this pins the
    committed extension."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state
    from sidequest.telemetry.spans.fate import SPAN_FATE_CONFLICT_PROJECTED

    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    enc.fate_commits.append(_commit("Rux"))
    sink = _Sink()

    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=snap, emit_fn=sink)

    spans = {s.name: s for s in otel_capture.get_finished_spans()}
    assert SPAN_FATE_CONFLICT_PROJECTED in spans
    attrs = spans[SPAN_FATE_CONFLICT_PROJECTED].attributes or {}
    assert int(attrs.get("committed_count")) == 1
    assert "Rux" in str(attrs.get("committed_actors"))


# ---------------------------------------------------------------------------
# AC5 — REGRESSION TRIPWIRE (green by design): the server guard is NOT relaxed.
# ---------------------------------------------------------------------------


def test_seal_fate_commit_guard_still_rejects_double_commit():
    """AC5 (ADR-129/151): this story is a legibility projection IN FRONT OF the guard,
    never a relaxation of it. The sealed-commit guard must still reject a second
    proactive action from an already-committed actor, loudly. This test is GREEN now
    and must STAY green — a tripwire so a future change can't quietly soften the one
    proactive-action-per-participant-per-exchange invariant."""
    from sidequest.server.dispatch.fate_conflict import FateConflictError, seal_fate_commit

    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    rux = next(a for a in enc.actors if a.side == "player")

    seal_fate_commit(encounter=enc, actor=rux, action="overcome", skill="Athletics")
    assert any(c.actor == "Rux" for c in enc.fate_commits)

    with pytest.raises(FateConflictError):
        seal_fate_commit(encounter=enc, actor=rux, action="attack", skill="Fight")
