"""Unified pertinence scorer — ADR-118 Amendment §A1 (Story 84-1, WI-1).

Supersedes §D4's two-mechanism floor/fill split (a binary scene-present floor
plus a similarity-ranked fill) with ONE weighted, scored selection:

    score(card) = w_mention·mention(card, action, aliases)   # dominant
                + w_location·here(card, snapshot)            # is it here / adjacent
                + w_recency·decay(card.last_seen, now)       # recently touched
                + w_sim·cosine(embed(action), card)          # topical fallback

Three hard rulings ride on top of the weighted sum:

  1. **Present-scene HARD invariant** — :func:`select_within_budget` admits every
     ``present_scene`` card first and unconditionally; the token budget governs
     only the remainder. A physically-engaged entity is exempt from the ceiling
     entirely (ADR-014 Living World: the current scene is never dropped).
  2. **Drama-gated embedding** — :func:`structured_signals_sufficient` returns
     True when mention + here come back strong, so the caller SKIPS the expensive
     daemon ``embed(action)`` round-trip. A skipped embed arrives as ``sim=None``
     and :func:`score_card` reports ``embed_used=False`` with a 0 sim contribution
     — never a phantom similarity (No Silent Fallbacks).
  3. **Per-type signal applicability** — :data:`SIGNAL_APPLICABILITY` declares,
     per :class:`~sidequest.game.entity_card.EntityType`, which of the four
     signals apply. A non-applicable signal contributes 0; the *weight* of each
     applicable signal is a single GLOBAL tuning vector
     (:data:`DEFAULT_PERTINENCE_WEIGHTS`) — one knob, no per-type weight terms.

This module is PURE: no I/O, no daemon. The orchestration
(:func:`sidequest.game.retrieval_orchestration.retrieve_turn_context`) computes
the raw signal values, decides whether to embed, and calls the scorer + selector.
"""

from __future__ import annotations

from dataclasses import dataclass

from sidequest.game.entity_card import EntityCard, EntityType

# ---------------------------------------------------------------------------
# Signal names — one string per term in the weighted sum.
# ---------------------------------------------------------------------------

SIGNAL_MENTION = "mention"
SIGNAL_HERE = "here"
SIGNAL_RECENCY = "recency"
SIGNAL_SIM = "sim"


@dataclass(frozen=True)
class PertinenceWeights:
    """The single GLOBAL tuning vector — one weight per signal.

    There is exactly one weight vector for the whole index; per-type *behavior*
    comes from :data:`SIGNAL_APPLICABILITY` (which signals apply), never from
    per-type weights.
    """

    w_mention: float
    w_location: float
    w_recency: float
    w_sim: float


# §A1 resolved lean: ``mention ≫ location > recency``. Mention is DOMINANT — it
# outweighs location + recency + sim combined, so a named entity always wins
# ranking. (1.0 > 0.4 + 0.2 + 0.3 = 0.9.)
DEFAULT_PERTINENCE_WEIGHTS = PertinenceWeights(
    w_mention=1.0,
    w_location=0.4,
    w_recency=0.2,
    w_sim=0.3,
)


# Per-EntityType applicable-signal matrix. Every EntityType MUST appear (No
# Silent Fallbacks: an un-declared type would silently score zero). ``here`` is
# load-bearing for an NPC and a location (both occupy the scene); a faction is
# diffuse — it is not physically "here" the way an NPC is, so ``here`` does not
# apply and cannot pollute its ranking.
SIGNAL_APPLICABILITY: dict[str, frozenset[str]] = {
    EntityType.NPC: frozenset({SIGNAL_MENTION, SIGNAL_HERE, SIGNAL_RECENCY, SIGNAL_SIM}),
    EntityType.LOCATION: frozenset({SIGNAL_MENTION, SIGNAL_HERE, SIGNAL_RECENCY, SIGNAL_SIM}),
    EntityType.FACTION: frozenset({SIGNAL_MENTION, SIGNAL_RECENCY, SIGNAL_SIM}),
    # Story 84-3 (WI-4, ADR-118 §A2): a relationship card surfaces because its NPC
    # is named/present (mention) or in the scene (here), or was recently touched
    # (recency). It carries NO ``sim`` — topical cosine matching is the NPC card's
    # job; the relationship rides the related NPC's structural signals, never a
    # free-floating embedding. Omitting ``sim`` means its contribution is dropped
    # (per-type applicability), not weighted.
    EntityType.RELATIONSHIP: frozenset({SIGNAL_MENTION, SIGNAL_HERE, SIGNAL_RECENCY}),
    # Story 84-5 (WI-2, ADR-118 §A2): a DORMANT quest / trope is NOT physically
    # present, so ``here`` does NOT apply (omitted → contributes 0). It surfaces by
    # NAME reference (mention) or TOPICAL similarity (sim), decayed by recency. Same
    # signal set for both — they are both recall-by-pertinence notes.
    EntityType.QUEST: frozenset({SIGNAL_MENTION, SIGNAL_RECENCY, SIGNAL_SIM}),
    EntityType.TROPE: frozenset({SIGNAL_MENTION, SIGNAL_RECENCY, SIGNAL_SIM}),
}


