"""ResolutionSignal — one-shot payload feeding the [ENCOUNTER RESOLVED] zone.

Set by ``apply_beat`` (via narration_apply or dispatch/dice) when the
encounter flips ``resolved=True``. The narrator prompt assembler reads
this slot on the next turn and clears it. Spec 2026-04-25-dual-track-
momentum-design.md §"[ENCOUNTER RESOLVED] zone (one-shot)".
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ResolutionSignal(BaseModel):
    """The transient signal carried into one (and only one) narrator turn."""

    model_config = {"extra": "forbid"}

    encounter_type: str
    outcome: str
    final_player_metric: int
    final_opponent_metric: int
    yielded_actors: tuple[str, ...] = Field(default_factory=tuple)
    edge_refreshed: int = 0
