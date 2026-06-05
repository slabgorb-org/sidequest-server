"""``EntityCard`` — a uniform, embeddable projection of a world entity.

Foundation slice of the ADR-118 universal retrieval layer (Story 75-4). An
``EntityCard`` is a structural sibling of :class:`~sidequest.game.lore_store.LoreFragment`:
it carries the identical embedding-worker contract (``embedding`` /
``embedding_pending`` / ``embedding_retry_count``) so the *same* embedding worker
and cosine machinery that already serve lore can index NPCs, locations, and
factions — typed by ``entity_type``, no new model, no migration (ADR-118 §D3).

This module owns the card model and the per-type projectors. The typed index
lives in :mod:`sidequest.game.entity_store`. Per-turn retrieval orchestration
(75-5), the dirty-flag reproject hook (75-6), and OTEL *emission* (75-7) are
later stories — this module only defines the OTEL attribute *names* they share.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

# Reuse the live token-estimate math (ADR-118 D3 / Don't Reinvent). The card's
# token budget must agree with LoreFragment's so 75-5's floor+fill accounting is
# honest across both stores.
from sidequest.game.lore_store import _estimate_tokens
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.genre.models.lore import Faction

if TYPE_CHECKING:
    # Story 76-6: the projector accepts both the identity-only pool member and
    # the stateful ``Npc``. Imported under TYPE_CHECKING only — ``session`` pulls
    # in this module's siblings, so a runtime import would risk a cycle. The
    # ``from __future__ import annotations`` above keeps the union annotation lazy.
    from sidequest.game.disposition import DispositionBeat
    from sidequest.game.session import Npc


class EntityType(StrEnum):
    """The entity kinds the universal index holds (ADR-118 §D2). ``StrEnum`` so
    members compare equal to their string values and serialize naturally into
    card ids, OTEL attributes, and JSON — the same pattern as ``Attitude``."""

    NPC = "npc"
    LOCATION = "location"
    FACTION = "faction"
    # Story 84-3 (WI-4, ADR-118 §A2): the relationship between the PC and an NPC,
    # projected SUMMARY-tier from the disposition_log. A sibling of the npc card —
    # the NPC card carries identity, the relationship card carries standing + why.
    RELATIONSHIP = "relationship"


# Card-id namespace per entity type. ADR-118 §D3 ids are ``npc:borin``,
# ``loc:black_hart``, ``faction:tide_syndicate`` — note ``location`` abbreviates
# to ``loc`` in the id while the ``entity_type`` field stays ``"location"``.
# Centralized here so every projector and ``EntityCard.new`` share one
# convention. ``StrEnum`` keys hash equal to their string values, so a plain
# ``"location"`` lookup also resolves.
_ID_NAMESPACE: dict[str, str] = {
    EntityType.NPC: "npc",
    EntityType.LOCATION: "loc",
    EntityType.FACTION: "faction",
    # ``rel:<slug>`` — distinct from ``npc:<slug>`` so the two coexist in the index.
    EntityType.RELATIONSHIP: "rel",
}


# ---------------------------------------------------------------------------
# OTEL attribute names (ADR-118 §D5) — DEFINED here, EMITTED by 75-5/75-7.
# Pinning the strings now keeps the emitters consistent and lets 75-4 tests
# assert the contract without any span machinery.
# ---------------------------------------------------------------------------

SPAN_CARD_REPROJECT_COUNT = "card_reproject_count"
SPAN_STALE_CARD_COUNT = "stale_card_count"

# ADR-138 §D6 — the defensive-eviction span. A card stranded on a now-unprojectable
# entity is evicted loud, never served stale (No Silent Fallbacks). The span name is
# the ADR literal; ``reason`` is pinned to ``unprojectable`` to leave room for other
# eviction causes without colliding the schema. Emitted by the dispatch tier
# (:mod:`sidequest.server.dispatch.entity_sync`), one span per evicted card.
SPAN_ENTITY_CARD_EVICTED = "entity_card.evicted"
ENTITY_CARD_EVICTED_REASON_UNPROJECTABLE = "unprojectable"

UNIVERSAL_RETRIEVAL_SPAN_ATTRS: frozenset[str] = frozenset(
    {
        "retrieval.budget_total",
        "retrieval.outcome",
        "retrieval.floor_count",
        "retrieval.floor_token_cost",
        "retrieval.fill_candidate_count",
        "retrieval.fill_selected_count",
        "retrieval.fill_token_cost",
        "retrieval.npc_count",
        "retrieval.location_count",
        "retrieval.faction_count",
        # ADR-118 §A2 (84-3, WI-4): RELATIONSHIP-card fill count.
        "retrieval.relationship_count",
        "retrieval.rejected_below_similarity",
        "retrieval.dimension_mismatch_count",
        # ADR-118 §A1 (84-1) drama-gate observable. 84-1 EMITTED this on the span
        # but never added it to the contract set — 84-4 (WI-6) closes that gap.
        "retrieval.embed_skipped",
        # ADR-118 §A5 (84-4, WI-6): per-card score decomposition (JSON-encoded
        # list) + the A3 forgetting-lifecycle zero-state counters (logic is WI-3).
        "retrieval.card.reason",
        "retrieval.tier_demotions",
        "retrieval.tier_rehydrations",
        "retrieval.vectors_shed",
    }
)


# ---------------------------------------------------------------------------
# EntityCard — sibling of LoreFragment
# ---------------------------------------------------------------------------


class EntityCard(BaseModel):
    """A uniform, embeddable projection of one world entity (ADR-118 §D3).

    The card owns no truth — ``entity_ref`` is a back-pointer to the
    system-of-record struct (``NpcPoolMember`` / room source / ``Faction``);
    the live entity remains authoritative (ADR-118 §D1).
    """

    model_config = {"extra": "forbid"}

    id: str
    entity_type: str
    entity_ref: str
    content: str = Field(min_length=1)
    token_estimate: int
    metadata: dict[str, str] = Field(default_factory=dict)
    # Identical worker contract to LoreFragment so the existing embedding
    # worker drains EntityCards unchanged.
    embedding: list[float] | None = None
    embedding_pending: bool = True
    embedding_retry_count: int = 0

    @field_validator("content")
    @classmethod
    def _content_must_not_be_blank(cls, v: str) -> str:
        """Whitespace-only content would embed to a degenerate vector — reject
        at the construction boundary, mirroring ``LoreFragment``."""
        if not v.strip():
            raise ValueError("content must not be blank or whitespace-only")
        return v

    @classmethod
    def new(
        cls,
        entity_type: str,
        entity_id: str,
        content: str,
        *,
        entity_ref: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> EntityCard:
        """Build a card with a stable namespaced id and a computed token
        estimate.

        ``id`` is ``f"{namespace}:{entity_id}"`` where the namespace comes from
        ``_ID_NAMESPACE`` (e.g. ``"npc:borin"``, ``"loc:black_hart"``). The token
        estimate is always derived from ``content`` (never caller-supplied) using
        the same math as ``LoreFragment``. Cards start ``embedding_pending=True``
        so the worker picks them up on the next pass.

        Raises ``ValueError`` on an ``entity_type`` not registered in
        ``_ID_NAMESPACE`` or a blank ``entity_id`` — both would otherwise yield a
        malformed id (No Silent Fallbacks: fail loud at the factory boundary).
        """
        type_str = str(entity_type)
        if entity_type not in _ID_NAMESPACE:
            raise ValueError(f"unknown entity_type {entity_type!r} — add it to _ID_NAMESPACE")
        if not str(entity_id).strip():
            raise ValueError("entity_id must not be blank or whitespace-only")
        namespace = _ID_NAMESPACE[entity_type]
        return cls(
            id=f"{namespace}:{entity_id}",
            entity_type=type_str,
            entity_ref=entity_ref if entity_ref is not None else entity_id,
            content=content,
            token_estimate=_estimate_tokens(content),
            metadata=dict(metadata or {}),
        )


# ---------------------------------------------------------------------------
# Per-type projectors (ADR-118 §D3)
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Stable, case-folded id fragment. Epic-72 NPC identity is a case-folded
    name string; factions follow the same convention.

    Raises ``ValueError`` on a blank/whitespace name — a blank name would slug to
    ``""`` and produce a degenerate id like ``"npc:"`` (No Silent Fallbacks: fail
    at the boundary closest to the authoring mistake).
    """
    if not name.strip():
        raise ValueError("entity name must not be blank or whitespace-only")
    return name.strip().casefold().replace(" ", "_")


