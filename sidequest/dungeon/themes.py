"""Theme palette schema + strict loader (spec: Beneath Sünden §5.2, §6;
§10 step 4).

The pack ships a curated `themes/` directory; each theme keys an interior
generator, declares its depth_score eligibility band (Plan 3 raw units —
NOT player-facing level buckets, spec §5), creature/loot tables, narrator
register, adjacency affinities, and a set-piece library.

Loader is STANDALONE and fail-loud (CLAUDE.md No Silent Fallbacks):
deliberately NOT wired into the generic load_genre_pack — a `themes/` dir
is dungeon-specific to beneath_sunden; an optional generic loader would
silently no-op for the other 5 packs. The runtime consumer (Plan 7's
materializer building a depth-filtered theme_pool + Plan 6's set-piece
roll) is an honest deferral, identical to Plan 2/3's stance. Task 7 will
add tests/dungeon/test_themes_wiring.py to prove this loader against the
REAL shipped caverns_and_claudes scaffold and the real interiors.ALGORITHMS
registry (not yet present at this commit — authored in Task 7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sidequest.dungeon.interiors import ALGORITHMS
from sidequest.dungeon.setpieces import SetPiece


class InteriorSpec(BaseModel):
    """Which ported maze-maker generator fills this theme's interiors.

    `algorithm` is validated against the REAL Plan-1 coordinator registry
    (`interiors.ALGORITHMS`) — a genuine cross-module wire, not a copied
    enum. `braid_ratio` is range-checked here; `generate_interior` itself
    skips the braid post-process when it is <= 0.0 (spec §5.2: labyrinth-
    trap stays a pristine perfect maze at 0.0)."""

    model_config = ConfigDict(extra="forbid")

    algorithm: str
    params: dict = Field(default_factory=dict)
    braid_ratio: float = 0.0

    @field_validator("algorithm")
    @classmethod
    def _v_algorithm(cls, v: str) -> str:
        if v not in ALGORITHMS:
            raise ValueError(f"unknown interior algorithm {v!r}; known: {sorted(ALGORITHMS)}")
        return v

    @field_validator("braid_ratio")
    @classmethod
    def _v_braid(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("braid_ratio must be in [0.0, 1.0]")
        return v


# spec §5.2 — theme class -> generator family (hard invariant)
_CLASS_ALGORITHM = {
    "organic": "cellular",
    "labyrinthine": "depthfirst",
    "structured": "prim",
    "built": "roomcorridor",
}


def _nonblank(v: str) -> str:
    if not v.strip():
        raise ValueError("must be a non-blank string")
    return v


class DepthBand(BaseModel):
    """Raw depth_score eligibility window (Plan 3 units). max=None ->
    unbounded-deep. NOT player-facing level buckets (spec §5)."""

    model_config = ConfigDict(extra="forbid")

    min: float = 0.0
    max: float | None = None

    @field_validator("min")
    @classmethod
    def _v_min(cls, v: float) -> float:
        if v < 0.0:
            raise ValueError("depth_band.min must be >= 0")
        return v

    @model_validator(mode="after")
    def _v_band(self) -> DepthBand:
        if self.max is not None and self.max < self.min:
            raise ValueError("depth_band.max must be >= depth_band min")
        return self


class NarratorFlavor(BaseModel):
    """Register + flavor seed for Plan 7's prompt assembly. Beneath
    Sünden plays grave/lethal (spec §3) — register & flavor non-blank."""

    model_config = ConfigDict(extra="forbid")

    register: str  # spec §6 name; shadows ABCMeta.register via metaclass — known, non-breaking, do not rename without spec sign-off
    flavor: str
    motifs: list[str] = Field(default_factory=list)

    @field_validator("register", "flavor")
    @classmethod
    def _v_text(cls, v: str) -> str:
        return _nonblank(v)

    @field_validator("motifs")
    @classmethod
    def _v_motifs(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip():
                raise ValueError("a motif cannot be blank")
        return v


class Adjacency(BaseModel):
    """Theme-placement affinities (spec §6: 'tomb -> crypt deepens;
    flooded clusters'). Palette-level cross-resolution (ids must exist,
    no self-avoidance against the OWNING id) is enforced in
    load_theme_palette (Task 5); here we reject the trivially-nonsensical
    forms detectable without the owning id."""

    model_config = ConfigDict(extra="forbid")

    prefers: list[str] = Field(default_factory=list)
    avoids: list[str] = Field(default_factory=list)

    @field_validator("prefers")
    @classmethod
    def _v_prefers(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip():
                raise ValueError("a prefers entry cannot be a blank id")
        return v

    @field_validator("avoids")
    @classmethod
    def _v_avoids(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip():
                raise ValueError(
                    "a theme cannot avoid itself / a blank id (empty avoids entry is nonsensical)"
                )
        return v

    @model_validator(mode="after")
    def _v_disjoint(self) -> Adjacency:
        both = set(self.prefers) & set(self.avoids)
        if both:
            raise ValueError(f"theme id(s) in both prefers and avoids: {sorted(both)}")
        return self


class CreatureEntry(BaseModel):
    """Weighted creature ref. Resolution vs monster manual is Plan 6."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    weight: float = 1.0
    depth_band: DepthBand | None = None

    @field_validator("ref")
    @classmethod
    def _v_ref(cls, v: str) -> str:
        return _nonblank(v)

    @field_validator("weight")
    @classmethod
    def _v_weight(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError("weight must be > 0")
        return v


class LootEntry(BaseModel):
    """Weighted loot ref. Resolution vs inventory.yaml is Plan 6."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    weight: float = 1.0
    depth_band: DepthBand | None = None

    @field_validator("ref")
    @classmethod
    def _v_ref(cls, v: str) -> str:
        return _nonblank(v)

    @field_validator("weight")
    @classmethod
    def _v_weight(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError("weight must be > 0")
        return v


class ExpansionQuestTemplate(BaseModel):
    """Per-expansion quest template (ADR-137 × ADR-106). Deterministic slot
    fill at attach; narrator flavors the prose at surface (Amendment C).

    ``signature`` declares what beat completes the quest:
      - big_bad   : the deepest region's big_bad NPC (hp_depletion resolves)
      - set_piece : the set-piece named by ``set_piece_id`` (trope handshake resolves)
      - reach_deep: the deepest region (frontier transition into it resolves)
    """

    model_config = ConfigDict(extra="forbid")

    signature: Literal["big_bad", "set_piece", "reach_deep"]
    title: str
    objective: str
    set_piece_id: str | None = None  # required iff signature == "set_piece"

    @model_validator(mode="after")
    def _v_set_piece_id(self) -> "ExpansionQuestTemplate":
        if self.signature == "set_piece" and not (self.set_piece_id or "").strip():
            raise ValueError("signature 'set_piece' requires a non-blank set_piece_id")
        return self


class DungeonTheme(BaseModel):
    """One curated themed zone definition (spec §6)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    generator_class: str
    interior: InteriorSpec
    depth_band: DepthBand
    narrator: NarratorFlavor
    adjacency: Adjacency = Field(default_factory=Adjacency)
    creature_table: list[CreatureEntry] = Field(default_factory=list)
    loot_table: list[LootEntry] = Field(default_factory=list)
    set_pieces: list[SetPiece] = Field(default_factory=list)
    quest_template: ExpansionQuestTemplate | None = None

    @field_validator("id", "display_name")
    @classmethod
    def _v_text(cls, v: str) -> str:
        return _nonblank(v)

    @field_validator("generator_class")
    @classmethod
    def _v_class(cls, v: str) -> str:
        if v not in _CLASS_ALGORITHM:
            raise ValueError(f"unknown generator_class {v!r}; known: {sorted(_CLASS_ALGORITHM)}")
        return v

    @model_validator(mode="after")
    def _v_class_matches_algorithm(self) -> DungeonTheme:
        expected = _CLASS_ALGORITHM[self.generator_class]
        if self.interior.algorithm != expected:
            raise ValueError(
                f"generator_class {self.generator_class!r} does not match "
                f"interior.algorithm {self.interior.algorithm!r} "
                f"(spec §5.2 expects {expected!r})"
            )
        return self


class ThemePaletteMissingError(FileNotFoundError):
    """Raised when a pack directory has no `themes/` directory at all —
    fail loud, never an empty-palette silent fallback (CLAUDE.md)."""

    def __init__(self, pack_dir: Path) -> None:
        self.pack_dir = pack_dir
        super().__init__(f"themes/ directory missing in {pack_dir}")


class ThemePalette(BaseModel):
    """The loaded, cross-validated curated palette, keyed by theme id."""

    model_config = ConfigDict(extra="forbid")

    themes: dict[str, DungeonTheme] = Field(default_factory=dict)

    def get(self, theme_id: str) -> DungeonTheme:
        """Fail-loud lookup — unknown id is a bug, not an empty default."""
        if theme_id not in self.themes:
            raise KeyError(f"no theme {theme_id!r} in palette; have: {sorted(self.themes)}")
        return self.themes[theme_id]

    def themes_for_depth(self, depth_score: float) -> list[DungeonTheme]:
        """Themes eligible at a raw depth_score (Plan 3 units), sorted by
        id for deterministic theme_pool construction. The runtime caller
        (Plan 7's materializer) is the consumer; this is the pure helper."""
        return [
            self.themes[tid]
            for tid in sorted(self.themes)
            if theme_eligible_at_depth(self.themes[tid], depth_score)
        ]


def theme_eligible_at_depth(theme: DungeonTheme, depth_score: float) -> bool:
    """True iff depth_score falls in the theme's raw depth_band
    (inclusive; max=None == unbounded-deep). Spec §5: depth_score is the
    authoritative gradient driving theme-band eligibility."""
    band = theme.depth_band
    if depth_score < band.min:
        return False
    return band.max is None or depth_score <= band.max


def load_theme_palette(pack_dir: Path) -> ThemePalette:
    """Strict loader for `<pack_dir>/themes/*.yaml`.

    Fail-loud (CLAUDE.md No Silent Fallbacks):
      - no themes/ dir            -> ThemePaletteMissingError
      - themes/ but no *.yaml     -> ValueError
      - schema violation          -> ValueError (filename in message)
      - duplicate theme id        -> ValueError
      - affinity id not in palette-> ValueError
      - self-avoidance            -> ValueError

    Standalone by design — NOT called from load_genre_pack (a themes/
    dir is dungeon-specific to beneath_sunden). Plan 7's materializer is
    the runtime caller.
    """
    themes_dir = pack_dir / "themes"
    if not themes_dir.is_dir():
        raise ThemePaletteMissingError(pack_dir)

    yaml_files = sorted(p for p in themes_dir.glob("*.yaml") if p.is_file())
    if not yaml_files:
        raise ValueError(f"no theme files (*.yaml) in {themes_dir}")

    themes: dict[str, DungeonTheme] = {}
    for path in yaml_files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            theme = DungeonTheme.model_validate(raw)
        except Exception as e:  # yaml.YAMLError, pydantic ValidationError, et al.
            raise ValueError(f"{path.name}: {e}") from e
        if theme.id in themes:
            raise ValueError(f"duplicate theme id {theme.id!r} (seen again in {path.name})")
        themes[theme.id] = theme

    # Palette-level cross-validation: affinities must resolve, and a theme
    # cannot avoid itself (preferring itself IS valid — spec §6 'flooded
    # clusters').
    known = set(themes)
    for tid, theme in themes.items():
        for ref in (*theme.adjacency.prefers, *theme.adjacency.avoids):
            if ref not in known:
                raise ValueError(
                    f"theme {tid!r} adjacency references unknown theme id "
                    f"{ref!r}; known: {sorted(known)}"
                )
        if tid in theme.adjacency.avoids:
            raise ValueError(f"theme {tid!r} cannot avoid itself")

    return ThemePalette(themes=themes)
