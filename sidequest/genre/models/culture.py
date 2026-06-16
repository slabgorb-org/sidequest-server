"""Name generation culture types from cultures.yaml.

Port of sidequest-genre/src/models/culture.rs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CorpusRef(BaseModel):
    """A reference to a Markov corpus file."""

    model_config = {"extra": "forbid"}

    corpus: str
    weight: float


class CultureSlot(BaseModel):
    """A name-generation slot — corpus-based, word-list-based, or file-based."""

    model_config = {"extra": "forbid"}

    corpora: list[CorpusRef] | None = None
    lookback: int | None = None
    word_list: list[str] | None = None
    names_file: str | None = None
    reject_files: list[str] = Field(default_factory=list)


class Culture(BaseModel):
    """A name-generation culture.

    ``dictionary`` is a placeholder for per-culture translation entries
    (always empty in current content). Rust dropped it; accepted as
    pass-through so future content can populate it without a schema change.
    """

    model_config = {"extra": "forbid"}

    name: str
    summary: str
    description: str
    slots: dict[str, CultureSlot] = Field(default_factory=dict)
    person_patterns: list[str] = Field(default_factory=list)
    place_patterns: list[str] = Field(default_factory=list)
    dictionary: dict[str, str] = Field(default_factory=dict)
    # heavy_metal flag: when False, culture is lore-only (NPCs, history) and
    # must not be offered during player chargen. Rust dropped it; chargen UI
    # is currently unaware — wiring story pending.
    chargen: bool = True