@dataclass(frozen=True)
class PertinenceSignals:
    """The raw, per-card computed signal values for one turn.

    ``sim`` is ``None`` when the drama-gate skipped the embed this turn — that is
    distinct from a real ``0.0`` cosine, and the scorer treats it honestly
    (``embed_used=False``, 0 contribution).
    """

    mention: float  # 0..1  alias/name match strength
    here: float  # 0..1  scene-present / adjacent
    recency: float  # 0..1  decayed last_seen
    sim: float | None  # 0..1 cosine, or None when the embed was skipped
    present_scene: bool  # the player is physically engaging this entity


@dataclass(frozen=True)
class PertinenceScore:
    """The scored, per-signal-decomposed result for one card.

    This struct is the A5/WI-6 ``retrieval.card.reason`` OTEL payload — the
    per-signal contributions let the GM panel show WHY a card was selected.
    """

    card_id: str
    mention_contribution: float
    here_contribution: float
    recency_contribution: float
    sim_contribution: float
    score: float  # the weighted sum (applicable signals only)
    present_scene: bool
    embed_used: bool  # False when the drama-gate skipped cosine


# The drama-gate threshold: mention + here are "strong" when both clear this bar.
# A named, scene-present action ("I attack Borin") has mention≈1 and here≈1, so
# the structured signals suffice and the cosine pass is skipped.
_STRUCTURED_SUFFICIENT_THRESHOLD = 0.5


def structured_signals_sufficient(signals: PertinenceSignals) -> bool:
    """The drama-gate predicate (SOUL: Cost Scales with Drama).

    Returns True when mention AND here come back strong enough that the cheap
    structured signals already resolve the turn — the caller then SKIPS the
    expensive ``embed(action)`` cosine fallback. Returns False for a thin action
    (named nothing, nowhere specific), which must run the embed as fallback.
    """
    return (
        signals.mention >= _STRUCTURED_SUFFICIENT_THRESHOLD
        and signals.here >= _STRUCTURED_SUFFICIENT_THRESHOLD
    )


def _applicable_signals(card: EntityCard) -> frozenset[str]:
    """Which signals apply to this card's entity type.

    Fails loud on an un-declared type (No Silent Fallbacks): a missing entry
    would silently zero every signal rather than surface the authoring gap.
    """
    applicable = SIGNAL_APPLICABILITY.get(card.entity_type)
    if applicable is None:
        raise ValueError(
            f"entity type {card.entity_type!r} has no SIGNAL_APPLICABILITY entry — "
            "add it to the matrix"
        )
    return applicable


def score_card(
    card: EntityCard,
    signals: PertinenceSignals,
    weights: PertinenceWeights = DEFAULT_PERTINENCE_WEIGHTS,
) -> PertinenceScore:
    """Score one card: ``Σ wᵢ·signalᵢ`` over the APPLICABLE signals only.

    A non-applicable signal contributes 0 (no nonsense terms). A skipped embed
    (``signals.sim is None``) contributes 0 to sim and sets ``embed_used=False``;
    a present cosine contributes ``w_sim·sim`` and sets ``embed_used=True``.
    """
    applicable = _applicable_signals(card)

    mention_contribution = (
        weights.w_mention * signals.mention if SIGNAL_MENTION in applicable else 0.0
    )
    here_contribution = weights.w_location * signals.here if SIGNAL_HERE in applicable else 0.0
    recency_contribution = (
        weights.w_recency * signals.recency if SIGNAL_RECENCY in applicable else 0.0
    )

    embed_used = signals.sim is not None
    if embed_used and SIGNAL_SIM in applicable:
        sim_contribution = weights.w_sim * signals.sim  # type: ignore[operator]
    else:
        sim_contribution = 0.0

    score = mention_contribution + here_contribution + recency_contribution + sim_contribution

    return PertinenceScore(
        card_id=card.id,
        mention_contribution=mention_contribution,
        here_contribution=here_contribution,
        recency_contribution=recency_contribution,
        sim_contribution=sim_contribution,
        score=score,
        present_scene=signals.present_scene,
        embed_used=embed_used,
    )


def select_within_budget(
    scores: list[PertinenceScore],
    cards_by_id: dict[str, EntityCard],
    *,
    budget_tokens: int,
) -> list[EntityCard]:
    """Rank + admit cards under a per-turn token budget, present-scene exempt.

    The hard invariant (§A1): every ``present_scene`` card is admitted FIRST and
    unconditionally — exempt from the token ceiling, even at a zero budget. The
    remaining budget then admits the non-present cards in descending score order.
    Returns the selected cards (present-scene first, then by score).
    """
    selected: list[EntityCard] = []
    selected_ids: set[str] = set()

    # 1) Present-scene cards: admitted unconditionally, no budget charge.
    for ps in scores:
        if ps.present_scene:
            card = cards_by_id.get(ps.card_id)
            if card is not None and ps.card_id not in selected_ids:
                selected.append(card)
                selected_ids.add(ps.card_id)

    # 2) The rest: descending score, charged against the remaining budget.
    remaining = budget_tokens
    rest = sorted(
        (ps for ps in scores if not ps.present_scene),
        key=lambda ps: ps.score,
        reverse=True,
    )
    for ps in rest:
        card = cards_by_id.get(ps.card_id)
        if card is None or ps.card_id in selected_ids:
            continue
        if card.token_estimate > remaining:
            continue
        selected.append(card)
        selected_ids.add(ps.card_id)
        remaining -= card.token_estimate

    return selected
