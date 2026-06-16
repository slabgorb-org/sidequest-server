"""Interactive Fate chargen — choices model, legality validator, pure sheet builder.

ADR-144 F4a2 (story 121-7). F4a (121-1) seeds a DEFAULT FateSheet (the Menu path);
this module turns EXPLICIT player choices (archetype -> aspects -> skill pyramid ->
stunts) into a legal sheet. ``validate_fate_sheet`` is the SINGLE legality authority
(design §9): the engine (``FateRulesetModule.apply_fate_chargen``), the wire contract,
and the content validator all reuse it, so a rule lives in exactly one place.

Layering: ``game`` depends on ``genre`` (never the reverse), so this game-tier module
may read a genre-tier ``FateConfig`` but the genre tier never imports this.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sidequest.game.fate_sheet import Aspect, FateSheet, Stunt
from sidequest.genre.models.rules import FateConfig


class FateChargenError(ValueError):
    """An interactive Fate chargen submission was illegal (an unfillable pyramid, a
    missing mandatory aspect, an out-of-catalog stunt, a broken refresh invariant).
    Fail loud — No Silent Fallbacks (SOUL.md / CLAUDE.md)."""


class FateChargenChoices(BaseModel):
    """The explicit player choices the interactive Fate chargen flow produces (the
    Guided/Freeform output), POST-edit. ``apply_fate_chargen`` turns these into a
    FateSheet and validates them. Aspects are split into the mandatory High
    Concept + Trouble and the free aspects; the pyramid is a placed-skill ->
    ladder-rating map (unplaced skills are Mediocre/+0 and simply absent)."""

    model_config = {"extra": "forbid"}

    archetype: str = ""
    high_concept: str
    trouble: str
    free_aspects: list[str] = Field(default_factory=list)
    pyramid: dict[str, int] = Field(default_factory=dict)
    stunts: list[str] = Field(default_factory=list)


def required_refresh(cfg: FateConfig, total_stunts: int) -> int:
    """The refresh a sheet MUST carry for ``total_stunts`` stunts: base refresh
    minus one per stunt over ``free_stunts``, floored at 1 (the gear-model
    invariant, one source of truth)."""
    return max(1, cfg.refresh - max(0, total_stunts - cfg.free_stunts))


def expected_rung_counts(cfg: FateConfig) -> dict[int, int]:
    """rating -> expected skill count for the pack's pyramid. Rung ``i`` (0 = apex)
    sits at ladder rating ``chargen_apex_rating - i`` and holds ``chargen_pyramid[i]``
    skills."""
    return {cfg.chargen_apex_rating - i: count for i, count in enumerate(cfg.chargen_pyramid)}


def build_fate_sheet(choices: FateChargenChoices, cfg: FateConfig) -> FateSheet:
    """Build a FateSheet from explicit choices (pure; no validation, no I/O). The
    refresh is computed from the stunt economy. Call ``validate_fate_sheet`` on the
    result before trusting it."""
    aspects: list[Aspect] = [
        Aspect(text=choices.high_concept, kind="high_concept"),
        Aspect(text=choices.trouble, kind="trouble"),
        *(Aspect(text=a, kind="character") for a in choices.free_aspects),
    ]
    catalog = {s.name: s.description for s in cfg.stunts}
    stunts = [Stunt(name=n, description=catalog.get(n, "")) for n in choices.stunts]
    refresh = required_refresh(cfg, len(choices.stunts))
    return FateSheet(
        skills=dict(choices.pyramid),
        aspects=aspects,
        stunts=stunts,
        refresh=refresh,
        fate_points=refresh,
    )


def validate_fate_sheet(sheet: FateSheet, cfg: FateConfig) -> list[str]:
    """Return a list of human-readable legality violations; empty == legal.

    Pure; no I/O. The single legality authority (design §9), reused by the engine,
    the wire contract, and the content validator. Checks: skill-pyramid shape vs
    ``chargen_pyramid``/``chargen_apex_rating``, skills in the pack list, mandatory
    non-empty High Concept + Trouble, the free-aspect count, stunts in the pack
    catalog, and the refresh invariant.
    """
    violations: list[str] = []

    # --- skill pyramid shape -------------------------------------------------
    expected = expected_rung_counts(cfg)
    placed = {name: rating for name, rating in sheet.skills.items() if rating > 0}
    actual_by_rating: dict[int, int] = {}
    for rating in placed.values():
        actual_by_rating[rating] = actual_by_rating.get(rating, 0) + 1
    rungs_desc = sorted(expected, reverse=True)
    for rating in sorted(actual_by_rating, reverse=True):
        if rating not in expected:
            violations.append(
                f"pyramid: rating {rating} is not a valid rung (apex {cfg.chargen_apex_rating}, "
                f"rungs {rungs_desc})"
            )
    for rating, want in expected.items():
        have = actual_by_rating.get(rating, 0)
        if have != want:
            violations.append(
                f"pyramid rung at rating {rating} expects {want} skill(s) but has {have}"
            )

    # --- skills belong to the pack -------------------------------------------
    for name in sheet.skills:
        if name not in cfg.skills:
            violations.append(f"skill {name!r} is not in the pack skill list")

    # --- mandatory aspects ---------------------------------------------------
    if not any(a.kind == "high_concept" and a.text.strip() for a in sheet.aspects):
        violations.append("missing or empty High Concept aspect")
    if not any(a.kind == "trouble" and a.text.strip() for a in sheet.aspects):
        violations.append("missing or empty Trouble aspect")
    free_aspects = [a for a in sheet.aspects if a.kind == "character"]
    if len(free_aspects) != cfg.free_aspect_count:
        violations.append(
            f"expected {cfg.free_aspect_count} free aspect(s), found {len(free_aspects)}"
        )
    if any(not a.text.strip() for a in free_aspects):
        violations.append("a free aspect is empty")

    # --- stunts belong to the catalog ----------------------------------------
    catalog = {s.name for s in cfg.stunts}
    for stunt in sheet.stunts:
        if stunt.name not in catalog:
            violations.append(f"stunt {stunt.name!r} is not in the pack stunt catalog")

    # --- refresh invariant ----------------------------------------------------
    want_refresh = required_refresh(cfg, len(sheet.stunts))
    if sheet.refresh != want_refresh:
        violations.append(
            f"refresh {sheet.refresh} violates the invariant (expected {want_refresh} for "
            f"{len(sheet.stunts)} stunt(s); base {cfg.refresh}, free_stunts {cfg.free_stunts})"
        )

    return violations
