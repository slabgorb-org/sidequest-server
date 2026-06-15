"""Deterministic quest-seed minting (Story 117-3, ADR-146).

The engine seam beneath the ``quest_offer`` router subsystem. An authored
opening carries a ``tone.quest_seed`` (a typed :class:`QuestSeed`). At
chargen-complete :func:`stash_quest_offers` reads it onto
``snapshot.pending_quest_offers`` (resume-safe, keyed on ``quest_id``). When
the Intent Router classifies the player's turn as accepting that named offer at
high confidence, :func:`mint_quest_offer` mints a :class:`QuestEntry` into
``quest_log`` from the seed — no narrator tool call required — and fires the
``quest.seeded`` OTEL span.

Mirrors ``quest_seed.py::seed_quest_spine`` (the turn-0 drive spine): both write
the canonical ``quest_log`` / ``quest_anchors`` / ``active_stakes`` substrate,
both are **fill, not clobber**. The handler
(``agents/subsystems/quest_offer.py``) is a thin wrapper that reads ``params``
and calls :func:`mint_quest_offer`.
"""

from __future__ import annotations

from sidequest.game.session import (
    QUEST_LOG_CARDINALITY_CAP,
    GameSnapshot,
    QuestEntry,
)
from sidequest.genre.models.narrative import Opening
from sidequest.telemetry.spans import quest_seeded_span


def stash_quest_offers(snapshot: GameSnapshot, opening: Opening) -> None:
    """Populate ``snapshot.pending_quest_offers`` from the opening's quest_seed.

    Reads ``opening.tone.quest_seed`` (ADR-146 §1) and stashes it keyed on its
    ``quest_id``. Stashing alone is bait, NOT a mint (ADR-146 Alternative B
    rejected): it must not touch ``quest_log`` — minting waits for the router to
    classify acceptance. An opening with no seed is a no-op (authoring a seed is
    optional — ADR-146 "Neutral"); no phantom offer is created.
    """
    tone = getattr(opening, "tone", None)
    seed = getattr(tone, "quest_seed", None)
    if seed is None:
        return
    snapshot.pending_quest_offers[seed.quest_id] = seed


def mint_quest_offer(
    snapshot: GameSnapshot, quest_id: str, *, confidence: float
) -> QuestEntry | None:
    """Mint a ``QuestEntry`` from a stashed authored offer on acceptance.

    Copies the stashed :class:`QuestSeed`'s ``title``/``objective``/``anchor``
    into ``quest_log[quest_id]`` (a straight copy, not a re-derivation), flows
    the anchor into ``quest_anchors`` (dedup), seeds ``active_stakes`` from
    ``stakes`` **only when empty** (fill, don't clobber — consistent with
    ``seed_quest_spine``), consumes the offer, and fires ``quest.seeded``.
    Returns the minted entry.

    Idempotent (ADR-146 §3 "Authored seed is fill, not clobber"): if
    ``quest_id`` is already in ``quest_log`` — the narrator front-ran it via
    ``record_quest``, or the player re-accepts — this no-ops the MINT (first
    writer wins; no double-mint, no overwrite, no second span) and returns
    ``None``, but STILL consumes the pending offer so a taken job never leaks
    back into the router's offer surface every turn.

    Unknown ``quest_id`` (no matching pending offer) no-ops and returns ``None``;
    the handler (not this function) surfaces the mismatch.

    Cardinality cap (ADR-146 §3): a mint that would push ``quest_log`` past
    :data:`QUEST_LOG_CARDINALITY_CAP` raises loudly (No Silent Fallbacks) BEFORE
    consuming the offer — the offer is never silently dropped by a failed mint.
    """
    # Idempotent no-op: first writer wins (narrator front-ran via record_quest,
    # or the player re-accepts). The quest already exists — do NOT double-mint
    # and do NOT re-fire the span. But STILL consume the pending offer: the job
    # has been taken (by whichever path minted it first), so the offer must
    # leave the pending pool. Leaving it live would leak the offer back into the
    # router's <game_state> every turn, re-prompting acceptance of a quest that
    # is already in the log.
    if quest_id in snapshot.quest_log:
        snapshot.pending_quest_offers.pop(quest_id, None)
        return None

    seed = snapshot.pending_quest_offers.get(quest_id)
    if seed is None:
        # Unknown offer — the router named a quest_id with no pending seed. The
        # handler emits the mismatch; this function fabricates nothing.
        return None

    # Fail loud past the cap, BEFORE consuming the offer (No Silent Fallbacks) —
    # never silently drop the offer, never grow state unbounded. Honours the
    # SAME cap as record_quest (shared constant).
    if len(snapshot.quest_log) >= QUEST_LOG_CARDINALITY_CAP:
        raise ValueError(
            f"quest_log cardinality cap reached ({QUEST_LOG_CARDINALITY_CAP}); "
            f"cannot mint authored offer {quest_id!r}. Resolve or consolidate "
            "existing quests first."
        )

    snapshot.quest_log[quest_id] = QuestEntry(
        title=seed.title,
        objective=seed.objective,
        status="active",
        anchor_id=seed.anchor,
    )

    anchor_count = 0
    if seed.anchor:
        if seed.anchor not in snapshot.quest_anchors:
            snapshot.quest_anchors.append(seed.anchor)
        anchor_count = 1

    # Fill, don't clobber: a world-authored spine's stakes win (consistent with
    # seed_quest_spine's own doctrine).
    if seed.stakes and not snapshot.active_stakes:
        snapshot.active_stakes = seed.stakes

    # Consume the offer — taken bait earns promotion into persistent state and
    # leaves the pending pool (ADR-014).
    snapshot.pending_quest_offers.pop(quest_id, None)

    quest_seeded_span(
        quest_id=quest_id,
        title=seed.title,
        source="authored_seed",
        anchor_count=anchor_count,
        confidence=confidence,
    )
    return snapshot.quest_log[quest_id]


__all__ = ["mint_quest_offer", "stash_quest_offers"]