def npc_card_id(name: str) -> str:
    """The stable card id an NPC named ``name`` projects to (``npc:<slug>``).

    Single source of the NPC id convention, shared by :func:`project_npc_card`
    (which builds the same id via ``EntityCard.new``) and the ADR-138 §D5
    defensive-eviction lookup, which must compute the would-be card id for a
    now-unprojectable member *without* projecting it. Raises ``ValueError`` on a
    blank name (via :func:`_slug`) — a blank-named phantom could never have
    produced a card, so the eviction caller treats that as "nothing to evict".
    """
    return f"{_ID_NAMESPACE[EntityType.NPC]}:{_slug(name)}"


def project_npc_card(npc: NpcPoolMember | Npc) -> EntityCard:
    """Project an NPC — pool member *or* stateful ``Npc`` — into a card.

    Content carries name, role, pronouns, and the disposition *attitude band*
    (not the raw int) so retrieval keys on relationship. Deterministic: the same
    entity state yields the same content every time (ADR-118 §D3 dual-rep risk —
    75-6's reproject relies on it).

    Story 76-6: the two sources carry their name differently — a
    ``NpcPoolMember`` at ``.name``, a stateful ``Npc`` at ``.core.name`` (and it
    has no ``role``). Both expose ``.pronouns`` and a ``.disposition``
    (``Disposition``), so only name/role need source-specific extraction. The
    stateful name comes from ``CreatureCore``, whose validator rejects blanks, so
    a stateful card is always projectable (the ``_slug`` blank guard fires only
    for pool members).
    """
    aliases: list[str] = []
    if isinstance(npc, NpcPoolMember):
        name = npc.name
        role = npc.role
    else:
        # Stateful Npc — name lives on the nested CreatureCore; no role field.
        name = npc.core.name
        role = None
        # Story 84-2 (WI-5): only the stateful Npc carries accreted aliases.
        aliases = list(npc.aliases)
    segments: list[str] = [name]
    if role:
        segments.append(role)
    if npc.pronouns:
        segments.append(npc.pronouns)
    segments.append(npc.disposition.attitude().value)
    content = " — ".join(segments)
    # Story 84-2 (WI-5): carry the aliases into card metadata so the mention
    # resolver can read epithets off the indexed card. JSON-encoded and SORTED so
    # the same alias SET projects to identical metadata regardless of list order —
    # 75-6's dirty-flag reproject depends on this determinism. Aliases never enter
    # ``content`` (they must not pollute the retrieval embedding vector). Only set
    # the key when there are aliases — an empty NPC projects no bogus value.
    metadata = {"aliases": json.dumps(sorted(aliases))} if aliases else None
    return EntityCard.new(
        EntityType.NPC,
        _slug(name),
        content,
        entity_ref=name,
        metadata=metadata,
    )


