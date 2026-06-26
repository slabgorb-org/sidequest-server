"""Re-resolve a region's theme against its OWN final depth_score (Story 158-37).

The materializer's design stage picks each new region's theme from a ``theme_pool``
gated by the frontier edge's single ``spawn_depth_score`` — BEFORE
``assign_depth_scores`` computes each region's final, independent ``depth_score``
(ordinary-route hop distance from the entrance). A region spawned off a deep
frontier can therefore be themed "deep" and then land shallow (a short stitch
route to the surface), ending up wearing a theme its own ``depth_band`` excludes.
The 2026-06-23 beneath_sunden playtest caught exactly this: ``exp011.r2`` themed
``bone_crypt`` (band ``{min:30}``) at ``depth_score=8.216``.

This module owns the correction. After the final depth_scores are known, every
new region whose theme is no longer eligible at its own depth is re-themed
deterministically from the themes eligible at that depth. Regions whose theme is
still eligible are left untouched — that preserves the random-dungeon grab-bag
variety (Keith, 2026-06-24: depth tunes encounter difficulty, themes stay a broad
grab-bag) and only corrects the genuine band violations.

No Silent Fallbacks (CLAUDE.md): a region with no ``depth_score`` (the function
ran before ``assign_depth_scores``) or a region whose own depth has NO eligible
theme in the palette (a content gap) raises loudly — never a clamp, never a
silent default theme.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field

from sidequest.dungeon.region_graph.model import Expansion, RegionGraph
from sidequest.dungeon.themes import ThemePalette, theme_eligible_at_depth


@dataclass(frozen=True)
class ThemeResolution:
    """One region re-themed because its original theme's band excluded its
    own final depth_score."""

    region_id: str
    from_theme: str
    to_theme: str
    depth_score: float


@dataclass
class ThemeResolutionReport:
    """Span-ready record of the corrections applied (mirrors DepthReport /
    GenerationReport's ``as_dict()`` precedent — the GM-panel lie-detector
    surface for this fix)."""

    resolutions: list[ThemeResolution] = field(default_factory=list)

    @property
    def resolved_count(self) -> int:
        return len(self.resolutions)

    def as_dict(self) -> dict:
        return {
            "resolved_count": self.resolved_count,
            "resolutions": [
                {
                    "region_id": r.region_id,
                    "from_theme": r.from_theme,
                    "to_theme": r.to_theme,
                    "depth_score": r.depth_score,
                }
                for r in self.resolutions
            ],
        }


def _pick_index(*, campaign_seed: int, expansion_id: int, region_id: str, n: int) -> int:
    """Deterministic index in ``[0, n)`` for the replacement theme.

    blake2b sub-seed (NOT XOR) — mirrors ``depth.depth_jitter`` /
    ``generator._subseed`` and refuses to reproduce the ``seed ^ 0x5EED``
    fixed-point-at-24301 class of bug (Beneath Sünden carry-forward gotcha).
    """
    digest = hashlib.blake2b(
        f"{campaign_seed}|theme_resolve|{expansion_id}|{region_id}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") % n


def resolve_themes_for_final_depth(
    graph: RegionGraph,
    expansion: Expansion,
    palette: ThemePalette,
    *,
    campaign_seed: int,
    expansion_id: int,
) -> ThemeResolutionReport:
    """Make every new region's theme eligible at its OWN final ``depth_score``.

    Reads each new region's frozen ``depth_score`` from ``graph`` (so
    ``assign_depth_scores`` must already have run on ``graph``). For a region
    whose current theme is no longer eligible at that depth, re-picks
    deterministically from ``palette.themes_for_depth(depth)`` and rewrites the
    theme on BOTH ``graph.nodes[id]`` (preserving the frozen ``depth_score``) and
    the matching ``expansion.new_nodes`` entry (the object the fill / curate /
    attach stages consume downstream). Regions already eligible are untouched.

    Raises loudly (No Silent Fallbacks):
      - a new region with no ``depth_score`` (ran before ``assign_depth_scores``);
      - a region whose own depth has NO eligible theme (a palette/content gap).
    """
    report = ThemeResolutionReport()
    for idx, original in enumerate(expansion.new_nodes):
        rid = original.id
        scored = graph.nodes[rid]
        if scored.depth_score is None:
            raise ValueError(
                f"region {rid!r} has no depth_score; resolve_themes_for_final_depth "
                f"must run AFTER assign_depth_scores (No Silent Fallbacks)"
            )
        depth = scored.depth_score
        current = palette.get(scored.theme)
        if theme_eligible_at_depth(current, depth):
            continue

        eligible = palette.themes_for_depth(depth)
        if not eligible:
            bands = {tid: (t.depth_band.min, t.depth_band.max) for tid, t in palette.themes.items()}
            raise ValueError(
                f"region {rid!r} at depth_score={depth!r} has NO eligible theme in "
                f"the palette (theme bands: {bands}); cannot re-resolve a "
                f"depth-coherent theme. Widen a theme's depth_band so every reachable "
                f"depth is covered. No silent default theme."
            )

        new_theme = eligible[
            _pick_index(
                campaign_seed=campaign_seed,
                expansion_id=expansion_id,
                region_id=rid,
                n=len(eligible),
            )
        ].id
        graph.nodes[rid] = dataclasses.replace(scored, theme=new_theme)
        expansion.new_nodes[idx] = dataclasses.replace(original, theme=new_theme)
        report.resolutions.append(
            ThemeResolution(
                region_id=rid,
                from_theme=original.theme,
                to_theme=new_theme,
                depth_score=depth,
            )
        )
    return report
