"""``EntityStore`` — the typed universal index (ADR-118 §D3, Story 75-4).

Generalizes the live :class:`~sidequest.game.lore_store.LoreStore` machinery to
hold :class:`~sidequest.game.entity_card.EntityCard` objects typed by
``entity_type``. It reuses the same cosine ranking and the same embedding-worker
write-back contract — no new embedding model, no schema migration. Lore stays in
its own ``LoreStore``; this is the sibling index that shares the worker.

Per-turn floor+fill retrieval (75-5) and the dirty-flag reproject hook (75-6)
build on this store; this module is storage only.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Reuse the live cosine ranking (ADR-118 D3 / Don't Reinvent).
from sidequest.game.lore_store import cosine_similarity

from sidequest.game.entity_card import EntityCard


class DuplicateEntityId(Exception):
    """Raised by :meth:`EntityStore.add` when a card id already exists.

    Mirrors ``DuplicateLoreId`` — no silent overwrite of an existing card.
    """


class EntityStore(BaseModel):
    """In-memory collection of :class:`EntityCard` keyed by id.

    Save files serialize the full ``cards`` dict; embeddings and pending-retry
    bookkeeping round-trip untouched, exactly like ``LoreStore``.
    """

    model_config = {"extra": "forbid"}

    cards: dict[str, EntityCard] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, card: EntityCard) -> None:
        """Insert a card. Raises :class:`DuplicateEntityId` on a duplicate id."""
        if card.id in self.cards:
            raise DuplicateEntityId(f"duplicate id: {card.id}")
        self.cards[card.id] = card

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def query_by_type(self, entity_type: str) -> list[EntityCard]:
        """Return all cards of ``entity_type`` (cf. ``LoreStore.query_by_category``)."""
        return [c for c in self.cards.values() if c.entity_type == entity_type]

    def query_by_similarity(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        entity_type: str | None = None,
    ) -> list[tuple[float, EntityCard]]:
        """Return up to ``top_k`` cards ranked by cosine similarity against
        ``query_embedding``, optionally scoped to one ``entity_type``.

        Cards without an embedding are skipped (the worker fills them on a later
        pass). Sorted descending by similarity; ties broken by id for
        deterministic output — identical contract to ``query_by_similarity`` on
        ``LoreStore``.
        """
        candidates: list[tuple[float, EntityCard]] = []
        for card in self.cards.values():
            if card.embedding is None:
                continue
            if entity_type is not None and card.entity_type != entity_type:
                continue
            sim = cosine_similarity(query_embedding, card.embedding)
            candidates.append((sim, card))
        candidates.sort(key=lambda item: (-item[0], item[1].id))
        return candidates[: max(0, top_k)]

    # ------------------------------------------------------------------
    # Embedding worker contract
    # ------------------------------------------------------------------

    def pending_embedding_ids(self, *, max_retries: int | None = None) -> list[str]:
        """Return ids of cards awaiting an embedding, ordered by id.

        A card qualifies if ``embedding_pending`` is true and its
        ``embedding_retry_count`` is below ``max_retries`` (or ``max_retries`` is
        ``None``). Mirrors ``LoreStore.pending_embedding_ids``.
        """
        ids = [
            cid
            for cid, card in self.cards.items()
            if card.embedding_pending
            and (max_retries is None or card.embedding_retry_count < max_retries)
        ]
        return sorted(ids)

    def update_embedding(self, card_id: str, embedding: list[float]) -> None:
        """Attach an embedding to an existing card, clearing the pending flag and
        resetting the retry count.

        Raises ``KeyError`` if the id is unknown — a silent no-op would hide a
        genuine worker bug (No Silent Fallbacks), matching ``LoreStore``.
        """
        card = self.cards[card_id]
        card.embedding = list(embedding)
        card.embedding_pending = False
        card.embedding_retry_count = 0

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def total_tokens(self) -> int:
        """Sum of ``token_estimate`` across all cards (budget accounting)."""
        return sum(card.token_estimate for card in self.cards.values())

    def __len__(self) -> int:
        return len(self.cards)