def select_load_bearing_beats(
    log: list[DispositionBeat], *, limit: int = 3
) -> list[DispositionBeat]:
    """Pick the most-recent NON-ZERO-delta beats from a disposition log (84-3, §A2).

    A relationship card SUMMARIZES the ADR-136 cap-10 ``disposition_log`` — it cannot
    embed the whole time series. The load-bearing beats are the ones where the
    standing actually MOVED (non-zero delta), most-recent first, capped at ``limit``
    (default 3). A zero-delta beat (nothing moved) is not load-bearing and is
    dropped; an empty or all-zero log yields ``[]`` (the caller falls back to the
    attitude band alone — never a blank card).
    """
    nonzero = [b for b in log if b.delta != 0]
    # Most-recent first. The log is appended chronologically (record_disposition_beat),
    # so reversing gives recency order deterministically.
    return list(reversed(nonzero))[:limit]


def project_relationship_card(npc: Npc) -> EntityCard:
    """Project the PC↔NPC relationship into a SUMMARY-tier card (84-3, WI-4, §A2).

    Content = the 5-level attitude BAND (reused from ADR-136 ``band_for`` — the
    5-level display band, NOT the engine 3-level ``Attitude``) plus the 2-3
    load-bearing beat reasons (:func:`select_load_bearing_beats`). NEVER the full
    cap-10 log. An empty / all-zero log yields an attitude-band-only card (never
    blank — ``EntityCard`` rejects blank content). Born at SUMMARY tier
    (``metadata['tier']='summary'``, §A3) — never FULL.

    Deterministic for the 75-6 reproject: the same ``Npc`` state yields byte-
    identical content + metadata. Aliases ride ``metadata['aliases']`` as a sorted
    JSON list (like :func:`project_npc_card`). Id is ``rel:<slug>`` — distinct from
    the NPC's ``npc:<slug>`` card so both coexist in the index.
    """
    from sidequest.game.projection.relationships import band_for

    name = npc.core.name
    band = band_for(int(npc.disposition))
    beats = select_load_bearing_beats(npc.disposition_log)
    segments: list[str] = [name, band]
    segments.extend(b.reason for b in beats if b.reason.strip())
    content = " — ".join(segments)

    # Tier rides metadata (§A3 — summary, never full). Aliases sorted (75-6
    # reproject determinism), like the NPC card. ``tier`` is always present so a
    # consumer never has to guess; aliases only when there are any.
    metadata: dict[str, str] = {"tier": "summary"}
    if npc.aliases:
        metadata["aliases"] = json.dumps(sorted(npc.aliases))
    return EntityCard.new(
        EntityType.RELATIONSHIP,
        _slug(name),
        content,
        entity_ref=name,
        metadata=metadata,
    )


