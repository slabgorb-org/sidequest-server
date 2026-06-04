"""Story 77-4 RED (ATOMIC BROAD) — retire the legacy quest_updates lane end-to-end.

Keith ruled ATOMIC BROAD: the complete ADR-137 AC-3 retirement in one story.
The narrow dead-field excision folds in as a subset. Surface under test:

NARROW subset (dead WorldStatePatch.quest_updates field):
- A1 construction rejected by extra="forbid"; A2 load-path rejected;
  A3 field absent from model_fields; A4 apply path has no dangling reader.

BROAD lane retirement:
- B  NarrationTurnResult.quest_updates field removed (extraction lane cut).
- C  apply_world_patch escape-hatch allowlist drops /active_stakes (set_stakes is
     the typed home now); /quest_log and /quest_updates stay rejected.
- D  NO-SILENT-FALLBACKS auto-forward guard: a narrator game_patch still carrying a
     ``quest_updates`` key is AUTO-FORWARDED to record_quest update-mode semantics
     (the status update LANDS in quest_log — never silently dropped) AND fires the
     loud GM-visible span ``quest.updates.legacy_emitted``; the legacy ``quest_update``
     span no longer fires; the turn never raises.
- E  atomicity guard: the status-only successor mechanism (upsert_quest_status, used by
     both record_quest update-mode and the auto-forward) is intact BEFORE the lane is cut.

RED honesty: the typed fields still exist today, so A1/A2 (DID NOT RAISE), A3/B
(field present), D (no auto-forward, no span), and C (/active_stakes still allowlisted)
all fail now for the right reason. A4 and E are green guards.

Held / GREEN-only (Dev), noted not tested here: narrator game_patch prompt contract
drops quest_updates (server-side agents/), websocket_session_handler telemetry update,
SPAN_QUEST_UPDATE constant teardown. The behavioral contracts above force those edits
(removing the typed field breaks any leftover reader loudly).
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from sidequest.game.session import (
    GameSnapshot,
    QuestEntry,
    WorldStatePatch,
    upsert_quest_status,
)


def _has_extra_forbidden_on(errors: list[dict], field: str) -> bool:
    """True iff some error is an ``extra_forbidden`` rejection of ``field``.

    After excision, passing the retired key trips ``extra="forbid"`` with this
    exact error at loc ``(field,)``. Before excision the field is accepted, so
    construction does not raise at all — the surrounding ``pytest.raises`` is
    what fails (DID NOT RAISE), giving an honest RED.
    """
    return any(
        bool(e.get("loc")) and e["loc"][0] == field and e["type"] == "extra_forbidden"
        for e in errors
    )


def test_world_patch_construction_rejects_quest_updates() -> None:
    """NARROW: once retired, constructing a patch with quest_updates is rejected
    loudly by extra="forbid" — the legacy status-only lane no longer has a home
    on WorldStatePatch."""
    with pytest.raises(ValidationError) as exc_info:
        WorldStatePatch(quest_updates={"q_witch": "active"})  # type: ignore[call-arg]
    assert _has_extra_forbidden_on(list(exc_info.value.errors()), "quest_updates")


def test_world_patch_model_validate_rejects_quest_updates_payload() -> None:
    """NARROW (load path): a persisted/narrator payload still carrying the legacy
    quest_updates key is rejected on validate — not silently dropped, not coerced."""
    with pytest.raises(ValidationError) as exc_info:
        WorldStatePatch.model_validate({"quest_updates": {"q_witch": "active"}})
    assert _has_extra_forbidden_on(list(exc_info.value.errors()), "quest_updates")


def test_world_patch_has_no_quest_updates_field() -> None:
    """NARROW (reflection tripwire): the field is gone from the model entirely.

    Interrogates the runtime pydantic field registry (not source text), per the
    CLAUDE.md 'No Source-Text Wiring Tests' sanctioned reflection exception."""
    assert "quest_updates" not in WorldStatePatch.model_fields
    # quest_log (the widened structured successor) must remain.
    assert "quest_log" in WorldStatePatch.model_fields


def test_apply_world_patch_unrelated_patch_has_no_dangling_quest_updates_reader() -> None:
    """NARROW (removal-safety guard): applying an ordinary patch must not raise.

    If the field is excised but the apply-branch coercion that reads
    ``patch.quest_updates`` is left behind, the first apply of ANY field-less
    patch raises AttributeError. This guards a half-done removal — the apply
    path carries no reference to the retired field. (Green today; the assertion
    is the no-raise + unchanged-quest_log invariant.)"""
    snap = GameSnapshot()
    snap.apply_world_patch(WorldStatePatch(atmosphere="tense"))
    assert snap.atmosphere == "tense"
    assert snap.quest_log == {}


# ---------------------------------------------------------------------------
# BROAD — B: NarrationTurnResult.quest_updates field removed (extraction lane cut)
# ---------------------------------------------------------------------------


def test_narration_turn_result_has_no_quest_updates_field() -> None:
    """BROAD: the live extraction lane is cut — NarrationTurnResult no longer
    carries a quest_updates field (dataclass reflection tripwire, not source-grep).

    Removing the field forces the 3 extraction sites (orchestrator.py:1258/3219/3549)
    to stop populating it; any leftover populator fails loudly at construction."""
    from sidequest.agents.orchestrator import NarrationTurnResult

    names = {f.name for f in dataclasses.fields(NarrationTurnResult)}
    assert "quest_updates" not in names
    # game_patch_dict must remain — it's the raw lane the auto-forward guard reads.
    assert "game_patch_dict" in names


# ---------------------------------------------------------------------------
# BROAD — C: apply_world_patch escape hatch drops /active_stakes
# ---------------------------------------------------------------------------


def test_apply_world_patch_allowlist_drops_quest_and_stakes_paths() -> None:
    """BROAD (item 6): the escape-hatch allowlist no longer exposes /active_stakes
    (set_stakes is the typed home post-77-2); /quest_log and /quest_updates stay
    off it. The allowlist is the single source of truth that drives the recoverable
    rejection at apply_world_patch.py:169 — inspecting it is a runtime-data check,
    not a source-text assertion."""
    from sidequest.agents.tools.apply_world_patch import _SUPPORTED_PATHS

    assert "/active_stakes" not in _SUPPORTED_PATHS  # RED now: still allowlisted
    assert "/quest_log" not in _SUPPORTED_PATHS
    assert "/quest_updates" not in _SUPPORTED_PATHS
    # The five non-quest/stakes string fields remain the escape hatch's job.
    assert "/location" in _SUPPORTED_PATHS


# ---------------------------------------------------------------------------
# BROAD — D: NO-SILENT-FALLBACKS auto-forward guard (the ruled contract)
# ---------------------------------------------------------------------------


def test_legacy_quest_updates_in_game_patch_auto_forwards_to_quest_log(otel_capture) -> None:
    """BROAD (No-Silent-Fallbacks, the ruled contract): after the lane is cut, a
    narrator game_patch that STILL carries a ``quest_updates`` key must NOT lose the
    update. It is auto-forwarded to record_quest update-mode semantics — the status
    lands in quest_log — and a loud, GM-visible ``quest.updates.legacy_emitted`` span
    fires. The turn never raises (live-session safety).

    RED now: the live writer reads the typed ``result.quest_updates`` (which we leave
    unset), so nothing lands and no legacy_emitted span exists.
    """
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    snap = GameSnapshot(quest_log={})
    result = NarrationTurnResult(
        narration="The witch is vanquished.",
        game_patch_dict={"quest_updates": {"q_witch": "resolved"}},
    )

    # Must not raise — graceful auto-forward, never crash a live turn.
    _apply_narration_result_to_snapshot(snap, result, player_name="Sam", room=room_for(snap))

    # 1) The update LANDED (not silently dropped) via record_quest update-mode semantics.
    assert "q_witch" in snap.quest_log
    assert snap.quest_log["q_witch"].status == "resolved"

    spans = otel_capture.get_finished_spans()
    # 2) Loud, GM-visible legacy signal fired.
    legacy = [s for s in spans if s.name == "quest.updates.legacy_emitted"]
    assert legacy, "quest.updates.legacy_emitted span did not fire"
    # 3) The retired legacy span is gone — quest.updated is the sole successor.
    assert not [s for s in spans if s.name == "quest_update"], (
        "legacy SPAN_QUEST_UPDATE must no longer fire"
    )


# ---------------------------------------------------------------------------
# BROAD — E: atomicity guard — status-only successor mechanism intact pre-cut
# ---------------------------------------------------------------------------


def test_upsert_quest_status_status_only_path_intact() -> None:
    """BROAD (atomicity): the shared status-only mechanism that BOTH record_quest
    update-mode AND the auto-forward route through must be intact before the lane is
    cut — no zero-writer window. (Green guard; the full tool path is proven by
    tests/agents/tools/test_record_quest.py::test_update_existing_quest_changes_status_and_fires_quest_updated,
    which is DB-gated.)"""
    log: dict[str, QuestEntry] = {}
    upsert_quest_status(log, "q_witch", "active")
    assert log["q_witch"].status == "active"
    # Update-in-place on an existing quest (the status-only update case).
    upsert_quest_status(log, "q_witch", "resolved")
    assert log["q_witch"].status == "resolved"
    assert len(log) == 1
