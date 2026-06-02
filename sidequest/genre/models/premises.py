"""Premise / Bloc content models for the wry_whimsy political substrate.

Content-tier only (Plan 1): these models load and validate authored YAML. They
carry NO runtime behavior — the live ``belief_reserve``/``defiance`` dials,
belief flow, and thresholds are Plan 2's ``PremiseState``/``BlocState`` on the
session snapshot.

Boundary (ADR-120): the witnessed-act *vocabulary* is genre-tier mechanics
(``witnessed_acts.yaml``); the *illusions* (which authority, which population,
how much each act moves) are world-tier flavor (``premises.yaml``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WitnessedActArchetype(BaseModel):
    """Genre-tier vocabulary: a kind of witnessed act that can move belief.

    Premises bind these by ``id`` in ``drained_by``; blocs bind them in
    ``awakening_acts``. The archetype is pure vocabulary — magnitude is tuned
    per-world on the binding (mechanics-in-genre, flavor-in-world).
    """

    model_config = {"extra": "forbid"}

    id: str
    label: str
    description: str = ""


class WitnessedActsFile(BaseModel):
    """Genre-tier ``witnessed_acts.yaml`` — the act-archetype vocabulary."""

    model_config = {"extra": "forbid"}

    witnessed_acts: list[WitnessedActArchetype] = Field(default_factory=list)


class PremiseClaim(BaseModel):
    """The proposition a population must believe for the authority to hold power.

    Plan 2 expresses this as an ADR-053 ``BeliefClaim``; at content tier it is
    the ``subject`` (who/what the claim is about — typically an NPC id) plus the
    believed ``proposition`` text.
    """

    model_config = {"extra": "forbid"}

    subject: str
    proposition: str


class PremiseDrain(BaseModel):
    """A witnessed-act archetype that contradicts a premise, with its tuned cost."""

    model_config = {"extra": "forbid"}

    act: str  # references WitnessedActArchetype.id
    belief_delta: int = Field(ge=1, le=100)
    cost: str = ""  # narrator-facing flavor: turns / risk / exposure


class PremiseCollapse(BaseModel):
    """What happens when ``belief_reserve`` falls to ``threshold`` or below."""

    model_config = {"extra": "forbid"}

    threshold: int = Field(ge=0, le=100)
    outcome: str
    vacuum_hook: str = ""  # the loose thread the collapse leaves behind


class PremiseDef(BaseModel):
    """A world-tier illusion that sustains an authority's power."""

    model_config = {"extra": "forbid"}

    premise_id: str
    authority: str  # references an AuthoredNpc id in the same world
    claim: PremiseClaim
    belief_reserve: int = Field(default=100, ge=0, le=100)
    propped_by: list[str] = Field(default_factory=list)  # bloc_ids
    drained_by: list[PremiseDrain] = Field(default_factory=list)
    collapse: PremiseCollapse


class BlocAwakening(BaseModel):
    """A witnessed-act archetype that raises a bloc's defiance, with tuned magnitude."""

    model_config = {"extra": "forbid"}

    act: str  # references WitnessedActArchetype.id
    defiance_delta: int = Field(ge=1, le=100)


class BlocDef(BaseModel):
    """A world-tier population the outsider can move."""

    model_config = {"extra": "forbid"}

    bloc_id: str
    defiance: int = Field(default=0, ge=0, le=100)  # starts low
    grants_belief_to: list[str] = Field(default_factory=list)  # premise_ids
    awakening_acts: list[BlocAwakening] = Field(default_factory=list)
    tipping_threshold: int = Field(default=70, ge=0, le=100)
    tipped_outcome: str
    flavor: str = ""


class PremisesFile(BaseModel):
    """World-tier ``premises.yaml`` — a world's illusions and the blocs that prop them."""

    model_config = {"extra": "forbid"}

    premises: list[PremiseDef] = Field(default_factory=list)
    blocs: list[BlocDef] = Field(default_factory=list)
