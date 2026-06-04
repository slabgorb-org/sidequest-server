"""Story 77-4 RED (NARROW scope) — retire the dead WorldStatePatch.quest_updates lane.

SM ruling (session 77-4): this file is the NARROW excision — removing the
``WorldStatePatch.quest_updates`` field (session.py:501) and its apply-branch
coercion (session.py:1381-1385). That field is DEAD in production: nothing
constructs ``WorldStatePatch(quest_updates=...)`` (the apply_world_patch escape
hatch does not expose it; every other producer passes other fields), and the
only reader is the apply-branch coercion. Removing it is a zero-behavior
dead-code excision, valid regardless of the still-pending BROAD ruling
(retiring the separate LIVE ``NarrationTurnResult.quest_updates`` lane → record_quest).

These tests fail NOW for the right reason (the field still exists, so the
construction is accepted and the field is present in model_fields) and pass
once the field is excised and ``extra="forbid"`` rejects the key.

NOT in scope here (held for the BROAD ruling): NarrationTurnResult.quest_updates,
the narration_apply writer, SPAN_QUEST_UPDATE, websocket telemetry.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.session import GameSnapshot, WorldStatePatch


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
