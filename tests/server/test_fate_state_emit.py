"""RED tests for Story 118-1 (F3a) — the reactive FATE_STATE emitter (behavior).

Mirrors ``_maybe_emit_quests`` / ``_maybe_emit_relationships``: build a global payload
and broadcast via ``emit_fn``, change-gated on a signature so it fires only when the
Fate state actually changes (Cost Scales with Drama — NOT per-turn).

The load-bearing DIFFERENCE from its siblings (ADR-144 / epic 118): the emitter is
gated on ``sd.genre_pack.rules.ruleset == "fate"`` so a Fate surface NEVER co-renders
with the WN/native beat/dial overlay. The paired negative tests below (native + a WN
sibling) are the epic's mandated guard.

All FAIL today: ``sidequest.server.websocket_handlers.fate_state_emit`` does not exist
(RED). The emitter is imported inside each test so every gap reports cleanly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.session import GameSnapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sd(ruleset: str = "fate"):
    """A session-data stand-in exposing the gate the emitter reads:
    ``sd.genre_pack.rules.ruleset`` (the path the narration turn uses at
    websocket_session_handler.py:1488)."""
    return SimpleNamespace(genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset=ruleset)))


def _pc(name: str = "Vance", *, fate_points: int = 3) -> Character:
    sheet = FateSheet(skills={"Fight": 3}, fate_points=fate_points)
    sheet.aspects.append(Aspect(text="Last Honest Cop in Vega", kind="high_concept"))
    return Character(
        core=CreatureCore(name=name, description="d", personality="p", fate_sheet=sheet),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


def _snapshot(*, fate_points: int = 3) -> GameSnapshot:
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[EncounterActor(name="Vance", role="lead", side="player")],
    )
    return GameSnapshot(
        genre_slug="pulp_noir",
        characters=[_pc(fate_points=fate_points)],
        encounter=enc,
    )


class _Sink:
    """Captures broadcast messages + the kind tag."""

    def __init__(self):
        self.sent: list[tuple[object, str]] = []

    def __call__(self, msg, kind):
        self.sent.append((msg, kind))


# ---------------------------------------------------------------------------
# AC-1 — happy path: a Fate pack with live Fate state broadcasts a FATE_STATE.
# ---------------------------------------------------------------------------


def test_emit_sends_message_on_fate_pack():
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    from sidequest.protocol.messages import FateStateMessage

    sink = _Sink()
    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=_snapshot(), emit_fn=sink)

    assert len(sink.sent) == 1
    msg, kind = sink.sent[0]
    assert kind == "FATE_STATE"
    assert isinstance(msg, FateStateMessage)
    assert msg.payload.characters[0].name == "Vance"


# ---------------------------------------------------------------------------
# AC-2 — THE GATE (paired negative): never fire off a Fate pack, even with state.
# ---------------------------------------------------------------------------


def test_emit_skipped_for_native_pack():
    """No Silent Fallbacks / never co-render with the native overlay: a native pack
    must NOT emit FATE_STATE even when the snapshot carries Fate-shaped state."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    sink = _Sink()
    _maybe_emit_fate_state(_Handler(), sd=_sd("native"), snapshot=_snapshot(), emit_fn=sink)
    assert sink.sent == []


def test_emit_skipped_for_without_number_pack():
    """The paired negative for a WN sibling — a Fate surface must never collide with
    the WN beat/dial ConfrontationOverlay (epic 118 mandate)."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    sink = _Sink()
    _maybe_emit_fate_state(_Handler(), sd=_sd("wwn"), snapshot=_snapshot(), emit_fn=sink)
    assert sink.sent == []


# ---------------------------------------------------------------------------
# AC-3 — change gate: on-change, NOT per-turn.
# ---------------------------------------------------------------------------


def test_emit_skipped_when_unchanged():
    """Second call with identical state is a no-op (Cost Scales with Drama)."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    handler = _Handler()
    sink = _Sink()
    snap = _snapshot()
    _maybe_emit_fate_state(handler, sd=_sd("fate"), snapshot=snap, emit_fn=sink)
    _maybe_emit_fate_state(handler, sd=_sd("fate"), snapshot=snap, emit_fn=sink)
    assert len(sink.sent) == 1


def test_emit_refires_when_fate_points_change():
    """A fate-point spend is a dramatic change — it must re-broadcast."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    handler = _Handler()
    sink = _Sink()
    _maybe_emit_fate_state(handler, sd=_sd("fate"), snapshot=_snapshot(fate_points=3), emit_fn=sink)
    _maybe_emit_fate_state(handler, sd=_sd("fate"), snapshot=_snapshot(fate_points=2), emit_fn=sink)
    assert len(sink.sent) == 2


def test_emit_fires_on_first_call_for_fresh_handler():
    """A fresh handler (reconnect) has no cached signature, so the first call MUST
    emit even though the state matches a prior session (mirrors the quests guard)."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    snap = _snapshot()
    a, b = _Sink(), _Sink()
    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=snap, emit_fn=a)
    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=snap, emit_fn=b)
    assert len(a.sent) == 1 and len(b.sent) == 1


# ---------------------------------------------------------------------------
# AC-4 — empty state: nothing to show is a silent no-op (not an error, not a frame).
# ---------------------------------------------------------------------------


def test_emit_skipped_when_no_fate_characters():
    """A Fate pack whose snapshot has no Fate-sheet PC shows nothing (mirrors the
    empty-spine no-op of the quests emitter)."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    snap = GameSnapshot(
        genre_slug="pulp_noir",
        characters=[
            Character(
                core=CreatureCore(name="NoSheet", description="d", personality="p"),
                char_class="Agent",
                race="Human",
                backstory="b",
            )
        ],
    )
    sink = _Sink()
    _maybe_emit_fate_state(_Handler(), sd=_sd("fate"), snapshot=snap, emit_fn=sink)
    assert sink.sent == []


# ---------------------------------------------------------------------------
# AC-5 — signature committed only AFTER a successful broadcast (no stale skip).
# ---------------------------------------------------------------------------


def test_signature_not_committed_when_emit_raises():
    """If the broadcast raises, the cached signature must stay unset so the NEXT
    turn retries rather than skipping a never-delivered frame (the quests emitter's
    'commit after broadcast' discipline — client must not be left stale)."""
    from sidequest.server.websocket_handlers.fate_state_emit import _maybe_emit_fate_state

    handler = _Handler()
    snap = _snapshot()

    def _boom(_msg, _kind):
        raise RuntimeError("transport down")

    with pytest.raises(RuntimeError):
        _maybe_emit_fate_state(handler, sd=_sd("fate"), snapshot=snap, emit_fn=_boom)

    # The retry on a healthy transport must now deliver the frame.
    sink = _Sink()
    _maybe_emit_fate_state(handler, sd=_sd("fate"), snapshot=snap, emit_fn=sink)
    assert len(sink.sent) == 1


class _Handler:
    """A bare handler object — the emitter caches its change signature as an
    attribute on this instance (mirrors the quests/relationships handlers)."""
