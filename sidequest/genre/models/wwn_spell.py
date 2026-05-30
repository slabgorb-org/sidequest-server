"""WWN spell content model — WwnSpell + WwnSpellCatalog.

Net-new; the B/X ``Spell`` model (sidequest/magic/spell_catalog.py) is NOT
reused — B/X tradition/save columns do not apply to WWN's spell economy.

Each ``WwnSpell`` holds the authored content data for one WWN spell and
produces the engine's ``CastInput`` via ``to_cast_input()``.
``WwnSpellCatalog`` aggregates a pack's full spell list with duplicate-id
rejection at load time.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from sidequest.game.wwn_magic import CastInput, SaveCategory


class WwnSpell(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    name: str
    level: int  # 1..max
    save: SaveCategory | None = None  # defender save category, or None
    damage_die: str | None = None  # "1d6" etc, or None for non-damage
    damage_per_level: bool = False  # caster_level x die when True
    genre_description: str  # player-facing prose
    mechanical_effect: str  # Approach C: narrator-adjudicated bespoke effect
    range: str = "near"  # flavor
    target: str = "single"  # flavor

    def to_cast_input(self) -> CastInput:
        return CastInput(
            id=self.id,
            level=self.level,
            save=self.save,
            damage_die=self.damage_die,
            damage_per_level=self.damage_per_level,
        )


class WwnSpellCatalog(BaseModel):
    model_config = {"extra": "forbid"}

    version: str = "1.0"
    spells: list[WwnSpell] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_spell_ids(self) -> WwnSpellCatalog:
        seen: dict[str, int] = {}
        for s in self.spells:
            seen[s.id] = seen.get(s.id, 0) + 1
        dupes = sorted(sid for sid, n in seen.items() if n > 1)
        if dupes:
            raise ValueError(f"WwnSpellCatalog has duplicate spell ids: {dupes}")
        return self

    def get(self, spell_id: str) -> WwnSpell:
        for s in self.spells:
            if s.id == spell_id:
                return s
        raise KeyError(f"spell {spell_id!r} not in catalog (have: {[s.id for s in self.spells]})")


def load_wwn_spell_catalog(path: Path) -> WwnSpellCatalog:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return WwnSpellCatalog.model_validate(raw)
