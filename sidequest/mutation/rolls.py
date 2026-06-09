"""Deterministic, resume-safe dice for mutation acquisition.

House pattern from lull_escalation.py: SHA-256 over stable identifiers,
NO ``random`` module, NO wallclock — a resume re-rolls identically
(spec P2-6 / ADR-128). The ``sequence`` input is MutationState.roll_sequence,
which is persisted on the snapshot: each consumed roll increments it, so a
save mid-chargen reloads to the same next roll.
"""

from __future__ import annotations

import hashlib


def deterministic_roll(*, session_id: str, actor: str, purpose: str, sequence: int, sides: int) -> int:
    """Return a 1..sides roll, fully determined by the inputs."""
    if sides < 1:
        raise ValueError(f"sides must be >= 1, got {sides}")
    payload = f"{session_id}|{actor}|{purpose}|{sequence}".encode()
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") % sides + 1
