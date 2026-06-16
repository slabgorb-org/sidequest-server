"""Reserved event kinds for Group B/C going-forward corpus capture.

This module defines constants and payload schemas. It does NOT emit events —
emitter wiring belongs to the group that owns the subsystem. Group D reserves
only; miner then picks them up automatically once Groups B/C emit.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

DISPATCH_PACKAGE_KIND: Final[str] = "DISPATCH_PACKAGE"
NARRATOR_DIRECTIVE_USED_KIND: Final[str] = "NARRATOR_DIRECTIVE_USED"
VERDICT_OVERRIDE_KIND: Final[str] = "VERDICT_OVERRIDE"


class DispatchPackageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decomposer_session_id: str
    dispatched_at: str
    raw_package_json: str


class NarratorDirectiveUsedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directive_kind: str  # e.g. "must_narrate", "must_not_narrate"
    directive_text: str


class VerdictOverrideEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    previous_verdict: str | None
    new_verdict: str
    labeler: str