def project_faction_card(faction: Faction) -> EntityCard:
    """Project a faction into an embeddable card (name, summary, attitude)."""
    segments: list[str] = [faction.name, faction.summary]
    if faction.disposition:
        segments.append(faction.disposition)
    # Filter blank segments (a blank summary would otherwise embed a degenerate
    # "Name — " fragment) — same discipline as project_location_card.
    content = " — ".join(seg for seg in segments if seg)
    return EntityCard.new(
        EntityType.FACTION,
        _slug(faction.name),
        content,
        entity_ref=faction.name,
    )


def project_location_card(
    *,
    location_id: str,
    name: str,
    description: str,
    source: str | None = None,
    mechanical_properties: dict[str, str] | None = None,
    linked_npcs: list[str] | None = None,
) -> EntityCard:
    """Project a location into an embeddable card from a *normalized view*.

    Locations are diffuse across the room graph, ``world_materialization``, and
    PG ``location_promotions`` (ADR-118 §D3 / §Consequences). The per-source
    adaptation belongs to the consumer (``dispatch.entity_sync``); this projector
    takes the already-normalized fields. A location with no projectable
    description fails loud — no silent placeholder card.

    Story 76-7: ``source`` records WHICH diffuse source minted the card
    (``"room_graph"`` / ``"world_materialization"`` / ``"promotion"``). It rides
    in ``metadata["source"]`` — the existing card metadata channel, no new field
    — so a promotion-sourced card is distinguishable from a room-graph one for
    GM-panel forensics and tiered forgetting (ADR-118 amendment). The tag never
    enters ``content``: provenance must not pollute the retrieval vector.
    """
    if not description.strip():
        raise ValueError("location description must not be blank or whitespace-only")
    segments: list[str] = [name, description]
    if mechanical_properties:
        segments.append("; ".join(f"{k}: {v}" for k, v in mechanical_properties.items()))
    if linked_npcs:
        segments.append("NPCs: " + ", ".join(linked_npcs))
    content = " — ".join(seg for seg in segments if seg)
    return EntityCard.new(
        EntityType.LOCATION,
        location_id,
        content,
        entity_ref=location_id,
        metadata={"source": source} if source is not None else None,
    )
