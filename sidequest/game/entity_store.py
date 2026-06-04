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

    def upsert(self, card: EntityCard) -> bool:
        """Insert or refresh a card by id; return ``True`` if the store changed.

        The reproject-safe sibling of :meth:`add` (Story 75-6, ADR-118 §D2).
        Where ``add`` raises on a duplicate id, ``upsert`` is the per-turn write
        path the dirty-flag reproject uses: an entity re-projected with the same
        stable id (``npc:borin``) must not crash the turn.

        - **New id** → insert the card; return ``True``.
        - **Same id, identical ``content``** → no-op; the stored card (and its
          embedding) is left untouched so an unchanged cast does not churn the
          index; return ``False``.
        - **Same id, changed ``content``** → replace with the fresh projection
          and re-arm ``embedding_pending`` so the worker re-embeds the changed
          cast on its next pass; return ``True``.

        ``content`` is the equality key because the card embeds exactly that
        string — two projections with identical content embed to the identical
        vector, so re-embedding them would be pure churn (the projection
        determinism 75-4 guarantees is what makes this safe).
        """
        existing = self.cards.get(card.id)
        if existing is None:
            self.cards[card.id] = card
            return True
        if existing.content == card.content:
            return False
        # Content changed — replace and re-arm for re-embedding. The fresh card
        # already carries embedding_pending=True from the projector; set it
        # explicitly so the contract does not depend on the caller's construction.
        card.embedding_pending = True
        self.cards[card.id] = card
        return True

    def mark_embedding_failed(self, card_id: str) -> int:
        """Increment the retry counter for a card whose embed dispatch failed
        transiently. Returns the new count.

        Does not flip ``embedding_pending`` — the card stays queued so the next
        worker pass re-tries. Mirrors :meth:`LoreStore.mark_embedding_failed`.
        """
        card = self.cards[card_id]
        card.embedding_retry_count += 1
        return card.embedding_retry_count

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

    def update_embedding(
        self,
        card_id: str,
        embedding: list[float],
        *,
        expected_dim: int | None = None,
    ) -> bool:
        """Attach an embedding to an existing card, clearing the pending flag and
        resetting the retry count. Returns ``True`` on a successful write,
        ``False`` when a dimension guard refuses it.

        Raises ``KeyError`` if the id is unknown — a silent no-op would hide a
        genuine worker bug (No Silent Fallbacks), matching ``LoreStore``. Raises
        ``ValueError`` on an empty embedding: an empty vector would clear the
        pending flag while leaving the card un-rankable (cosine 0.0 forever),
        stranding it silently.

        ``expected_dim`` mirrors :meth:`LoreStore.update_embedding` (Story 76-3):
        when set and the vector length does not match, the write is *refused*
        (returns ``False``) so the card keeps its prior state and stays pending
        for re-embedding on the next worker pass. This defends against the
        retrieve/worker race where a mid-session daemon model-dim change
        (MiniLM-384 → -768) would otherwise write a stale-dimension vector back
        and clear the pending flag, orphaning the card (cosine 0.0 forever)
        until :meth:`requeue_dimension_mismatched` self-heals it a turn later.
        When ``expected_dim`` is ``None`` the write is unconditional — callers
        that do not track a session dim keep the pre-76-3 behavior.
        """
        if not embedding:
            raise ValueError("embedding must not be empty")
        if expected_dim is not None and len(embedding) != expected_dim:
            return False
        card = self.cards[card_id]
        card.embedding = list(embedding)
        card.embedding_pending = False
        card.embedding_retry_count = 0
        return True

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
