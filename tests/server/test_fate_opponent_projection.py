"""RED tests — project the opponent FateSheet + a conflict-progress metric into
FATE_STATE (playtest 150-2 server follow-up).

PR #431 styled the Fate Conflict surface but carved OUT the opponent stress track
and the win-condition meter because the data was never on the wire:
``FateConflictEntry.participants`` carried only ``{name, side}``. The opponent's
``core.fate_sheet`` exists server-side (seeded by #966) but never projected.

This slice puts it on the wire. Per ADR-143 (Bind the Ruleset) the win meter is the
opponent's STRESS FILL toward taken-out (stress boxes checked + consequences taken),
NOT the vestigial native ``opponent_metric.tension`` dial — so we project the
opponent's FateSheet, never the dial.

RED reasons (today): ``FateConflictParticipant`` has no ``stress``/``consequences``
fields, ``conflict_opponent_progress`` does not exist, and the
``fate.conflict.projected`` span/constant does not exist. New production symbols are
imported INSIDE each test so every gap reports cleanly (mirrors test_fate_state_emit).
"""

from __future__ import annotations

import pytest

from sidequest.game.fate_sheet import Aspect
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from tests._helpers.fate_fixtures import conflict_with_pc_and_npc

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _wounded_conflict():
    """A live PC-vs-opponent Fate conflict where the opponent has absorbed harm:
    one value-2 physical stress box checked + a filled mild consequence. Those marks
    are what a win meter reads — used=2(stress)+2(mild)=4 of a 26-point SRD sheet."""
    snap, enc = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    sheet = snap.npcs[0].core.fate_sheet
    assert sheet is not None  # the helper seeds a Fate sheet on the opponent
    sheet.stress["physical"].boxes[1].checked = True
    sheet.consequences[0].aspect = Aspect(text="Winged Shoulder", kind="consequence")
    return snap, enc


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


def _opponent(conflict):
    return next(p for p in conflict.participants if p.side == "opponent")


def _player(conflict):
    return next(p for p in conflict.participants if p.side == "player")


# ---------------------------------------------------------------------------
# Projection — opponent-side participant carries its stress + consequences.
# ---------------------------------------------------------------------------


def test_opponent_participant_carries_stress_and_consequences():
    snap, _ = _wounded_conflict()
    payload = build_fate_state_payload(snap)
    opp = _opponent(payload.conflict)

    # Stress track projected with the SRD two-box shape, the value-2 box checked.
    assert [b.value for b in opp.stress["physical"]] == [1, 2]
    assert opp.stress["physical"][1].checked is True
    assert opp.stress["physical"][0].checked is False

    # Consequences projected; the mild slot is filled and carries its aspect text.
    mild = next(c for c in opp.consequences if c.level == "mild")
    assert mild.filled is True
    assert mild.text == "Winged Shoulder"
    # An untaken slot reads filled=False with empty text.
    severe = next(c for c in opp.consequences if c.level == "severe")
    assert severe.filled is False and severe.text == ""


def test_player_participant_track_stays_empty():
    """The PC's full sheet rides in ``payload.characters`` — the conflict
    participant must NOT duplicate it (the projection is opponent-track only)."""
    snap, _ = _wounded_conflict()
    payload = build_fate_state_payload(snap)
    pc = _player(payload.conflict)
    assert pc.stress == {}
    assert pc.consequences == []
    # and the PC sheet IS present in characters (no data lost).
    assert any(c.name == "Rux" for c in payload.characters)


def test_sheetless_opponent_projects_empty_track_not_a_crash():
    """A seated opponent actor with no resolvable creature/sheet projects an empty
    track (honest empty — the enforcement lives in decide_opponent_action's #966
    guard), never crashing the FATE_STATE frame."""
    snap, _ = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    snap.npcs.clear()  # the "Bandit" actor now resolves to no creature core
    payload = build_fate_state_payload(snap)
    opp = _opponent(payload.conflict)
    assert opp.stress == {}
    assert opp.consequences == []


# ---------------------------------------------------------------------------
# Progress metric — taken-out fill computed from the WIRE payload.
# ---------------------------------------------------------------------------


def test_conflict_opponent_progress_from_payload():
    from sidequest.game.ruleset.fate_projection import conflict_opponent_progress

    snap, _ = _wounded_conflict()
    payload = build_fate_state_payload(snap)
    progress = conflict_opponent_progress(payload.conflict)

    assert len(progress) == 1
    name, fraction = progress[0]
    assert name == "Bandit"
    # used = 2 (checked value-2 box) + 2 (mild consequence) ; capacity = 6 stress + 20 cons.
    assert fraction == pytest.approx(4 / 26)


def test_conflict_opponent_progress_excludes_sheetless_and_player():
    from sidequest.game.ruleset.fate_projection import conflict_opponent_progress

    snap, _ = conflict_with_pc_and_npc(pc="Rux", npc="Bandit")
    snap.npcs.clear()
    payload = build_fate_state_payload(snap)
    # no opponent has a projected track -> nothing for the meter (and never the PC).
    assert conflict_opponent_progress(payload.conflict) == []


# ---------------------------------------------------------------------------
# Wiring — the emitter puts the track on the wire AND fires the lie-detector span.
# ---------------------------------------------------------------------------


def test_emit_projects_opponent_track_and_fires_conflict_span(otel_capture):
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state
    from sidequest.telemetry.spans.fate import SPAN_FATE_CONFLICT_PROJECTED

    snap, _ = _wounded_conflict()
    sink = _Sink()
    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=snap, emit_fn=sink)

    # The broadcast payload carries the opponent track (UI can draw the meter).
    assert len(sink.sent) == 1
    msg, kind = sink.sent[0]
    assert kind == "FATE_STATE"
    opp = _opponent(msg.payload.conflict)
    assert opp.stress["physical"][1].checked is True

    # The GM-panel lie detector fired with the opponent count + taken-out progress.
    spans = {s.name: s for s in otel_capture.get_finished_spans()}
    assert SPAN_FATE_CONFLICT_PROJECTED in spans
    attrs = spans[SPAN_FATE_CONFLICT_PROJECTED].attributes or {}
    assert attrs.get("opponent_count") == 1
    assert float(attrs.get("max_taken_out_progress")) == pytest.approx(4 / 26)


def test_no_conflict_span_when_no_active_conflict(otel_capture):
    """A Fate snapshot with a sheet but no seated conflict emits FATE_STATE but must
    NOT fire ``fate.conflict.projected`` (the span is conflict-scoped)."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore
    from sidequest.game.fate_sheet import FateSheet
    from sidequest.game.session import GameSnapshot
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state
    from sidequest.telemetry.spans.fate import SPAN_FATE_CONFLICT_PROJECTED

    snap = GameSnapshot(
        genre_slug="pulp_noir",
        characters=[
            Character(
                core=CreatureCore(
                    name="Solo", description="d", personality="p", fate_sheet=FateSheet(skills={})
                ),
                char_class="Agent",
                race="Human",
                backstory="b",
            )
        ],
    )
    sink = _Sink()
    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=snap, emit_fn=sink)

    assert len(sink.sent) == 1  # FATE_STATE still emits (sheet present)
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert SPAN_FATE_CONFLICT_PROJECTED not in span_names
