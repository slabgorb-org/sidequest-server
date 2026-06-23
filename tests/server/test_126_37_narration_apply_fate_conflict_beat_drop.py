"""RED (story 126-37): de-nativize Fate confrontation RESOLUTION — narration_apply beat-drop.

The THIRD of the three downstream guards. 126-30 seated Fate standoffs with
``win_condition == "fate_conflict"`` (native dial removed), but a Fate conflict seats with
``resolution_mode == "beat_selection"`` (the native default — NOT ``contest``). So in the
narration-apply dispatch tree (narration_apply.py ~5502-5955) a Fate conflict falls past
the ``ResolutionMode.contest`` drop branch into the ``else: _legacy_beat_path = True`` arm
and the narrator's beat selections are fed straight to the legacy ``apply_beat`` dial
engine — the exact leak this story closes.

The fix mirrors the existing Fate-Contest drop (``contest_beat_dropped_dial_blocked``):
for a ``win_condition == "fate_conflict"`` encounter, the narrator's native beat selections
are DROPPED before the legacy beat loop and the block is surfaced LOUDLY on the GM panel
(No Silent Fallbacks). The conflict resolves through the 4dF engine (FATE_ACTION), never
the dial. Per ADR-143/144 the dial engine is REMOVED from the Fate path, not balanced.

Harness mirrors tests/server/test_narration_apply_session_wiring.py: drive the real
``_apply_narration_result_to_snapshot`` with the ``synthetic_two_dial_pack`` fixture and an
opponent-side beat selection (opponent beats are not subject to the SOUL "The Test"
PC-consent gate, so ``from_explicit_action`` stays at its production default of False).

The drop-event ``op`` (``conflict_beat_dropped_dial_blocked``) is the chosen GM-panel marker,
sibling of ``contest_beat_dropped_dial_blocked``; Dev may finalize the exact string as long
as it is a distinct, fate_conflict-gated drop op. The hard RED assertions are behavioral:
the dial does NOT move and no ``beat_applied`` fires for a Fate conflict, while a native
dial_threshold encounter applies the same beat unchanged.
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
from sidequest.protocol.dice import RollOutcome
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.session_room import SessionRoom


def _make_room() -> tuple[SessionRoom, GameSnapshot]:
    room = SessionRoom(slug="coyote_star", mode=GameMode.SOLO)
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=1),
    )
    room.bind_world(snapshot=snap, store=MagicMock(spec=SaveRepository))
    return room, snap


def _encounter(win_condition: str) -> StructuredEncounter:
    """A 'combat' encounter matching the synthetic_two_dial_pack cdef. An opponent-side
    "attack" (kind=strike, base=2) at Success advances opponent_metric by 2 on the legacy
    path — the move this guard must suppress for a Fate conflict."""
    return StructuredEncounter(
        encounter_type="combat",
        win_condition=win_condition,
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Reb", role="combatant", side="player"),
            EncounterActor(name="Foe", role="combatant", side="opponent"),
        ],
    )


def _opponent_attack_result() -> NarrationTurnResult:
    return NarrationTurnResult(
        narration="The Foe lunges at Reb.",
        beat_selections=[BeatSelection(actor="Foe", beat_id="attack", outcome=RollOutcome.Success)],
        npcs_present=[NpcMention(name="Foe", side="opponent", role="hostile")],
    )


def _capture_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture state_transition events published from the narration-apply pipeline
    (patched where used, per check #6)."""
    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _spy)
    return events


# ---------------------------------------------------------------------------
# 1 — RED (AC-1, AC-2): native beats are dropped for a Fate conflict.
# ---------------------------------------------------------------------------


def test_narration_apply_drops_native_beats_for_fate_conflict(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_two_dial_pack,
) -> None:
    """AC-1/AC-2: a ``win_condition == "fate_conflict"`` encounter seats with the native
    ``beat_selection`` resolution mode, so the narrator's beat falls into the legacy dial
    loop today and advances opponent_metric to 2. The guard must DROP that selection
    before apply_beat (no dial move, no beat_applied) and surface the block on the GM
    panel. Today there is no fate_conflict drop branch — the beat applies and this fails
    (RED)."""
    events = _capture_watcher(monkeypatch)
    room, snap = _make_room()
    snap.encounter = _encounter("fate_conflict")

    _apply_narration_result_to_snapshot(
        snap, _opponent_attack_result(), "Reb", room=room, pack=synthetic_two_dial_pack
    )

    assert snap.encounter is not None
    assert snap.encounter.opponent_metric.current == 0, (
        "a Fate conflict resolves through the 4dF engine (opponent FateSheet stress), NOT "
        "the native dial — the narrator's beat advanced the suppressed opponent dial to "
        f"{snap.encounter.opponent_metric.current} (ADR-143/144: dial engine REMOVED from "
        "the Fate path)"
    )
    assert not [e for e in events if e["fields"].get("op") == "beat_applied"], (
        "no beat_applied may fire for a Fate conflict — the native beat was dropped, not "
        "applied to the dial"
    )
    dropped = [e for e in events if e["fields"].get("op") == "conflict_beat_dropped_dial_blocked"]
    assert len(dropped) == 1, (
        "the narration pipeline must DROP the native beat selection for a Fate conflict and "
        "surface it on the GM panel (sibling of contest_beat_dropped_dial_blocked); got "
        f"ops={[e['fields'].get('op') for e in events]}"
    )
    assert dropped[0]["fields"].get("beat_id") == "attack", (
        "the drop event must name the dropped beat for GM-panel audit"
    )


# ---------------------------------------------------------------------------
# 2 — PIN (cross-ruleset): a native dial_threshold encounter applies the beat.
# ---------------------------------------------------------------------------


def test_narration_apply_applies_native_beat_for_dial_threshold(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_two_dial_pack,
) -> None:
    """Cross-ruleset guard: the drop MUST be ``win_condition``-gated. A native
    dial_threshold encounter still feeds the narrator's beat to apply_beat — the dial
    advances and beat_applied fires, with NO drop event. Passes before AND after the fix
    (pins native resolution is untouched)."""
    events = _capture_watcher(monkeypatch)
    room, snap = _make_room()
    snap.encounter = _encounter("dial_threshold")

    _apply_narration_result_to_snapshot(
        snap, _opponent_attack_result(), "Reb", room=room, pack=synthetic_two_dial_pack
    )

    assert snap.encounter is not None
    assert snap.encounter.opponent_metric.current == 2, (
        "a native dial_threshold encounter must still apply the opponent strike (base 2) "
        f"to the dial; got {snap.encounter.opponent_metric.current}"
    )
    assert [e for e in events if e["fields"].get("op") == "beat_applied"], (
        "a native encounter must emit beat_applied for the applied beat"
    )
    assert not [
        e for e in events if e["fields"].get("op") == "conflict_beat_dropped_dial_blocked"
    ], "conflict_beat_dropped_dial_blocked must be Fate-only — it must never fire for a native pack"
