"""Saint curation layer — world-tier presets over the AWN mutation catalog.

Story 103-1 (build plan 2026-06-10 §D-A; AWN rebase addendum 2026-06-09):
a Saint is a curated bundle of positive mutation ids plus exactly one
negative as the canonical drawback. Curation replaces the dice, not the
pricing — the bundle is funded by the same MP economy the Wild path
spends (base MP + the drawback's MP, at the random-pull rate).

World-tier content per ADR-140 (``worlds/<slug>/saints.yaml``); the genre
catalog stays the only mutation authority. Every referenced id must
resolve against it at load time, loudly (No Silent Fallbacks).

Part of the bespoke mutation subsystem (AWN spec D5) — NOT the
MagicPlugin seam.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.state import CharacterMutationState, MutationState
from sidequest.telemetry.spans.awn import awn_saint_applied_span

__all__ = [
    "SaintDef",
    "SaintRegistry",
    "apply_saint_preset",
    "load_saint_registry",
]

# Saint ids are world-tier names (``herman_of_the_acushnet``), never
# catalog-shaped ``category/...`` ids — a slash signals an authoring mixup.
_SAINT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SaintDef(BaseModel):
    """One Saint: a curated mutation preset with a single canonical drawback."""

    model_config = {"extra": "forbid"}

    id: str
    name: str
    tradition: Literal["literary", "catholic_immigrant", "folk_place", "wilderness_sleeper"]
    patron_regions: list[str] = Field(default_factory=list)
    bundle: list[str]
    drawback: str
    affinity: list[str] = Field(default_factory=list)
    iconography: str = ""
    veneration: str = ""

    @field_validator("id")
    @classmethod
    def id_bare_snake(cls, v: str) -> str:
        if not _SAINT_ID_RE.match(v):
            raise ValueError(
                f"saint id {v!r} must be bare snake_case (a world-tier name like "
                "'herman_of_the_acushnet', not a category/ catalog id)"
            )
        return v

    @field_validator("bundle")
    @classmethod
    def bundle_positive_and_unique(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("saint bundle must be non-empty — a Saint with no marks is no Saint")
        for mid in v:
            if mid.startswith("negative/"):
                raise ValueError(
                    f"bundle entry {mid!r} is a negative — the drawback field is the "
                    "single sanctioned burden; negatives never ride the bundle"
                )
        return v

    @model_validator(mode="after")
    def bundle_entries_unique(self) -> SaintDef:
        # model-level (not field-level) so the error can NAME the saint —
        # the loud-failure contract is "saint id + offending detail".
        dupes = {mid for mid in self.bundle if self.bundle.count(mid) > 1}
        if dupes:
            raise ValueError(f"saint {self.id!r}: duplicate bundle entries: {sorted(dupes)}")
        return self

    @field_validator("drawback")
    @classmethod
    def drawback_is_negative(cls, v: str) -> str:
        if not v.startswith("negative/"):
            raise ValueError(
                f"drawback {v!r} must be a negative/ catalog id — the Saint's "
                "drawback is a burden, not a gift"
            )
        return v

    @field_validator("affinity")
    @classmethod
    def affinity_positive(cls, v: list[str]) -> list[str]:
        for mid in v:
            if mid.startswith("negative/"):
                raise ValueError(
                    f"affinity entry {mid!r} is a negative — affinity lists hold "
                    "purchasable positives only"
                )
        return v


class SaintRegistry(BaseModel):
    """The world's Saint canon (``worlds/<slug>/saints.yaml``)."""

    model_config = {"extra": "forbid"}

    saints: list[SaintDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> SaintRegistry:
        seen: set[str] = set()
        dupes: set[str] = set()
        for s in self.saints:
            if s.id in seen:
                dupes.add(s.id)
            seen.add(s.id)
        if dupes:
            raise ValueError(f"duplicate saint ids: {sorted(dupes)}")
        return self

    def by_id(self, saint_id: str) -> SaintDef:
        for s in self.saints:
            if s.id == saint_id:
                return s
        raise KeyError(
            f"saint {saint_id!r} not in registry; known: {sorted(s.id for s in self.saints)}"
        )


def _validate_against_catalog(registry: SaintRegistry, catalog: MutationCatalog) -> None:
    """Every Saint reference must resolve against the genre catalog, and every
    bundle must be affordable under the MP economy. Loud, naming the saint AND
    the offending id — never a skip."""
    negative_ids = {n.id for n in catalog.negatives}
    positive_ids = {p.id for p in catalog.positives}
    eco = catalog.mp_economy
    budget = eco.base_mp + eco.per_negative_mp
    for saint in registry.saints:
        for mid in [*saint.bundle, *saint.affinity]:
            if mid not in positive_ids:
                raise ValueError(
                    f"saint {saint.id!r} references unknown positive mutation {mid!r}; "
                    f"not in the genre mutation catalog"
                )
        if saint.drawback not in negative_ids:
            raise ValueError(
                f"saint {saint.id!r} drawback {saint.drawback!r} is not in the genre "
                "mutation catalog's negative table"
            )
        cost = len(saint.bundle) * eco.spend_random_positive
        if cost > budget:
            raise ValueError(
                f"saint {saint.id!r} bundle of {len(saint.bundle)} marks costs {cost} MP "
                f"at the random-pull rate but the preset budget is {budget} "
                f"(base {eco.base_mp} + drawback {eco.per_negative_mp}) — the Saint "
                "would grant more than the economy pays for; move marks to affinity"
            )


def load_saint_registry(path: Path, catalog: MutationCatalog) -> SaintRegistry:
    """saints.yaml -> SaintRegistry, cross-validated against the genre catalog.

    Fail-loud; absence is the CALLER's decision (the genre loader treats a
    missing file as 'world has no Saints' — mirrors load_mutation_catalog).
    """
    if not path.is_file():
        raise FileNotFoundError(f"saint registry not found: {path} (expected saints.yaml)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    registry = SaintRegistry.model_validate(raw)
    _validate_against_catalog(registry, catalog)
    return registry


def apply_saint_preset(
    state: MutationState,
    catalog: MutationCatalog,
    registry: SaintRegistry,
    *,
    actor: str,
    saint_id: str,
    session_id: str,
) -> CharacterMutationState:
    """Seed a Saint-Marked character: the Saint's bundle + drawback through
    the SAME MpEconomy the Wild path spends.

    Ledger: ``mp_remaining = base_mp + per_negative_mp - len(bundle) *
    spend_random_positive`` — the spring rolls for you at the random rate;
    affordability was enforced at registry load. The drawback lands first in
    the acquisition log (AWN p.16 — burdens before gifts).

    Idempotent: an already-seeded actor returns the existing state unchanged
    and re-emits nothing (re-entrant chargen handlers must not double-grant).
    """
    saint = registry.by_id(saint_id)  # KeyError loud on unknown saint
    if actor in state.characters:
        return state.characters[actor]
    eco = catalog.mp_economy
    mp_spent = len(saint.bundle) * eco.spend_random_positive
    mp_remaining = eco.base_mp + eco.per_negative_mp - mp_spent
    cs = CharacterMutationState(
        mp_remaining=mp_remaining,
        negative_ids=[saint.drawback],
        positive_ids=list(saint.bundle),
        acquisition_log=[saint.drawback, *saint.bundle],
    )
    state.characters[actor] = cs
    awn_saint_applied_span(
        actor=actor,
        saint_id=saint.id,
        drawback=saint.drawback,
        bundle_count=len(saint.bundle),
        mp_base=eco.base_mp,
        mp_from_drawback=eco.per_negative_mp,
        mp_spent=mp_spent,
        mp_remaining=mp_remaining,
        session_id=session_id,
    )
    return cs
