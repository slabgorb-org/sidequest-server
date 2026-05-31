"""Runtime lore accretion — bridge KnownFacts into the lore RAG (story 75-1).

Restoration of the Rust ``accumulate_and_persist_lore`` loop
(``crates/sidequest-server/src/dispatch/lore_sync.rs:26-120``). Runtime
narrator-discovered facts (:class:`KnownFact`) are minted into the lore
store as ``GameEvent`` :class:`LoreFragment` entries so the existing embed
worker picks them up and the next turn's RAG retrieval can find them —
instead of dead-ending in the un-embedded ``known_facts`` list.

Mirrors the established ``seed_lore_from_arc_promotion`` /
``mint_threshold_lore`` conventions: a deterministic per-fact id drives
idempotent-by-id minting, and the sweep returns a result dataclass so the
dispatch layer can emit GM-panel telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sidequest.daemon_client.client import MAX_EMBED_BYTES
from sidequest.game.character import KnownFact
from sidequest.game.lore_store import (
    DuplicateLoreId,
    LoreCategory,
    LoreFragment,
    LoreSource,
    LoreStore,
)
from sidequest.protocol.models import FactCategory

# Total map FactCategory -> LoreCategory. Every variant has a home, or
# accretion would silently drop a class of facts from retrieval.
_FACT_TO_LORE: dict[FactCategory, str] = {
    FactCategory.Lore: LoreCategory.History,
    FactCategory.Place: LoreCategory.Geography,
    FactCategory.Person: LoreCategory.Character,
    FactCategory.Quest: LoreCategory.Event,
    FactCategory.Ability: LoreCategory.Item,
}


def fact_category_to_lore_category(category: FactCategory) -> str:
    """Map a ``KnownFact`` category to its ``LoreCategory`` home.

    Fails loud on an unmapped enum addition rather than silently bucketing
    facts into a default category (No Silent Fallbacks).
    """
    try:
        return _FACT_TO_LORE[category]
    except KeyError as exc:  # pragma: no cover - guards future enum additions
        raise ValueError(f"unmapped FactCategory: {category!r}") from exc


@dataclass
class AccretionResult:
    """Outcome of one accretion sweep (mirrors ``ArcSeedResult`` shape)."""

    accreted: int = 0
    skipped_duplicate: int = 0
    skipped_blank: int = 0
    skipped_oversized: int = 0
    fragment_ids: list[str] = field(default_factory=list)


def _fragment_id(fact: KnownFact) -> str:
    """Deterministic id so the every-turn sweep is idempotent."""
    return f"lore_kf_{fact.fact_id}"


def accrete_facts_to_lore(
    store: LoreStore,
    facts: list[KnownFact],
    *,
    interaction: int,
    pc_name: str,
) -> AccretionResult:
    """Mint a :class:`LoreFragment` for each not-yet-accreted ``KnownFact``.

    Idempotent: a fact already accreted on a prior turn collides on its
    deterministic id and is counted as ``skipped_duplicate`` — never
    re-minted and never surfacing :class:`DuplicateLoreId` (which would
    crash the turn). Blank-content facts cannot form a valid fragment
    (``content`` has ``min_length=1``) and are skipped explicitly as
    ``skipped_blank`` rather than silently minted broken. Content exceeding
    ``MAX_EMBED_BYTES`` is rejected loud at the boundary as ``skipped_oversized``
    instead of being minted and then failing embedding silently downstream.
    """
    result = AccretionResult()
    for fact in facts:
        if not fact.content.strip():
            result.skipped_blank += 1
            continue
        # Reject oversized content loud at the minting boundary. Without this
        # the fragment is minted, then fails embedding downstream (exceeds
        # MAX_EMBED_BYTES), gets retry-counted, and is silently dropped from
        # the pending queue — a starving-index failure with no minting signal.
        if len(fact.content.encode("utf-8")) > MAX_EMBED_BYTES:
            result.skipped_oversized += 1
            continue
        frag_id = _fragment_id(fact)
        fragment = LoreFragment.new(
            id=frag_id,
            category=fact_category_to_lore_category(fact.category),
            content=fact.content,
            source=LoreSource.GameEvent,
            turn_created=interaction,
            metadata={"fact_id": fact.fact_id, "pc_name": pc_name},
        )
        try:
            store.add(fragment)
        except DuplicateLoreId:
            result.skipped_duplicate += 1
            continue
        result.accreted += 1
        result.fragment_ids.append(frag_id)
    return result
