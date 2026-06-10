"""Psionic discipline catalog — per-pack content (pure data; mechanics in the engine).

Story 102-6 (Psionics, SWN design §6 follow-on / P7). Mirrors
``genre/models/wwn_spell.py``: the discipline catalog is CONTENT (ADR-140
"Crunch in the Genre, Flavor in the World" — a homebrew discipline is authored
purely in pack YAML, validated and activatable with zero engine edits), while
the Effort economy + System Strain that resolve a discipline live on the
SWN-family ruleset module.

Faithful to SWN SRD §6 (Psychics): a discipline draws ``effort_cost`` from the
psychic's single Effort pool for a ``duration`` (instant/scene/day), may force a
``save`` on its target, and an overcommit "push" can buy effect with System
Strain (``strain_cost``).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class PsionicDiscipline(BaseModel):
    """One psychic discipline/technique (SWN SRD §6). Authored in pack YAML.

    - ``effort_cost``: Effort committed from the psychic's pool on activation.
    - ``duration``: how long the Effort stays committed ("maintained"/"scene"/
      "day") — matches ``EffortCommitment.duration`` so it commits directly.
    - ``save``: the save category the discipline forces on its target
      ("physical"/"evasion"/"mental"/"luck"), or None for a no-save discipline.
    - ``strain_cost``: System Strain the caster takes when this discipline is a
      "push" (0 for a normal discipline) — the AC3 overcommit cost.
    """

    model_config = {"extra": "forbid"}

    id: str
    name: str
    level: int
    effort_cost: int
    duration: str = "scene"
    save: str | None = None
    strain_cost: int = 0
    genre_description: str = ""
    mechanical_effect: str = ""


class PsionicDisciplineCatalog(BaseModel):
    """A pack's psionic discipline catalog (``disciplines_psionic.yaml``).

    Mirrors ``WwnSpellCatalog``: duplicate ids are a content error caught at
    load, and ``get`` fails loud on an unknown id (No Silent Fallbacks).
    """

    model_config = {"extra": "forbid"}

    version: str = "1.0"
    disciplines: list[PsionicDiscipline] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_duplicate_ids(self) -> PsionicDisciplineCatalog:
        ids = [d.id for d in self.disciplines]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate psionic discipline id(s): {dupes}")
        return self

    def get(self, discipline_id: str) -> PsionicDiscipline:
        """Return the discipline with ``discipline_id``; raise KeyError if absent."""
        for d in self.disciplines:
            if d.id == discipline_id:
                return d
        raise KeyError(
            f"unknown psionic discipline id {discipline_id!r}; "
            f"have {[d.id for d in self.disciplines]}"
        )


def load_psionic_discipline_catalog(path: str | Path) -> PsionicDisciplineCatalog:
    """Load a psionic discipline catalog from a YAML file.

    ``yaml.safe_load`` (Python rule #8: no arbitrary code) → ``model_validate``
    (``extra="forbid"`` + duplicate-id rejection fail loud on a content typo).
    """
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return PsionicDisciplineCatalog.model_validate(data)
