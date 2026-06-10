"""MutationState — pydantic field on GameSnapshot (mirrors MagicState).

Per-character mutation crunch: MP pool, owned mutations, stigma, usage
counters. ``roll_sequence`` is the persisted cursor feeding
rolls.deterministic_roll — incrementing it is how a consumed roll
becomes un-rerollable across resumes (spec P2-6).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StigmaRecord(BaseModel):
    model_config = {"extra": "forbid"}

    body_part: str
    nature: str
    flavor: str
    concealable: bool = False


class UsageCounter(BaseModel):
    model_config = {"extra": "forbid"}

    period: Literal["per_scene", "per_day"]
    used: int = 0


class CharacterMutationState(BaseModel):
    model_config = {"extra": "forbid"}

    mp_remaining: int
    stigma: list[StigmaRecord] = Field(default_factory=list)
    negative_ids: list[str] = Field(default_factory=list)
    positive_ids: list[str] = Field(default_factory=list)
    usage: dict[str, UsageCounter] = Field(default_factory=dict)
    # Ordered acquisition history — the negatives-before-positives chargen
    # ordering (AWN p.16) is asserted against this log.
    acquisition_log: list[str] = Field(default_factory=list)


class MutationUseLogEntry(BaseModel):
    """One turn-stamped mutation-use receipt — Story 102-7.

    The ``WwnCastLogEntry`` mirror: the durable provenance artifact the
    ``magic_working`` engagement witness reads to answer "did the
    router-dispatched mutation use actually run the engine this turn?".
    Stamped on EVERY ``use_mutation`` resolution — a REFUSED use
    (limit exhausted, Strain over max, unowned) is still mechanical
    engagement: the engine answered, so the lie-detector must not cry wolf
    on a fizzle the narrator honestly narrates.
    """

    model_config = {"extra": "ignore"}

    turn: int
    actor: str
    mutation_id: str
    applied: bool


class MutationState(BaseModel):
    model_config = {"extra": "forbid"}

    characters: dict[str, CharacterMutationState] = Field(default_factory=dict)
    roll_sequence: int = 0

    def reset_scene(self) -> None:
        for cs in self.characters.values():
            for counter in cs.usage.values():
                if counter.period == "per_scene":
                    counter.used = 0

    def reset_day(self) -> None:
        for cs in self.characters.values():
            for counter in cs.usage.values():
                counter.used = 0
