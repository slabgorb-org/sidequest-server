"""Fate Core character facet — pure data (ADR-144 F1b, design §4.3).

A Fate-shaped sheet carried ALONGSIDE the d20 CreatureCore — it does NOT replace
stats/HpPool. A Fate-bound creature simply ALSO has this facet, mirroring how CWN
chrome adds ``system_strain`` and WWN adds ``spellcasting``. The model is inert:
the fate-point economy, stress marking, consequence-taking, and aspect invocation
— with their OTEL spans — live on ``FateRulesetModule`` (fate.py), exactly as
``SystemStrainPool``'s rules live on ``CwnRulesetModule.apply_system_strain``.

Faithful to the Fate Core SRD (Evil Hat, CC-BY):
- aspects: free-text phrases (high concept, trouble, others) — the LLM-narrator
  synergy ADR-144 turns on.
- skills: name -> ladder rating (the ladder int lives in fate_resolution.py).
- stunts: named special rules (the mechanical effect is content/F2; stored here).
- refresh / fate_points: the per-session economy.
- stress: physical + mental tracks of checkable boxes.
- consequences: mild(2)/moderate(4)/severe(6)/extreme(8) slots; a FILLED slot
  becomes an aspect (SRD: a consequence is an aspect with a free invoke for the
  attacker who inflicted it).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AspectKind = Literal["high_concept", "trouble", "character", "situation", "consequence", "boost"]
ConsequenceLevel = Literal["mild", "moderate", "severe", "extreme"]
StressTrackName = Literal["physical", "mental"]

#: SRD consequence slot shift-values. A filled slot absorbs this many shifts.
CONSEQUENCE_VALUES: dict[str, int] = {"mild": 2, "moderate": 4, "severe": 6, "extreme": 8}


class Aspect(BaseModel):
    """A free-text Fate aspect. ``free_invokes`` is the count of unused free
    invocations on it (create-advantage and consequences grant these)."""

    model_config = {"extra": "forbid"}

    text: str
    kind: AspectKind
    free_invokes: int = 0


class Stunt(BaseModel):
    """A named stunt. The mechanical effect is authored as content (F2/F4); the
    engine spine in F1 only needs to carry the name/description."""

    model_config = {"extra": "forbid"}

    name: str
    description: str = ""


class StressBox(BaseModel):
    """One checkable stress box of a fixed ``value``."""

    model_config = {"extra": "forbid"}

    value: int
    checked: bool = False


class StressTrack(BaseModel):
    """An ordered list of stress boxes (physical or mental)."""

    model_config = {"extra": "forbid"}

    boxes: list[StressBox] = Field(default_factory=list)


class Consequence(BaseModel):
    """One consequence slot. ``aspect is None`` => open; a set ``aspect`` => filled
    (and the consequence is now an invokable aspect). ``value`` is the SRD
    absorption value for the slot's level (see ``CONSEQUENCE_VALUES``)."""

    model_config = {"extra": "forbid"}

    level: ConsequenceLevel
    value: int
    aspect: Aspect | None = None


def _default_stress() -> dict[str, StressTrack]:
    return {
        "physical": StressTrack(boxes=[StressBox(value=1), StressBox(value=2)]),
        "mental": StressTrack(boxes=[StressBox(value=1), StressBox(value=2)]),
    }


def _default_consequences() -> list[Consequence]:
    return [
        Consequence(level="mild", value=CONSEQUENCE_VALUES["mild"]),
        Consequence(level="moderate", value=CONSEQUENCE_VALUES["moderate"]),
        Consequence(level="severe", value=CONSEQUENCE_VALUES["severe"]),
        Consequence(level="extreme", value=CONSEQUENCE_VALUES["extreme"]),
    ]


class FateSheet(BaseModel):
    """The Fate-shaped facet on a CreatureCore (pure data; rules on FateRulesetModule).

    Skill/aspect/stunt CONTENT is authored per genre (F4) and seeded at chargen
    (F2/F4); F1b ships the SRD baseline so a creature constructed without content
    is a valid, empty Fate sheet (refresh 3, two two-box stress tracks, four open
    consequence slots).
    """

    model_config = {"extra": "forbid"}

    aspects: list[Aspect] = Field(default_factory=list)
    skills: dict[str, int] = Field(default_factory=dict)
    stunts: list[Stunt] = Field(default_factory=list)
    refresh: int = 3
    fate_points: int = 3
    stress: dict[str, StressTrack] = Field(default_factory=_default_stress)
    consequences: list[Consequence] = Field(default_factory=_default_consequences)

    def all_aspects(self) -> list[Aspect]:
        """Every aspect on the sheet: character aspects + FILLED-consequence
        aspects. Open consequence slots contribute nothing. (Situation aspects
        live on the encounter, not here — F1c.)"""
        return [*self.aspects, *(c.aspect for c in self.consequences if c.aspect is not None)]
