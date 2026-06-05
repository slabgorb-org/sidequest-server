"""Per-turn universal retrieval orchestration — the ADR-118 §D4 floor+fill seam.

Story 75-5. This is the glue between 75-2's deterministic *floor*
(:func:`~sidequest.agents.npc_context.build_npc_working_set` — scene-present NPCs
at full detail) and 75-4's indexed *fill*
(:class:`~sidequest.game.entity_store.EntityStore` cosine retrieval). Each
narrator turn:

1. **Floor** — assemble the scene-present working set (75-2). Always present.
2. **Fill** — embed the action text once (the live daemon path the lore RAG
   already uses), then semantic top-k over non-floor cards.
3. **Budget** — count the floor cost first; the fill consumes only the remainder
   of a hard per-turn token ceiling (``DEFAULT_ENTITY_BUDGET_TOKENS``).
4. **Sanitize** — every retrieved ``EntityCard.content`` passes through
   ``sanitize_player_text`` (ADR-047) at this single choke-point before it can
   reach the narrator prompt. Player-influenced fields (Yes-And NPC/faction
   names, location descriptions) are otherwise unsanitized in narrator-context
   assembly.
5. **Guard** — re-queue dimension-mismatched cards before the query, so a daemon
   model upgrade does not silently orphan the pre-upgrade index at 0.0 cosine.
6. **Observe** — emit the ``retrieval.universal`` span (the GM-panel lie
   detector) with the full ADR-118 §D5 attribute set.

This sibling of :func:`sidequest.game.lore_embedding.retrieve_lore_context`
NEVER raises: every failure path records an ``outcome`` and emits the span. Lore
retrieval continues in parallel under its own budget (unifying the two budgets
is the 75-7 follow-up).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from opentelemetry import trace

from sidequest.agents.npc_context import NpcWorkingSet, build_npc_working_set
from sidequest.daemon_client import (
    DaemonClient,
    DaemonRequestError,
    DaemonUnavailableError,
)
from sidequest.game.entity_card import EntityCard, EntityType
from sidequest.game.entity_store import EntityStore
from sidequest.game.lore_embedding import DEFAULT_RETRIEVAL_MIN_SIMILARITY
from sidequest.game.lore_store import _estimate_tokens
from sidequest.game.pertinence import (
    PertinenceScore,
    PertinenceSignals,
    score_card,
    select_within_budget,
    structured_signals_sufficient,
)
from sidequest.game.session import GameSnapshot
from sidequest.protocol.sanitize import sanitize_player_text

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# The ``retrieval.universal`` span name (ADR-118 §D5). Pinned to a constant so
# the emitter here and the 75-7 dashboard consumer agree on the exact string.
SPAN_UNIVERSAL_RETRIEVAL = "retrieval.universal"

# Per-turn token budget for ENTITY retrieval (floor + fill). The lore RAG runs
# under its own independent budget in v1; unifying them is 75-7. The contract is
# explicit: "this budget is for entities."
DEFAULT_ENTITY_BUDGET_TOKENS = 4000

# Semantic top-k for the fill. More generous than lore's (which retrieves one
# kind) because the entity index spans three types under one query.
DEFAULT_ENTITY_TOP_K = 8

# Outcome taxonomy (ADR-118 §D5 / context AC-6).
_OUTCOME_SUCCESS = "success"
_OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"
_OUTCOME_QUERY_FAILED = "query_failed"
_OUTCOME_NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True)
class RetrievedEntities:
    """The result of one ``retrieve_turn_context`` pass.

    ``floor`` carries the full-detail scene-present working set (75-2); it is
    always present (may be empty) and is *not* re-injected by 75-5's wiring (the
    floor already reaches the prompt via the existing npc-roster path). The three
    ``retrieved_*`` fields carry the SEMANTIC FILL only — ``None`` when that type
    retrieved nothing, so the wiring registers no empty Valley section
    (zero-byte-leak). Their cards' ``content`` is already sanitized.
    """

    floor: NpcWorkingSet
    retrieved_npcs: list[EntityCard] | None
    retrieved_locations: list[EntityCard] | None
    retrieved_factions: list[EntityCard] | None
    budget_total: int
    floor_count: int
    floor_token_cost: int
    fill_candidate_count: int
    fill_selected_count: int
    fill_token_cost: int
    rejected_below_similarity: int
    dimension_mismatch_count: int
    outcome: str
    # Story 84-1 (ADR-118 §A1): the unified scorer's per-turn signals. These are
    # ADDED to the §D4 shape — every field above is preserved so the renderer
    # (session_helpers) and watcher (universal_retrieval) keep working unchanged.
    #
    # ``embed_skipped`` — True when the drama-gate resolved the turn on structured
    # signals (mention + here) and the cosine embed was never computed; the WI-6
    # GM-panel surface reads this to show WHY the cosine pass was bypassed.
    # ``card_scores`` — the ``PertinenceScore`` decomposition for each selected
    # fill card (feeds the A5/WI-6 ``retrieval.card.reason`` per-card OTEL).
    #
    # Defaulted so legacy/fixture constructors don't break, but ALWAYS populated
    # by ``_finish`` on the live path (No Silent Fallbacks — never silently empty).
    embed_skipped: bool = False
    card_scores: list[PertinenceScore] = field(default_factory=list)


def _floor_token_cost(working_set: NpcWorkingSet) -> int:
    """Estimate the floor's prompt cost from the full-detail scene-present NPCs.

    Uses the same ``_estimate_tokens`` math as ``EntityCard`` / ``LoreFragment``
    so floor and fill accounting agree (ADR-118 §D3). Off-stage brief/compact
    tiers are not charged to the entity budget — they ride the existing roster
    section, not 75-5's fill.
    """
    total = 0
    for npc in working_set.full_profiles:
        core = npc.core
        text = " ".join(
            seg
            for seg in (
                core.name,
                getattr(core, "description", ""),
                getattr(core, "personality", ""),
            )
            if seg
        )
        total += _estimate_tokens(text)
    return total


def _floor_card_ids(working_set: NpcWorkingSet) -> set[str]:
    """The card ids the floor NPCs would project to, for fill dedup.

    A scene-present NPC already rides the prompt in full detail; its semantic
    card must not ALSO surface in the fill (session-file design decision). The id
    convention matches ``EntityCard.new(EntityType.NPC, slug)`` →
    ``"npc:<casefolded_underscored_name>"``.
    """
    ids: set[str] = set()
    for npc in working_set.full_profiles:
        name = npc.core.name
        if name.strip():
            ids.add(f"npc:{name.strip().casefold().replace(' ', '_')}")
    return ids


def _sanitize_card(card: EntityCard) -> EntityCard:
    """Return a copy of ``card`` with ``content`` run through the ADR-047
    sanitizer. The stored card is never mutated — the store owns truth
    (ADR-118 §D1); only the outbound, prompt-bound projection is cleaned."""
    return card.model_copy(update={"content": sanitize_player_text(card.content)})


def render_entity_section(label: str, cards: list[EntityCard]) -> str:
    """Render a fill tier into a prompt block for the Valley zone.

    Sibling of the lore ``<lore>`` block. ``label`` is the section tag
    (``retrieved_npcs`` / ``retrieved_locations`` / ``retrieved_factions``).
    """
    lines = [f"<{label}>"]
    lines.extend(f"- {card.content}" for card in cards)
    lines.append(f"</{label}>")
    return "\n".join(lines)


async def retrieve_turn_context(
    entity_store: EntityStore,
    snapshot: GameSnapshot,
    action_text: str,
    *,
    current_turn: int,
    budget_tokens: int = DEFAULT_ENTITY_BUDGET_TOKENS,
    player_referenced_npcs: set[str] | None = None,
    client: DaemonClient | None = None,
    min_similarity: float = DEFAULT_RETRIEVAL_MIN_SIMILARITY,
) -> RetrievedEntities:
    """Floor+fill entity retrieval for one narrator turn (ADR-118 §D4).

    Assembles the floor via :func:`build_npc_working_set`, embeds ``action_text``
    once via the daemon, queries ``entity_store`` for the semantic fill within the
    budget remaining after the floor, sanitizes retrieved content, and emits the
    ``retrieval.universal`` span. Never raises — daemon failure yields
    ``outcome="query_failed"`` with an empty fill and the span still fired.
    """
    with tracer.start_as_current_span(SPAN_UNIVERSAL_RETRIEVAL) as span:
        # --- Floor (always computed; independent of the daemon) ---
        floor = build_npc_working_set(
            snapshot,
            current_turn=current_turn,
            player_referenced_npcs=player_referenced_npcs,
        )
        floor_count = len(floor.full_profiles)
        floor_token_cost = _floor_token_cost(floor)
        remaining = budget_tokens - floor_token_cost

        # Defaults for the early-return (failure / empty) paths.
        dimension_mismatch_count = 0
        fill_candidate_count = 0
        fill_selected_count = 0
        fill_token_cost = 0
        rejected_below_similarity = 0
        selected: list[EntityCard] = []
        card_scores: list[PertinenceScore] = []
        # ADR-118 §A1 drama-gate state. ``embed_skipped`` flips True only when the
        # structured signals (mention + here) suffice and we deliberately bypass
        # the cosine embed. A daemon-down / blank-query degrade is a DIFFERENT
        # thing (query_failed) — it must NOT masquerade as a drama-gate skip
        # (No Silent Fallbacks), so it leaves embed_skipped False.
        embed_skipped = False

        def _finish(outcome: str) -> RetrievedEntities:
            by_type: dict[str, list[EntityCard]] = {
                EntityType.NPC: [],
                EntityType.LOCATION: [],
                EntityType.FACTION: [],
            }
            for card in selected:
                by_type.setdefault(card.entity_type, []).append(card)
            npc_cards = by_type.get(EntityType.NPC) or []
            loc_cards = by_type.get(EntityType.LOCATION) or []
            fac_cards = by_type.get(EntityType.FACTION) or []

            span.set_attribute("retrieval.budget_total", budget_tokens)
            span.set_attribute("retrieval.outcome", outcome)
            span.set_attribute("retrieval.floor_count", floor_count)
            span.set_attribute("retrieval.floor_token_cost", floor_token_cost)
            span.set_attribute("retrieval.fill_candidate_count", fill_candidate_count)
            span.set_attribute("retrieval.fill_selected_count", fill_selected_count)
            span.set_attribute("retrieval.fill_token_cost", fill_token_cost)
            span.set_attribute("retrieval.npc_count", len(npc_cards))
            span.set_attribute("retrieval.location_count", len(loc_cards))
            span.set_attribute("retrieval.faction_count", len(fac_cards))
            span.set_attribute("retrieval.rejected_below_similarity", rejected_below_similarity)
            span.set_attribute("retrieval.dimension_mismatch_count", dimension_mismatch_count)
            # Story 84-1 (ADR-118 §A1): the drama-gate observable. WI-6 reads this
            # on the GM panel to show whether the cosine pass was bypassed.
            span.set_attribute("retrieval.embed_skipped", embed_skipped)

            return RetrievedEntities(
                floor=floor,
                retrieved_npcs=npc_cards or None,
                retrieved_locations=loc_cards or None,
                retrieved_factions=fac_cards or None,
                budget_total=budget_tokens,
                floor_count=floor_count,
                floor_token_cost=floor_token_cost,
                fill_candidate_count=fill_candidate_count,
                fill_selected_count=fill_selected_count,
                fill_token_cost=fill_token_cost,
                rejected_below_similarity=rejected_below_similarity,
                dimension_mismatch_count=dimension_mismatch_count,
                outcome=outcome,
                embed_skipped=embed_skipped,
                card_scores=card_scores,
            )

        # --- Drama-gate (ADR-118 §A1): can structured signals resolve the turn? ---
        # The present-scene floor (mention + here) is the cheap structured signal.
        # ``here`` is strong when the scene has a present NPC; ``mention`` is strong
        # when the player named a roster NPC this turn. When both clear the gate the
        # cosine embed is SKIPPED entirely ("I attack Borin" resolves on mention +
        # location) — strictly cheaper than the §D4 always-embed fill. The floor
        # already carries the present scene to the prompt, so the fill stays empty.
        #
        # Mention is name-match only for now (WI-5/84-2 adds aliases); the seam is
        # ``player_referenced_npcs`` flowing in from the caller's word-bounded match.
        turn_signals = PertinenceSignals(
            mention=1.0 if player_referenced_npcs else 0.0,
            here=1.0 if floor.full_profiles else 0.0,
            # deferred: recency decay not yet wired — no card-level last_seen to
            # decay against; the gate resolves on mention + here only (see the
            # fill-scoring block below for the full deferral note).
            recency=0.0,
            sim=None,
            present_scene=bool(floor.full_profiles),
        )
        if structured_signals_sufficient(turn_signals):
            embed_skipped = True
            return _finish(_OUTCOME_SUCCESS)

        # --- Fill: embed the action text (graceful, recorded degradation) ---
        if client is None:
            client = DaemonClient()
        if not action_text.strip() or not client.is_available():
            logger.warning(
                "retrieve_turn_context query_failed reason=%s",
                "blank_query" if not action_text.strip() else "daemon_unavailable",
            )
            return _finish(_OUTCOME_QUERY_FAILED)
        try:
            response = await client.embed(action_text)
        except (DaemonUnavailableError, DaemonRequestError, ValueError) as exc:
            # Mirror retrieve_lore_context's failure taxonomy. The failure is
            # RECORDED in the span (No Silent Fallbacks), never swallowed into a
            # false empty success — and crucially NOT folded into a false
            # embed_skipped (a real daemon failure is not a drama-gate skip).
            logger.warning("retrieve_turn_context embed_failed error=%s", exc)
            return _finish(_OUTCOME_QUERY_FAILED)

        query_embedding = response["embedding"]

        # --- Guard: re-queue dimension-mismatched cards before the query ---
        dimension_mismatch_count = entity_store.requeue_dimension_mismatched(len(query_embedding))
        if dimension_mismatch_count:
            logger.warning(
                "retrieve_turn_context dimension_mismatch requeued=%d current_dim=%d",
                dimension_mismatch_count,
                len(query_embedding),
            )

        # --- Query + similarity floor + floor dedup ---
        floor_ids = _floor_card_ids(floor)
        all_hits = entity_store.query_by_similarity(query_embedding, top_k=DEFAULT_ENTITY_TOP_K)
        rejected_below_similarity = sum(1 for sim, _ in all_hits if sim < min_similarity)
        candidates = [
            (sim, card)
            for sim, card in all_hits
            if sim >= min_similarity and card.id not in floor_ids
        ]
        fill_candidate_count = len(candidates)

        # --- Budget: the floor is charged first; the fill gets the remainder ---
        if remaining <= 0:
            return _finish(_OUTCOME_BUDGET_EXHAUSTED)
        if not candidates:
            return _finish(_OUTCOME_NO_CANDIDATES)

        # --- Score (ADR-118 §A1): one weighted selection over the fill cards. The
        # fill is the topical-fallback tail — these cards carry the cosine ``sim``
        # signal (mention/here are the floor's job). present_scene=False:
        # the present scene rides the floor, never the fill. select_within_budget
        # ranks by score and admits within the remaining token budget.
        #
        # DEFERRED (recency decay not yet wired): the §A1 ``w_recency·decay`` term
        # is hardcoded to 0.0 here because ``EntityCard`` carries no
        # ``last_seen_turn`` field to decay against. This is an EXPLICIT,
        # documented zero — NOT a silent fallback that looks like a live term: the
        # w_recency weight is real (0.2) but its signal input is unavailable on the
        # card model until a later Epic-84 work item adds card-level recency
        # (the EntityCard projector / lifecycle scope, WI-2 84-5 territory). ---
        cards_by_id = {card.id: card for _, card in candidates}
        scored = [
            score_card(
                card,
                PertinenceSignals(
                    mention=0.0,
                    here=0.0,
                    recency=0.0,  # deferred: no EntityCard.last_seen_turn to decay (see above)
                    sim=sim,
                    present_scene=False,
                ),
            )
            for sim, card in candidates
        ]
        chosen = select_within_budget(scored, cards_by_id, budget_tokens=remaining)
        chosen_ids = {card.id for card in chosen}
        # Preserve descending-score order from the selector, sanitize on the way out.
        selected = [_sanitize_card(card) for card in chosen]
        card_scores = [ps for ps in scored if ps.card_id in chosen_ids]
        fill_token_cost = sum(card.token_estimate for card in chosen)
        fill_selected_count = len(selected)

        if not selected:
            # Candidates existed but none fit the remaining budget.
            return _finish(_OUTCOME_BUDGET_EXHAUSTED)
        return _finish(_OUTCOME_SUCCESS)
