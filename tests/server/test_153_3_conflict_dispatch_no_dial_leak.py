"""Story 153-3 RT1 — Reviewer Finding 2 (the stray-beat dial leak): MEASURED, not real.

The Reviewer rejected 153-3 claiming a ``resolution_mode == conflict`` encounter
leaks the native dial: a stray narrator ``beat_selection`` was traced into the
``else: _legacy_beat_path = True`` arm (narration_apply.py ~6443) → ``apply_beat`` on
a display-only stub (ADR-144 REPLACE violation). That trace SKIPPED the branch that
runs first: the Story 126-37 ``if enc.win_condition == "fate_conflict":`` drop
(narration_apply.py ~5974), which precedes the whole ``resolution_mode`` ladder.

A ``conflict``-mode def ALWAYS seats with ``win_condition == "fate_conflict"`` —
``seat_as_fate_conflict`` (encounter_lifecycle.py:1701) is true for every Fate
non-contest/non-sealed mode, of ANY category, and line 1757 stamps the win track
accordingly (pinned by tests/server/dispatch/test_fate_seating_denativized_126_30.py
::test_*conflict*). So a stray beat against a live conflict is dropped at 5974 BEFORE
the dial — exactly as it is for the native ``beat_selection`` Fate conflict 126-37
closed. The dial does NOT leak.

These tests therefore PIN that protection for the new ``conflict`` resolution_mode
(they pass on the current branch — Finding 2 has no reproducible RED). If a future
change ever decouples conflict seating from ``win_condition='fate_conflict'`` (the
"footgun" the Architect flagged for a future non-combat Conflict), test #1 fails and
the leak is caught. The genuine RED for this round is the NARRATOR crash —
tests/agents/test_narrator_encounter_beats.py
::test_build_encounter_context_fate_conflict_does_not_render_native_beats.

Harness mirrors tests/server/test_126_37_narration_apply_fate_conflict_beat_drop.py:
drive the real ``_apply_narration_result_to_snapshot`` with a synthetic pack whose
``violence`` cdef declares ``resolution_mode: conflict`` and an opponent-side beat
(opponent beats bypass the SOUL "The Test" PC-consent gate, so ``from_explicit_action``
stays at its production default of False).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, RulesConfig
from sidequest.protocol.dice import RollOutcome
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.session_room import SessionRoom


def _conflict_pack() -> GenrePack:
    """A pack whose ``violence`` confrontation is a Fate Conflict (153-3): display-only
    stub beats (kind=None), no native dial metrics. (Ruleset is left native on the bare
    RulesConfig — the narration-apply ladder keys on ``enc.win_condition`` and
    ``cdef.resolution_mode``, not on the pack ruleset; a real FateConfig is unrelated
    to this dispatch path.)"""
    cdef = ConfrontationDef(
        type="violence",
        label="The Jabberwock",
        category="combat",
        resolution_mode="conflict",
        beats=[
            BeatDef(id="strike", label="Strike"),
            BeatDef(id="parry", label="Parry"),
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    pack.effective_cultures.return_value = ([], "genre")
    pack.source_dir = None
    pack.worlds = {}
    return pack


def _make_room() -> tuple[SessionRoom, GameSnapshot]:
    room = SessionRoom(slug="wonderland", mode=GameMode.SOLO)
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="wonderland",
        turn_manager=TurnManager(interaction=1),
    )
    room.bind_world(snapshot=snap, store=MagicMock(spec=SaveRepository))
    return room, snap


def _seated_conflict_encounter() -> StructuredEncounter:
    """A conflict-mode ``violence`` encounter as SEATING produces it: the native dial
    is replaced by inert ``fate_stress`` placeholders and ``win_condition`` is the
    engine-only ``fate_conflict`` track (encounter_lifecycle.py:1712-1718, 1757)."""
    return StructuredEncounter(
        encounter_type="violence",
        category="combat",
        win_condition="fate_conflict",
        player_metric=EncounterMetric(
            name="fate_stress", current=0, starting=0, threshold=1_000_000
        ),
        opponent_metric=EncounterMetric(
            name="fate_stress", current=0, starting=0, threshold=1_000_000
        ),
        actors=[
            EncounterActor(name="Alice", role="hero", side="player"),
            EncounterActor(name="Jabberwock", role="monster", side="opponent"),
        ],
    )


def _opponent_strike_result() -> NarrationTurnResult:
    """A stray opponent ``strike`` beat the narrator could hallucinate against a live
    conflict. ``strike`` is a display-only stub (kind=None); if it ever reached the
    dial engine, ``apply_beat`` would fault on ``DEFAULT_DELTAS[None]`` — the ADR-144
    leak this guard prevents."""
    return NarrationTurnResult(
        narration="The Jabberwock lunges at Alice.",
        beat_selections=[
            BeatSelection(actor="Jabberwock", beat_id="strike", outcome=RollOutcome.Success)
        ],
        npcs_present=[NpcMention(name="Jabberwock", side="opponent", role="hostile")],
    )


def _capture_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _spy)
    return events


# ---------------------------------------------------------------------------
# 1 — PIN (Finding 2): a seated conflict does NOT leak the native dial.
# ---------------------------------------------------------------------------


def test_seated_conflict_drops_stray_beat_and_does_not_touch_the_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``resolution_mode: conflict`` encounter, seated with
    ``win_condition='fate_conflict'``, drops a stray opponent beat BEFORE the dial
    engine (the 126-37 branch). The opponent dial stays at 0 and a
    ``conflict_beat_dropped_dial_blocked`` event surfaces on the GM panel — NO
    ``beat_applied``. Passes on the current branch: Finding 2's dial leak does not
    reproduce through real seating."""
    events = _capture_watcher(monkeypatch)
    room, snap = _make_room()
    snap.encounter = _seated_conflict_encounter()

    _apply_narration_result_to_snapshot(
        snap, _opponent_strike_result(), "Alice", room=room, pack=_conflict_pack()
    )

    assert snap.encounter is not None
    assert snap.encounter.opponent_metric.current == 0, (
        "a Fate Conflict resolves through the 4dF engine (opponent FateSheet stress), "
        "NOT the native dial — a stray beat advanced the suppressed opponent dial to "
        f"{snap.encounter.opponent_metric.current} (ADR-143/144: dial engine REMOVED "
        "from the Fate path)"
    )
    assert not [e for e in events if e["fields"].get("op") == "beat_applied"], (
        "no beat_applied may fire for a Fate Conflict — the stray beat was dropped, "
        "not applied to the dial"
    )
    dropped = [e for e in events if e["fields"].get("op") == "conflict_beat_dropped_dial_blocked"]
    assert len(dropped) == 1, (
        "the narration pipeline must DROP the stray beat for a Fate Conflict and "
        "surface it on the GM panel (the 126-37 win_condition='fate_conflict' branch "
        f"covers the new conflict mode); got ops={[e['fields'].get('op') for e in events]}"
    )
    assert dropped[0]["fields"].get("beat_id") == "strike", (
        "the drop event must name the dropped beat for GM-panel audit"
    )
