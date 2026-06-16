"""TurnRecord — immutable snapshot of a completed dispatch turn.

Assembled at the end of session_handler._execute_narration_turn and put
on the validator queue. Frozen for immutability across the queue boundary.

Per ADR-089 §2.1 (deliberate departure from Rust ADR-031), Python stores
snapshot_before_hash + snapshot_after + delta rather than two full
GameSnapshot clones — same validation power without the double-clone
cost on every turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PatchSummary:
    """Compact record of one patch applied during a turn."""

    patch_type: str  # "world" | "combat" | "chase" | "scenario"
    fields_changed: list[str]


@dataclass(frozen=True)
class TurnRecord:
    """One completed turn, ready for narrative validation."""

    turn_id: int
    timestamp: datetime
    player_id: str
    player_input: str
    classified_intent: str
    agent_name: str
    narration: str
    patches_applied: list[PatchSummary]
    snapshot_before_hash: str
    snapshot_after: Any  # GameSnapshot — typed Any to keep telemetry game-layer-free
    delta: Any  # StateDelta — same reason
    beats_fired: list[tuple[str, float]]  # (trope_name, threshold)
    extraction_tier: int
    token_count_in: int
    token_count_out: int
    agent_duration_ms: int
    is_degraded: bool
    # Phase-timing fields (Story: turn-pipeline phase-timing).
    # Defaulted so existing TurnRecord(...) call-sites that don't yet pass
    # phase data keep working until they migrate. The Validator emits
    # whichever values arrive — empty dicts are surfaced as missing keys
    # in the turn_complete event, not zeros (a missing phase ≠ a 0 ms phase).
    phase_durations_ms: dict[str, int] = field(default_factory=dict)
    phase_call_counts: dict[str, int] = field(default_factory=dict)
    total_duration_ms: int = 0
    # Count of footnote knowledge entries emitted by the narrator this turn.
    # Footnotes are the "Knowledge Gained" entries that surface in the UI
    # chip and as `state_transition field=footnotes` watcher events. They
    # do NOT go through the `patch.discovered_facts` path today, so the
    # validator's `delta_empty` computation must include them explicitly
    # — otherwise a turn that introduces 3 new knowledge entries but
    # no location/quest/lore patches reads as `Patches: none, Delta empty:
    # true` on the dashboard (playtest 2026-04-30).
    footnotes_count: int = 0
