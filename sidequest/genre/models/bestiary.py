"""Pack-root bestiary for ruleset-module packs (story 90-1).

A ``ruleset: wwn|cwn|swn|awn`` pack deliberately drops the native
``allowed_classes`` block (ADR-117), so encountergen cannot generate humanoid
enemies from rules.yaml. Instead the pack authors ``bestiary.yaml`` at the
pack root: SRD-aligned combat-layer stat blocks that encountergen samples and
dresses with the narrative layers (OCEAN, visual prompt) it already composes.

The bestiary is REQUIRED for ruleset-module packs — encountergen fails loud
when the bound ruleset is non-native and no bestiary is present (never a
silent empty Monster Manual pool; No Silent Fallbacks). Native packs ignore
the file entirely and keep the allowed_classes generation path.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class BestiaryEntry(BaseModel):
    """One combat-layer stat block.

    The required fields are the engine contract (90-1 schema decision):
    ``level`` maps to the encounter tier ladder, ``hp``/``armor_class``/
    ``attack_bonus`` are the SRD combat numbers. Extra keys are allowed so
    authors can carry SRD color (damage, move, morale, skill, save, ...)
    without a schema change.
    """

    model_config = {"extra": "allow"}

    id: str
    name: str
    level: int = Field(ge=1)
    hp: int = Field(ge=1)
    armor_class: int = Field(ge=1)
    attack_bonus: int
    damage: str | None = None
    role: str = ""
    description: str = ""
    abilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # Faction/zone-scoped content eligibility (epic-157, ADR-059 amendment).
    # Each value is either an exact world ``controlled_by`` faction slug
    # (e.g. ``the_houyhnhnm_assembly``) or the reserved sentinel ``"*"`` =
    # all zones in this world (world-global content). Default empty →
    # unzoned worlds and all existing content keep parsing unchanged; the
    # strict load validator (story 157-7), NOT this field, enforces non-empty
    # in a zoned world.
    factions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> BestiaryEntry:
        if not self.id:
            raise ValueError("bestiary entry id must not be empty")
        if not self.name:
            raise ValueError(f"bestiary entry {self.id!r} name must not be empty")
        return self


class Bestiary(BaseModel):
    """Top-level ``bestiary.yaml`` shape: a non-empty ``entries:`` list plus an
    optional ``generics:`` section (story 162-3).

    Generic rows are full ``BestiaryEntry`` stat blocks authored as the
    SANCTIONED last-resort Other for the opponent seater — the origin
    precedence ends ``... > MM pool > generics > error``, replacing the old
    DEFAULT-PATH ephemeral stub mint (No Silent Fallbacks). (Frame-sourced defs
    and the explicit degenerate opt-in still mint an ``EPHEMERAL_STUB`` — see
    ``OriginKind.GENERIC`` in ``game/origin.py``.) Ids are unique ACROSS both
    sections: identity is id-keyed (162-2 ``identity_key`` →
    ``creature:<id>``), so one id over two divergent stat blocks would fork
    identity at every downstream seam.
    """

    model_config = {"extra": "forbid"}

    entries: list[BestiaryEntry]
    generics: list[BestiaryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> Bestiary:
        if not self.entries:
            raise ValueError("bestiary.yaml must define a non-empty `entries:` list")
        seen: set[str] = set()
        for entry in (*self.entries, *self.generics):
            if entry.id in seen:
                raise ValueError(f"duplicate bestiary entry id {entry.id!r}")
            seen.add(entry.id)
        return self
