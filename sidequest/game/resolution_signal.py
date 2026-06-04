"""ResolutionSignal — one-shot payload feeding the [ENCOUNTER RESOLVED] zone.

Set by ``apply_beat`` (via narration_apply or dispatch/dice) when the
encounter flips ``resolved=True``. The narrator prompt assembler reads
this slot on the next turn and clears it. Spec 2026-04-25-dual-track-
momentum-design.md §"[ENCOUNTER RESOLVED] zone (one-shot)".
"""

from __future__ import annotations

from typing import Literal

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
    # Story 59-33 — which side yielded, when the resolution was a yield. DERIVED
    # from ``outcome`` at construction via ``encounter_classifier.yield_side_for``
    # (never hand-set — a hand-set None default would mislabel surrender/rout).
    # Orthogonal to ``is_player_victory``: a player yield is side "player" AND a
    # loss; ``None`` for non-yield resolutions (dial wins, abandonment, etc.).
    yield_side: Literal["player", "opponent"] | None = None
