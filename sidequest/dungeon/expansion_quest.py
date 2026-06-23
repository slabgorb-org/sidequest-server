"""Per-expansion quest lifecycle (ADR-137 × ADR-106): select signature beat,
seed a ledger thread, project into quest_log, resolve on the beat.
Deterministic — no LLM (Amendment C)."""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.dungeon.region_graph.model import Expansion, RegionNode
from sidequest.dungeon.themes import ExpansionQuestTemplate
from sidequest.game.cookbook.models import RegionContentManifest


@dataclass(frozen=True)
class SignatureBinding:
    kind: str            # "big_bad" | "set_piece" | "reach_deep" (effective, post-degrade)
    ref_id: str          # bound element id: region id, or big_bad name, or set_piece id
    anchor_region: str   # the region id the quest anchors to
    title: str
    objective: str
    degraded: bool


def _deepest(expansion: Expansion) -> RegionNode:
    """Return the node with the highest depth_score; None scores as -inf so
    attached (scored) nodes always win over unattached ones."""
    return max(
        expansion.new_nodes,
        key=lambda n: (n.depth_score if n.depth_score is not None else float("-inf")),
    )


def _fill(text: str, *, theme: str, big_bad: str, anchor: str) -> str:
    return (
        text.replace("{theme}", theme)
            .replace("{big_bad}", big_bad)
            .replace("{anchor}", anchor)
    )


def select_signature(
    *,
    expansion: Expansion,
    manifests_by_region: dict[str, RegionContentManifest],
    template: ExpansionQuestTemplate,
) -> SignatureBinding:
    """Pick the expansion's signature beat from a theme quest_template and the
    per-region content manifests.  Pure logic — no I/O, no randomness."""
    deepest = _deepest(expansion)
    theme = deepest.theme

    if template.signature == "big_bad":
        # Deepest region (by depth_score) that rolled a big_bad.
        candidates = sorted(
            (
                n
                for n in expansion.new_nodes
                if (manifests_by_region.get(n.id) or _empty_manifest()).big_bad
            ),
            key=lambda n: (n.depth_score if n.depth_score is not None else float("-inf")),
            reverse=True,
        )
        if candidates:
            node = candidates[0]
            bb = manifests_by_region[node.id].big_bad or {}
            name = str(bb.get("name", "")).strip() or "the master of this place"
            return SignatureBinding(
                kind="big_bad",
                ref_id=name,
                anchor_region=node.id,
                title=_fill(template.title, theme=theme, big_bad=name, anchor=node.id),
                objective=_fill(template.objective, theme=theme, big_bad=name, anchor=node.id),
                degraded=False,
            )
        # No big_bad rolled — loud degrade to reach_deep (caller emits the span).
        return _bind_reach_deep(template, deepest, theme, degraded=True)

    if template.signature == "set_piece":
        sp = (template.set_piece_id or "").strip()
        return SignatureBinding(
            kind="set_piece",
            ref_id=sp,
            anchor_region=deepest.id,
            title=_fill(template.title, theme=theme, big_bad="", anchor=deepest.id),
            objective=_fill(template.objective, theme=theme, big_bad="", anchor=deepest.id),
            degraded=False,
        )

    return _bind_reach_deep(template, deepest, theme, degraded=False)


def _bind_reach_deep(
    template: ExpansionQuestTemplate,
    deepest: RegionNode,
    theme: str,
    *,
    degraded: bool,
) -> SignatureBinding:
    return SignatureBinding(
        kind="reach_deep",
        ref_id=deepest.id,
        anchor_region=deepest.id,
        title=_fill(template.title, theme=theme, big_bad="", anchor=deepest.id),
        objective=_fill(template.objective, theme=theme, big_bad="", anchor=deepest.id),
        degraded=degraded,
    )


def _empty_manifest() -> RegionContentManifest:
    """Null object for regions not yet in the manifest dict."""
    return RegionContentManifest(
        race="",
        cr_band="",
        size_budget={},
        wandering_table=[],
        loot_table=[],
        special_rooms=[],
        big_bad=None,
    )
