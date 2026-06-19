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

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sidequest.game.fate_sheet import Aspect, FateSheet, Stunt
from sidequest.genre.models.rules import FateConfig, FateHintSeed

if TYPE_CHECKING:
    from sidequest.genre.models.pack import GenrePack


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


def pyramid_violations(allocation: dict[str, int], cfg: FateConfig) -> list[str]:
    """Pyramid-shape + skills-in-pack violations for a ``{skill: rating}`` allocation.

    The skill-allocation subset of :func:`validate_fate_sheet`, factored out so the
    live render mirror of the ``fate_skill_pyramid`` step can reuse the one authority
    (No duplicate rules). Pure; no I/O."""
    violations: list[str] = []
    expected = expected_rung_counts(cfg)
    placed = {name: rating for name, rating in allocation.items() if rating > 0}
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
    for name in allocation:
        if name not in cfg.skills:
            violations.append(f"skill {name!r} is not in the pack skill list")
    return violations


def select_chargen_seed(
    seed_table: dict[str, FateHintSeed],
    *,
    class_hint: str | None = None,
    rpg_role_hint: str | None = None,
    background: str | None = None,
) -> tuple[str, FateHintSeed] | None:
    """Pick the narrative-chargen seed for the accumulated hints (story 126-24).

    Precedence ``class_hint`` > ``rpg_role_hint`` > ``background``: the first hint whose
    VALUE is a key in ``seed_table`` wins. ``seed_table`` is the world-resolved table the
    builder holds (or the genre-tier ``cfg.chargen_seed_table`` fallback). Returns
    ``(matched_hint, seed)`` or ``None`` when no hint matches a table key — the builder then
    presents a blank pyramid for the player to rank by hand (No Silent Fallbacks: never
    fabricate a seed for an unmapped vocation). Pure; no I/O."""
    for hint in (class_hint, rpg_role_hint, background):
        if hint and hint in seed_table:
            return hint, seed_table[hint]
    return None


def resolve_fate_chargen_seed_table(
    pack: GenrePack | None, world_slug: str | None
) -> dict[str, FateHintSeed]:
    """Resolve the effective narrative-chargen seed table for the active world (story
    126-24, AC2). The genre-tier ``rules.fate.chargen_seed_table`` UNIONED with the active
    world's ``chargen_seed_table`` by hint key — world wins on a shared key (ADR-121 layered
    per-field resolution; the same world-wins by-key rule ``resolve_fate_gear_catalog`` uses
    for gear). A falsy/unknown world, or a world that authors no seed table, resolves to the
    pure genre baseline. Emits a merge watcher event carrying ``world_override_applied`` so
    the GM panel can confirm a world's seed overrides were WIRED, not merely authored."""
    fate_cfg = pack.rules.fate if pack is not None else None
    genre_table: dict[str, FateHintSeed] = (
        dict(fate_cfg.chargen_seed_table) if fate_cfg is not None else {}
    )

    world = pack.worlds.get(world_slug) if (pack is not None and world_slug) else None
    world_table: dict[str, FateHintSeed] = (
        dict(getattr(world, "chargen_seed_table", {}) or {}) if world is not None else {}
    )
    if not world_table:
        # Pure genre baseline (no world, unknown world, or world ships no seed table).
        return genre_table

    merged: dict[str, FateHintSeed] = dict(genre_table)
    overridden = sum(1 for k in world_table if k in merged)
    merged.update(world_table)  # world wins on a shared hint key

    _emit_fate_seed_table_merged(
        world_slug=world_slug or "",
        genre_count=len(genre_table),
        world_hints=list(world_table),
        overridden=overridden,
        merged_count=len(merged),
    )
    return merged


def _emit_fate_seed_table_merged(
    *,
    world_slug: str,
    genre_count: int,
    world_hints: list[str],
    overridden: int,
    merged_count: int,
) -> None:
    """Emit a ``state_transition`` watcher event for the world∪genre chargen seed-table
    merge (OTEL Observability Principle, story 126-24 AC6): the GM panel confirms a world's
    chargen seed overrides were WIRED into the effective table — ``world_override_applied``
    is the lie-detector boolean. Mirrors ``fate_gear._emit_fate_gear_merged``."""
    from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

    _watcher_publish(
        "state_transition",
        {
            "field": "resolved_fate_chargen_seed_table",
            "op": "merged",
            "world_slug": world_slug,
            "tier": "world",
            "genre_count": genre_count,
            "world_hints": world_hints,
            "overridden": overridden,
            "world_override_applied": overridden > 0,
            "merged_count": merged_count,
        },
        component="genre",
    )


def stunt_catalog_violations(stunts: list[str], cfg: FateConfig) -> list[str]:
    """Catalog-membership violations for a stunt-name list (the live ``fate_stunts``
    mirror subset of :func:`validate_fate_sheet`). Pure; no I/O."""
    catalog = {s.name for s in cfg.stunts}
    return [f"stunt {n!r} is not in the pack stunt catalog" for n in stunts if n not in catalog]


def validate_fate_sheet(sheet: FateSheet, cfg: FateConfig) -> list[str]:
    """Return a list of human-readable legality violations; empty == legal.

    Pure; no I/O. The single legality authority (design §9), reused by the engine,
    the wire contract, and the content validator. Checks: skill-pyramid shape vs
    ``chargen_pyramid``/``chargen_apex_rating``, skills in the pack list, mandatory
    non-empty High Concept + Trouble, the free-aspect count, stunts in the pack
    catalog, and the refresh invariant. Composes the factored ``pyramid_violations``
    / ``stunt_catalog_violations`` subsets so a rule lives in exactly one place.
    """
    violations: list[str] = []

    # --- skill pyramid shape + skills belong to the pack ---------------------
    violations.extend(pyramid_violations(sheet.skills, cfg))

    # --- mandatory aspects ---------------------------------------------------
    if not any(a.kind == "high_concept" and a.text.strip() for a in sheet.aspects):
        violations.append("missing or empty High Concept aspect")
    if not any(a.kind == "trouble" and a.text.strip() for a in sheet.aspects):
        violations.append("missing or empty Trouble aspect")
    free_aspects = [a for a in sheet.aspects if a.kind == "character"]
    # Free aspects are OPTIONAL at chargen (seeded + refined in play — story 121-8
    # AC1 / epic 121). ``free_aspect_count`` is the upper bound, not an exact
    # requirement; 0..N is legal. A present free aspect must still be non-empty.
    if len(free_aspects) > cfg.free_aspect_count:
        violations.append(
            f"too many free aspects: at most {cfg.free_aspect_count} allowed, "
            f"found {len(free_aspects)}"
        )
    if any(not a.text.strip() for a in free_aspects):
        violations.append("a free aspect is empty")

    # --- stunts belong to the catalog ----------------------------------------
    violations.extend(stunt_catalog_violations([s.name for s in sheet.stunts], cfg))

    # --- refresh invariant ----------------------------------------------------
    want_refresh = required_refresh(cfg, len(sheet.stunts))
    if sheet.refresh != want_refresh:
        violations.append(
            f"refresh {sheet.refresh} violates the invariant (expected {want_refresh} for "
            f"{len(sheet.stunts)} stunt(s); base {cfg.refresh}, free_stunts {cfg.free_stunts})"
        )

    return violations
