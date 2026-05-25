"""LethalityPolicy — per-genre lethality tuning consumed by LethalityArbiter.

Spec: docs/superpowers/specs/2026-04-23-local-dm-decomposer-design.md §4 + §10
Group C.

YAML lives in sidequest-content/genre_packs/<pack>/lethality_policy.yaml.
Strict validation (extra='forbid'): unknown keys raise at pack-load time,
not at runtime — CLAUDE.md "no silent fallbacks".

The arbiter (sidequest.agents.lethality_arbiter) reads this model to
decide what verdict shape a given genre produces when a PC or NPC hits
zero HP, and what narrator-tone constraint envelope ships alongside.

ADR-114: renamed verdicts_on_zero_edge → verdicts_on_zero_hp; the YAML
key in all lethality_policy.yaml files was updated to match.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from sidequest.protocol.dispatch import LethalityVerdictKind, Reversibility


class VerdictsOnZeroHp(BaseModel):
    """Per-actor-kind verdict shape when `core.hp.current == 0`."""

    model_config = ConfigDict(extra="forbid")

    pc: LethalityVerdictKind
    npc: LethalityVerdictKind


class LethalityPolicy(BaseModel):
    """Per-genre lethality-arbitration inputs.

    `genre_key` is required and must match the pack directory name — the
    loader validates this (Task 3). `must_narrate` + `must_not_narrate` ship
    as a paired envelope in the narrator prompt (see Task 12); neither may
    be blank.
    """

    model_config = ConfigDict(extra="forbid")

    genre_key: str
    default_reversibility: Reversibility
    verdicts_on_zero_hp: VerdictsOnZeroHp
    soul_md_constraint: str
    must_narrate: str
    must_not_narrate: str

    @field_validator("genre_key", "soul_md_constraint", "must_narrate", "must_not_narrate")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("field cannot be blank")
        return v


__all__ = ["LethalityPolicy", "VerdictsOnZeroHp"]
