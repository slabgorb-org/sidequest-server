"""Stock layer — world-tier chargen trait sets built from AWN primitives.

Story 103-2 (build plan 2026-06-10 §D-B; AWN rebase addendum 2026-06-09):
a stock is a character-entry path (Sleeper / Animal / Plant / Synthetic /
...) defined entirely as data — attr mods, Move/AC/Trauma-Target hooks,
granted positive mutation ids, and whether one Saint affinity bundle may
layer on top. The engine has ONE generic application path; nothing in
this module branches on a stock's id (epic guardrail: if Synthetic needs
``if stock == "synthetic"``, the schema is wrong).

World-tier content per ADR-140 (``worlds/<slug>/stocks.yaml``); the genre
catalog stays the only mutation authority — every granted id must resolve
against it at load time, loudly (No Silent Fallbacks).

Sleeper implants ride here too: ``use_implant`` charges System Strain
through the EXISTING ``CwnRulesetModule.apply_system_strain`` pool (same
slot as AWN cyberware/stims — no parallel implant economy).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.game.system_strain import StrainResult
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.saints import SaintRegistry
from sidequest.mutation.state import CharacterMutationState, MutationState
from sidequest.telemetry.spans.awn import awn_stock_applied_span

if TYPE_CHECKING:
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore
    from sidequest.game.ruleset.cwn import CwnRulesetModule
    from sidequest.genre.models.items import WorldItem
    from sidequest.genre.models.rules import SwnConfig

__all__ = [
    "StockDef",
    "StockRegistry",
    "apply_stock",
    "load_stock_registry",
    "use_implant",
]

# Stock ids are world-tier names (``harbor_seal``), never catalog-shaped
# ``category/...`` ids — a slash signals an authoring mixup.
_STOCK_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class StockDef(BaseModel):
    """One stock: a generic trait set. Defaults ARE the no-op — absence of a
    hook is data, not a code path."""

    model_config = {"extra": "forbid"}

    id: str
    name: str
    description: str = ""
    attr_mods: dict[str, int] = Field(default_factory=dict)
    move: int | None = None
    ac: int | None = None
    trauma_target_mod: int = 0
    granted_mutations: list[str] = Field(default_factory=list)
    saint_affinity_allowed: bool = False

    @field_validator("id")
    @classmethod
    def id_bare_snake(cls, v: str) -> str:
        if not _STOCK_ID_RE.match(v):
            raise ValueError(
                f"stock id {v!r} must be bare snake_case (a world-tier name like "
                "'harbor_seal', not a category/ catalog id)"
            )
        return v

    @field_validator("granted_mutations")
    @classmethod
    def grants_positive(cls, v: list[str]) -> list[str]:
        for mid in v:
            if mid.startswith("negative/"):
                raise ValueError(
                    f"granted mutation {mid!r} is a negative — drawbacks are the "
                    "Saint layer's vocabulary; stocks grant gifts only"
                )
        return v

    @model_validator(mode="after")
    def grants_unique(self) -> StockDef:
        # model-level (not field-level) so the error can NAME the stock —
        # the loud-failure contract is "stock id + offending detail".
        dupes = {mid for mid in self.granted_mutations if self.granted_mutations.count(mid) > 1}
        if dupes:
            raise ValueError(f"stock {self.id!r}: duplicate granted mutations: {sorted(dupes)}")
        return self


class StockRegistry(BaseModel):
    """The world's stock roster (``worlds/<slug>/stocks.yaml``)."""

    model_config = {"extra": "forbid"}

    stocks: list[StockDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> StockRegistry:
        seen: set[str] = set()
        dupes: set[str] = set()
        for s in self.stocks:
            if s.id in seen:
                dupes.add(s.id)
            seen.add(s.id)
        if dupes:
            raise ValueError(f"duplicate stock ids: {sorted(dupes)}")
        return self

    def by_id(self, stock_id: str) -> StockDef:
        for s in self.stocks:
            if s.id == stock_id:
                return s
        raise KeyError(
            f"stock {stock_id!r} not in registry; known: {sorted(s.id for s in self.stocks)}"
        )


def _validate_against_catalog(registry: StockRegistry, catalog: MutationCatalog) -> None:
    """Every granted id must resolve against the genre catalog. Loud, naming
    the stock AND the offending id — never a skip (same contract as saints)."""
    positive_ids = {p.id for p in catalog.positives}
    for stock in registry.stocks:
        for mid in stock.granted_mutations:
            if mid not in positive_ids:
                raise ValueError(
                    f"stock {stock.id!r} references unknown positive mutation {mid!r}; "
                    f"not in the genre mutation catalog"
                )


def load_stock_registry(path: Path, catalog: MutationCatalog) -> StockRegistry:
    """stocks.yaml -> StockRegistry, cross-validated against the genre catalog.

    Fail-loud; absence is the CALLER's decision (the genre loader treats a
    missing file as 'world has no stocks' — mirrors load_saint_registry).
    """
    if not path.is_file():
        raise FileNotFoundError(f"stock registry not found: {path} (expected stocks.yaml)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    registry = StockRegistry.model_validate(raw)
    _validate_against_catalog(registry, catalog)
    return registry


def apply_stock(
    character: Character,
    state: MutationState,
    catalog: MutationCatalog,
    registry: StockRegistry,
    *,
    actor: str,
    stock_id: str,
    session_id: str,
    saints: SaintRegistry | None = None,
    saint_id: str | None = None,
) -> CharacterMutationState:
    """Apply a stock's trait set generically; optionally layer ONE Saint.

    Trait hooks: ``attr_mods`` delta ``character.stats``; ``ac``/``move``
    override the creature core when set; ``trauma_target_mod`` lands on the
    core. Granted mutations are birthright — they reach the sheet at ZERO MP
    cost, never through the spend ladder.

    Saint layering (AC4): when the stock allows it, ``saint_id`` applies the
    bundle + drawback through 103-1's preset arithmetic exactly once —
    ``mp_remaining = base_mp + per_negative_mp - len(bundle) *
    spend_random_positive``. A saint_id against a non-affine stock, or with
    no registry, is a configuration error — loud, never improvised.

    Idempotent per actor: an already-seeded actor returns the existing state
    unchanged — no stat re-apply (the +1-STR-becomes-+2 reconnect bug), no
    re-grant, no span re-emit.
    """
    stock = registry.by_id(stock_id)  # KeyError loud on unknown stock
    saint = None
    if saint_id is not None:
        if not stock.saint_affinity_allowed:
            raise ValueError(
                f"stock {stock.id!r} does not allow a Saint affinity "
                f"(saint_affinity_allowed: false) but saint_id {saint_id!r} was given"
            )
        if saints is None:
            raise ValueError(
                f"saint_id {saint_id!r} given but no Saint registry supplied — the "
                "active world ships no saints.yaml"
            )
        saint = saints.by_id(saint_id)  # KeyError loud on unknown saint

    if actor in state.characters:
        return state.characters[actor]

    eco = catalog.mp_economy
    # Atomic application (review rework 2026-06-11): validate EVERY attr key
    # before mutating any — a partial apply on a bad key is a corrupting
    # trap for any caller that reuses the character object.
    unknown_attrs = [attr for attr in stock.attr_mods if attr not in character.stats]
    if unknown_attrs:
        raise ValueError(
            f"stock {stock.id!r} modifies attributes {unknown_attrs} which are not on "
            f"{actor!r}'s sheet; known: {sorted(character.stats)}"
        )
    for attr, mod in stock.attr_mods.items():
        character.stats[attr] += mod
    if stock.ac is not None:
        character.core.armor_class = stock.ac
    if stock.move is not None:
        character.core.move = stock.move
    character.core.trauma_target_mod = stock.trauma_target_mod

    positives = list(stock.granted_mutations)
    negatives: list[str] = []
    log: list[str] = []
    mp_remaining = eco.base_mp
    if saint is not None:
        # 103-1's preset math, verbatim — priced exactly once; the stock's
        # own grants stay free (burdens before gifts in the log, AWN p.16).
        mp_spent = len(saint.bundle) * eco.spend_random_positive
        mp_remaining = eco.base_mp + eco.per_negative_mp - mp_spent
        negatives = [saint.drawback]
        positives.extend(saint.bundle)
        log = [saint.drawback, *stock.granted_mutations, *saint.bundle]
    else:
        log = list(stock.granted_mutations)

    cs = CharacterMutationState(
        mp_remaining=mp_remaining,
        negative_ids=negatives,
        positive_ids=positives,
        acquisition_log=log,
    )
    state.characters[actor] = cs

    span_attrs: dict[str, object] = {
        "actor": actor,
        "stock_id": stock.id,
        "granted_count": len(stock.granted_mutations),
        "attr_mods": ", ".join(f"{k} {v:+d}" for k, v in stock.attr_mods.items()),
        "trauma_target_mod": stock.trauma_target_mod,
        "session_id": session_id,
    }
    if stock.ac is not None:
        span_attrs["ac"] = stock.ac
    if stock.move is not None:
        span_attrs["move"] = stock.move
    if saint is not None:
        span_attrs["saint_id"] = saint.id
    awn_stock_applied_span(**span_attrs)
    return cs


def use_implant(
    core: CreatureCore,
    item: WorldItem,
    *,
    module: CwnRulesetModule,
    cfg: SwnConfig | None,
    actor: str,
    session_id: str,
) -> StrainResult:
    """Use a Sleeper implant: charge its ``strain_cost`` through the EXISTING
    System Strain pool (CwnRulesetModule.apply_system_strain — the same slot
    AWN cyberware/stims spend; over-max refusal and pool arithmetic are the
    live machinery, not a reimplementation).

    An item with no positive integer ``strain_cost`` is not a System-Strain
    source; using it as one is a configuration error — loud, naming the item.
    """
    cost = getattr(item, "strain_cost", None)
    if not isinstance(cost, int) or isinstance(cost, bool) or cost < 1:
        raise ValueError(
            f"item {item.id!r} is not a System-Strain source: implants must "
            f"carry a positive integer strain_cost (got {cost!r})"
        )
    return module.apply_system_strain(
        core=core,
        kind="temporary",
        amount=cost,
        source=item.id,
        cfg=cfg,
    )
