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

from sidequest.game.entity_card import EntityCard

# Reuse the live cosine ranking (ADR-118 D3 / Don't Reinvent).
from sidequest.game.lore_store import cosine_similarity


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

    def requeue_dimension_mismatched(self, current_dim: int) -> int:
        """Flip ``embedding_pending`` back to ``True`` for every card whose stored
        embedding dimension differs from ``current_dim``.

        Mirrors :meth:`LoreStore.requeue_dimension_mismatched` (ADR-118 §D5,
        Story 75-4 forward-flag). Guards against the silent-orphan failure where a
        daemon model upgrade (MiniLM-384 → -768) leaves every pre-upgrade card
        scoring 0.0 against every query because :func:`cosine_similarity` returns
        0.0 on length mismatch. Without this re-queue, ``update_embedding``
        permanently clears the pending flag with no log, span, or GM-panel signal.

        Returns the number of cards re-queued so the caller can emit the
        ``retrieval.dimension_mismatch_count`` OTEL attribute. Called from
        :func:`sidequest.game.retrieval_orchestration.retrieve_turn_context` before
        the similarity query; the next worker pass re-embeds the re-queued cards on
        the current model. A non-positive ``current_dim`` is a no-op (a zero-length
        embedding from the daemon must not trigger a cascade wipe of the index).
        """
        if current_dim <= 0:
            return 0
        count = 0
        for card in self.cards.values():
            if card.embedding is None:
                continue
            if len(card.embedding) != current_dim:
                card.embedding = None
                card.embedding_pending = True
                card.embedding_retry_count = 0
                count += 1
        return count

    def update_embedding(self, card_id: str, embedding: list[float]) -> None:
        """Attach an embedding to an existing card, clearing the pending flag and
        resetting the retry count.

        Raises ``KeyError`` if the id is unknown — a silent no-op would hide a
        genuine worker bug (No Silent Fallbacks), matching ``LoreStore``. Raises
        ``ValueError`` on an empty embedding: an empty vector would clear the
        pending flag while leaving the card un-rankable (cosine 0.0 forever),
        stranding it silently.
        """
        if not embedding:
            raise ValueError("embedding must not be empty")
        card = self.cards[card_id]
        card.embedding = list(embedding)
        card.embedding_pending = False
        card.embedding_retry_count = 0

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def total_tokens(self) -> int:
        """Sum of ``token_estimate`` across all cards (budget accounting).

        A method, not a property, to match the sibling ``LoreStore.total_tokens``
        call convention — a caller switching between the two stores gets the same
        ``.total_tokens()`` shape (ADR-118 D3 *Don't Reinvent*).
        """
        return sum(card.token_estimate for card in self.cards.values())

    def __len__(self) -> int:
        return len(self.cards)
