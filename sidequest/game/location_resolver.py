"""resolve_location_entity — pure-Python resolver. Story 54-6 / ADR-109.

Two modes encode the Zork-Problem-safe split:

* ``narrator_proactive`` — narrator is the source of the entity name.
  Manifest miss = contract violation. ``resolved=False``; the narrator's
  pending mechanical action does not commit. (Protects the contract.)
* ``player_initiated`` — player is the source. Manifest miss =
  canonization. A new ``yes_and_minted`` entity is written to
  ``location_promotions`` and the player's action proceeds. (Honors
  Yes-And and the Zork doctrine.)

A ``flavor_only`` entity engaged with ``engagement_kind="mechanical"``
auto-promotes to ``yes_and`` (Diamonds-and-Coal). Pure mentions
(``engagement_kind="mention"``) are descriptive — no mutation.

Authored YAML is never mutated. All runtime mutation accumulates in the
``location_promotions`` SQLite table.

This module is pure-Python and intentionally tool-agnostic — the agent
tool layer (``agents/tools/resolve_location_entity.py``) is a thin
adapter that translates between the tool-call shape and this API.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from sidequest.game.pg.promotions import PgLocationPromotionRow
from sidequest.protocol.models import (
    EncounterLocationOverlay,
    LocationEntity,
    LocationEntityResolution,
)
from sidequest.telemetry.spans.reference import reference_url_skipped_span

ResolverMode = Literal["narrator_proactive", "player_initiated"]
EngagementKind = Literal["mention", "mechanical"]


# ---------------------------------------------------------------------------
# Label normalisation
# ---------------------------------------------------------------------------

_LEADING_ARTICLE_RE = re.compile(r"^\s*(the|a|an)\s+", re.IGNORECASE)
_ID_TRIM_RE = re.compile(r"[^a-z0-9]+")


def _normalize(label: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", label).strip().lower()


def _id_from_label(label: str) -> str:
    """Stable id from a player-supplied label. Lower-snake-ish; collisions
    are resolved by the ON CONFLICT UPDATE behaviour of upsert."""
    base = _ID_TRIM_RE.sub("_", _normalize(label)).strip("_")
    return base or "minted_entity"


# ---------------------------------------------------------------------------
# Effective manifest
# ---------------------------------------------------------------------------


def _apply_promotion(authored: LocationEntity, row: PgLocationPromotionRow) -> LocationEntity:
    """Layer a promotion row on top of an authored entity. Returns a NEW
    ``LocationEntity`` via ``model_copy`` — never mutates input."""
    return authored.model_copy(
        update={
            "tier": row.new_tier,
            "provenance": row.provenance,
            "promoted_at_turn": row.promoted_at_turn,
            "promoted_canon": row.promoted_canon,
        }
    )


def _minted_entity_from_row(row: PgLocationPromotionRow) -> LocationEntity:
    # Pack and world are not in scope at this pure-function level — the resolver
    # is called from multiple paths and deliberately carries no session context.
    # Emit a skipped span for observability; the reference_url is None.
    # A per-world registry tightening (attach URL when pack/world flow through
    # the tool adapter) is deferred until the loader exposes locations.yaml as
    # a runtime list. See Task 9 commit body.
    with reference_url_skipped_span(
        kind="location",
        pack="",
        world=None,
        keys=(row.label,),
        reason="no_pack_or_world_in_scope",
    ):
        pass
    return LocationEntity(
        id=row.entity_id,
        label=row.label,
        tier=row.new_tier,
        binding=None,
        affordances=[],
        provenance=row.provenance,
        promoted_at_turn=row.promoted_at_turn,
        promoted_canon=row.promoted_canon,
        reference_url=None,
    )


def _build_effective_manifest(
    *,
    authored: Iterable[LocationEntity],
    promotions: list[PgLocationPromotionRow],
    overlays: Iterable[EncounterLocationOverlay] = (),
) -> list[tuple[LocationEntity, bool]]:
    """Return ``(entity, from_promotion)`` for each effective entity.

    Read order (ADR-109 §5.5):
        authored (with promotion-row overlay)
        + active overlays' entity_delta (encounter arrival order)
        + minted-only promotion rows (no authored entity to layer on top of)

    ``overlays`` defaults to ``()`` for backward compatibility with the
    54-6 resolver contract. Overlay entities are encounter-scoped — they
    are never written to ``location_promotions`` — so they are tagged
    ``from_promotion=False``.
    """
    authored_list = list(authored)
    by_authored_id = {e.id: e for e in authored_list}
    promotions_by_id = {r.entity_id: r for r in promotions}

    result: list[tuple[LocationEntity, bool]] = []

    for entity in authored_list:
        row = promotions_by_id.get(entity.id)
        if row is not None:
            result.append((_apply_promotion(entity, row), True))
        else:
            result.append((entity, False))

    for overlay in overlays:
        for ent in overlay.entity_delta:
            result.append((ent, False))

    for row in promotions:
        if row.entity_id not in by_authored_id:
            result.append((_minted_entity_from_row(row), True))

    return result


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------


def _match_label(
    label: str, manifest: list[tuple[LocationEntity, bool]]
) -> tuple[LocationEntity, bool] | None:
    needle = _normalize(label)
    if not needle:
        return None
    for entity, from_promotion in manifest:
        if _normalize(entity.label) == needle:
            return entity, from_promotion
    return None


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------


def _promote_flavor_to_yes_and(
    *,
    store: object,
    region_id: str,
    entity: LocationEntity,
    turn_number: int,
) -> LocationEntity:
    row = PgLocationPromotionRow(
        region_id=region_id,
        entity_id=entity.id,
        provenance="yes_and_promoted",
        label=entity.label,
        promoted_at_turn=turn_number,
        # v1: canon defaults to the label. Narrator-supplied canon arrives
        # in Story 54-8 via OTEL when prose is captured.
        promoted_canon=entity.label,
        new_tier="yes_and",
        new_binding_kind=(entity.binding.kind if entity.binding is not None else None),
        new_binding_ref=(entity.binding.ref if entity.binding is not None else None),
    )
    store.upsert_location_promotion(row)
    return _apply_promotion(entity, row)


def _mint_yes_and(
    *,
    store: object,
    region_id: str,
    label: str,
    turn_number: int,
) -> LocationEntity:
    entity_id = _id_from_label(label)
    row = PgLocationPromotionRow(
        region_id=region_id,
        entity_id=entity_id,
        provenance="yes_and_minted",
        label=label,
        promoted_at_turn=turn_number,
        promoted_canon=label,
        new_tier="yes_and",
        new_binding_kind=None,
        new_binding_ref=None,
    )
    store.upsert_location_promotion(row)
    return _minted_entity_from_row(row)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def resolve(
    *,
    store: object,
    region_id: str,
    authored_entities: Iterable[LocationEntity],
    label: str,
    mode: ResolverMode,
    engagement_kind: EngagementKind = "mention",
    turn_number: int,
    overlays: Iterable[EncounterLocationOverlay] = (),
) -> LocationEntityResolution:
    """Resolve ``label`` in ``region_id`` against the effective manifest.

    Returns a ``LocationEntityResolution`` describing what happened. See
    module docstring for the full two-mode contract. ``overlays`` lets
    callers pass active encounter overlays (Story 54-7) so overlay-only
    entities can be matched without being persisted to promotions.
    """
    promotions = store.list_location_promotions(region_id=region_id)  # type: ignore[union-attr]
    manifest = _build_effective_manifest(
        authored=authored_entities, promotions=promotions, overlays=overlays
    )

    hit = _match_label(label, manifest)

    if hit is None:
        if mode == "narrator_proactive":
            return LocationEntityResolution(
                resolved=False,
                entity=None,
                mode_outcome="no_match",
                region_id=region_id,
                from_promotion=False,
            )
        # player_initiated miss → mint a new yes_and entity.
        minted = _mint_yes_and(
            store=store,
            region_id=region_id,
            label=label,
            turn_number=turn_number,
        )
        return LocationEntityResolution(
            resolved=True,
            entity=minted,
            mode_outcome="minted",
            region_id=region_id,
            from_promotion=True,
        )

    entity, from_promotion = hit

    # Diamonds-and-Coal: flavor_only entities promote on mechanical
    # engagement, regardless of mode.
    if entity.tier == "flavor_only" and engagement_kind == "mechanical":
        promoted = _promote_flavor_to_yes_and(
            store=store,
            region_id=region_id,
            entity=entity,
            turn_number=turn_number,
        )
        return LocationEntityResolution(
            resolved=True,
            entity=promoted,
            mode_outcome="promoted",
            region_id=region_id,
            from_promotion=True,
        )

    return LocationEntityResolution(
        resolved=True,
        entity=entity,
        mode_outcome="matched",
        region_id=region_id,
        from_promotion=from_promotion,
    )
