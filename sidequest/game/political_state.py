"""Runtime political state for the wry_whimsy substrate (Plan 2).

Live, mutable dials hydrated from the Plan-1 content (``PremiseDef``/``BlocDef``):
``belief_reserve`` (an authority's power) and ``defiance`` (a population's
willingness to act). These are AUTHORITATIVE aggregate dials per spec §4.4/§11
— not a roll-up over per-NPC ``BeliefState``. The ledger is the provenance trail
(which act, which witnesses, which turn) for OTEL and the future Standing panel.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PremiseState(BaseModel):
    """Live dial for one authority's sustaining illusion."""

    model_config = {"extra": "forbid"}

    premise_id: str
    belief_reserve: int
    collapsed: bool = False


class BlocState(BaseModel):
    """Live dial for one population the outsider can move."""

    model_config = {"extra": "forbid"}

    bloc_id: str
    defiance: int
    tipped: bool = False


class BeliefLedgerEntry(BaseModel):
    """One recorded change to a dial — the receipt the world keeps (spec §6)."""

    model_config = {"extra": "forbid"}

    turn: int
    act_id: str
    target_id: str
    target_kind: str  # "premise" | "bloc"
    effect: str  # "drained" | "coupled" | "awakened" | "collapsed" | "tipped"
    delta: int  # signed change applied (0 for collapse/tip markers)
    new_value: int  # belief_reserve or defiance after the change
    witnesses: list[str] = Field(default_factory=list)


class PoliticalState(BaseModel):
    """Container for a session's live premises, blocs, and provenance ledger."""

    model_config = {"extra": "forbid"}

    premises: dict[str, PremiseState] = Field(default_factory=dict)
    blocs: dict[str, BlocState] = Field(default_factory=dict)
    ledger: list[BeliefLedgerEntry] = Field(default_factory=list)

    @classmethod
    def from_world(cls, world: Any) -> "PoliticalState | None":
        """Hydrate live dials from a genre ``World``'s authored premises/blocs.

        Returns ``None`` when the world authors no political layer — a valid
        authoring choice (NOT an empty container, NOT a fallback). ``None`` is
        what the precondition gate keys on to make ``witnessed_act`` inert.
        """
        premises = list(getattr(world, "premises", None) or [])
        blocs = list(getattr(world, "blocs", None) or [])
        if not premises and not blocs:
            return None
        return cls(
            premises={
                p.premise_id: PremiseState(
                    premise_id=p.premise_id, belief_reserve=p.belief_reserve
                )
                for p in premises
            },
            blocs={
                b.bloc_id: BlocState(bloc_id=b.bloc_id, defiance=b.defiance)
                for b in blocs
            },
            ledger=[],
        )
