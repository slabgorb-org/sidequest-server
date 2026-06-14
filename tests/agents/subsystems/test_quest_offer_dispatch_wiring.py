"""RED wiring tests — Story 117-3 (ADR-146) — the ``quest_offer`` subsystem.

The deterministic acceptance trigger rides ADR-113's Intent Router. We add one
subsystem, ``quest_offer``, that mints an authored ``QuestSeed`` into
``quest_log`` when the router classifies the player's turn as accepting a named
pending offer at high confidence — no narrator tool call required.

**Router is STUBBED.** Per project lore, a real intent-router LLM pass makes
these tests flaky; the router's classification *is* the input to this layer, so
we inject it deterministically by constructing the ``SubsystemDispatch``
(``subsystem="quest_offer"``, params ``{quest_id, decision}``, ``confidence``)
directly and driving the REAL ``run_dispatch_bank`` / handler. No LLM runs.

Contract under test (TEA-defined for Dev), from ADR-146 §2/§4:

* ``sidequest/agents/subsystems/quest_offer.py`` exports
  ``run_quest_offer_dispatch`` and it is registered in the dispatch
  ``_REGISTRY`` under ``"quest_offer"`` (``_register_defaults``).
* THE WIRING TEST (mandatory): an opening-with-seed → stash → accept dispatch
  through the REAL bank → ``quest_log`` non-empty → the QUESTS projection
  reports ``quests >= 1``. Proves the subsystem is connected end-to-end, not
  that helpers work in isolation.
* ``decision="decline"`` does NOT mint — the offer is consumed declined.
* An accept naming an UNKNOWN offer emits the ``quest_offer`` mismatch witness
  rather than minting a phantom quest.
* The engagement watcher has a ``quest_offer`` witness (added to ``_WITNESSES``)
  that fires ``dispatch_engagement.quest_offer.mismatch`` when the router
  dispatched ``quest_offer accept`` but ``quest_log`` gained nothing (ADR-146
  §4 — the structurally-sound replacement for the keyword lie-detector).

These import names that do not exist yet (the handler, the snapshot field,
``QuestSeed``) — collection / registry lookup fails until Dev (117-3 GREEN)
lands all three wiring connections. That is the intended RED.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sidequest.game.quest_offer import stash_quest_offers

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.narrative import (
    Opening,
    OpeningSetting,
    OpeningTone,
    OpeningTrigger,
    QuestSeed,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

SPAN_NAME = "quest.seeded"


# ---------------------------------------------------------------------------
# Builders — synthetic only
# ---------------------------------------------------------------------------


def _seed(quest_id: str = "floor_boss_missing_person") -> QuestSeed:
    return QuestSeed(
        quest_id=quest_id,
        title="The Floor-Boss's Missing Person",
        objective="Find out who the floor-boss has lost in the under-levels.",
        stakes="a corporate favour owed",
        anchor="under_levels_beacon",
        giver="the Conglomerate floor-boss",
    )


def _opening(seed: QuestSeed | None) -> Opening:
    return Opening(
        id="solo_synthetic_arrival",
        triggers=OpeningTrigger(),
        setting=OpeningSetting(location_label="New Kowloon docks", situation="just arrived"),
        tone=OpeningTone(register="noir", quest_seed=seed),
        establishing_narration="The lift doors part on a wall of neon and rain.",
    )


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _quest_offer_dispatch(
    *,
    quest_id: str = "floor_boss_missing_person",
    decision: str = "accept",
    confidence: float = 0.9,
    idempotency_key: str = "k-quest-offer-1",
) -> SubsystemDispatch:
    """The STUBBED router output: a deterministic quest_offer classification.

    This stands in for the IntentRouter's Haiku pass — we inject what the
    router would have classified, so no LLM runs and the test is deterministic.
    """
    return SubsystemDispatch(
        subsystem="quest_offer",
        params={"quest_id": quest_id, "decision": decision},
        idempotency_key=idempotency_key,
        confidence=confidence,
        visibility=_open_viz(),
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Rux",
                raw_action="Alright, I'll find out who's missing.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _snap_with_offer(seed: QuestSeed | None = None) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    stash_quest_offers(snap, _opening(seed if seed is not None else _seed()))
    return snap


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# Registration — the handler is reachable from the dispatch bank
# ---------------------------------------------------------------------------


def test_quest_offer_handler_is_registered() -> None:
    """The bank's _REGISTRY must carry quest_offer → run_quest_offer_dispatch.
    Without registration, _REGISTRY.get('quest_offer') is None and the dispatch
    logs unknown_subsystem — silently dropping the mint."""
    from sidequest.agents.subsystems import get_registered

    registry = get_registered()
    assert "quest_offer" in registry, (
        f"quest_offer handler not registered; bank has {sorted(registry)}. "
        "Story 117-3 must add the registration in "
        "sidequest/agents/subsystems/__init__.py:_register_defaults()."
    )
    fn = registry["quest_offer"]
    assert callable(fn) and getattr(fn, "__name__", "") == "run_quest_offer_dispatch", (
        f"registered quest_offer handler should be run_quest_offer_dispatch; got {fn!r}"
    )


# ---------------------------------------------------------------------------
# THE WIRING TEST (mandatory) — opening-with-seed → accept → quests >= 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_accept_through_bank_mints_and_projects(otel_capture) -> None:
    """End-to-end through the REAL dispatch bank: an authored opening seed,
    stashed as a pending offer, accepted at high confidence, mints a QuestEntry
    AND the QUESTS projection reports quests >= 1. This is the load-bearing
    wiring proof — the subsystem is connected, not merely importable."""
    from sidequest.agents.subsystems import run_dispatch_bank
    from sidequest.game.projection.quests import build_quests_payload

    snap = _snap_with_offer()
    package = _package_with(_quest_offer_dispatch(decision="accept", confidence=0.9))

    await run_dispatch_bank(
        package,
        context={"snapshot": snap, "pack": MagicMock(), "player_name": "Rux"},
    )

    # quest_log non-empty — the mint landed through the real bank path.
    assert snap.quest_log, (
        "dispatch bank did not engage quest_offer — handler unregistered, "
        "signature mismatch, or no-op'd silently"
    )
    assert "floor_boss_missing_person" in snap.quest_log

    # quests.emitted-equivalent: the QUESTS projection reports quests >= 1.
    payload = build_quests_payload(snap)
    assert len(payload.quest_log) >= 1, "QUESTS projection must report quests >= 1 after accept"
    assert any(e.quest_id == "floor_boss_missing_person" for e in payload.quest_log)

    # The authored-mint span fired through the real path.
    assert len(_spans_named(otel_capture, SPAN_NAME)) == 1


# ---------------------------------------------------------------------------
# (e) decline does NOT mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decline_through_bank_does_not_mint(otel_capture) -> None:
    from sidequest.agents.subsystems import run_dispatch_bank

    snap = _snap_with_offer()
    package = _package_with(_quest_offer_dispatch(decision="decline", confidence=0.9))

    await run_dispatch_bank(
        package,
        context={"snapshot": snap, "pack": MagicMock(), "player_name": "Rux"},
    )

    assert snap.quest_log == {}, "a declined offer must NOT mint a quest"
    assert len(_spans_named(otel_capture, SPAN_NAME)) == 0, "decline must not fire quest.seeded"
    # The offer is consumed (declined) — not left dangling for a re-prompt.
    assert "floor_boss_missing_person" not in snap.pending_quest_offers


# ---------------------------------------------------------------------------
# Low-confidence accept degrades to a hint (no phantom mint) — bank gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_accept_degrades_and_does_not_mint() -> None:
    """The bank's per-subsystem confidence gate (default 0.6) protects the mint:
    an ambiguous turn below threshold degrades to a narrator hint, never fires
    the engine (ADR-146 §2 'an ambiguous turn scores low and degrades')."""
    from sidequest.agents.subsystems import run_dispatch_bank

    snap = _snap_with_offer()
    package = _package_with(_quest_offer_dispatch(decision="accept", confidence=0.2))

    result = await run_dispatch_bank(
        package,
        context={"snapshot": snap, "pack": MagicMock(), "player_name": "Rux"},
    )

    assert snap.quest_log == {}, "a below-threshold accept must not mint"
    assert "floor_boss_missing_person" in snap.pending_quest_offers, (
        "a degraded accept must leave the offer live for a clearer re-accept"
    )
    # The degrade produced a narrator hint directive.
    assert any(d.kind == "must_narrate" for d in result.directives)


# ---------------------------------------------------------------------------
# Unknown-offer accept does not phantom-mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_unknown_offer_does_not_mint() -> None:
    """ADR-146 §2 handler pseudocode: when the router names a quest_id with no
    matching pending offer, the handler emits a mismatch and returns — it does
    NOT fabricate a quest from nothing."""
    from sidequest.agents.subsystems import run_dispatch_bank

    snap = _snap_with_offer()  # only floor_boss_missing_person is pending
    package = _package_with(
        _quest_offer_dispatch(quest_id="a_quest_that_was_never_offered", decision="accept")
    )

    await run_dispatch_bank(
        package,
        context={"snapshot": snap, "pack": MagicMock(), "player_name": "Rux"},
    )

    assert "a_quest_that_was_never_offered" not in snap.quest_log
    # The real, pending offer is untouched.
    assert "floor_boss_missing_person" in snap.pending_quest_offers


# ---------------------------------------------------------------------------
# Engagement watcher — quest_offer witness (ADR-146 §4)
# ---------------------------------------------------------------------------


def test_quest_offer_witness_registered() -> None:
    """The dispatch engagement watcher must have a quest_offer witness in
    _WITNESSES — the structurally-sound replacement for the keyword
    detect_unminted_objective (ADR-146 §4)."""
    from sidequest.agents.dispatch_engagement_watcher import _WITNESSES

    assert "quest_offer" in _WITNESSES, (
        f"quest_offer has no engagement witness; _WITNESSES has {sorted(_WITNESSES)}. "
        "Story 117-3 must add it (dispatch_engagement_watcher.py:_WITNESSES)."
    )


def test_watcher_flags_accept_with_empty_quest_log() -> None:
    """Router dispatched 'quest_offer accept' but quest_log gained nothing →
    a real mismatch (dispatch_engagement.quest_offer.mismatch). Drives the pure
    detection function against a post-turn snapshot with no minted quest."""
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    # Accept was dispatched, but the snapshot's quest_log is empty (the engine
    # never minted — the exact router-claimed-but-engine-idle case).
    snap = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    package = _package_with(
        _quest_offer_dispatch(decision="accept", confidence=0.9)
    )

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert any(m.subsystem == "quest_offer" for m in mismatches), (
        "watcher must flag an accept dispatch that left quest_log empty "
        "(router-claimed-but-engine-idle), got: "
        f"{[(m.subsystem, m.evidence) for m in mismatches]}"
    )


def test_watcher_silent_when_accept_minted(otel_capture) -> None:
    """The inverse: when the accept actually minted the quest, the witness sees
    the QuestEntry and reports NO mismatch (honest engagement)."""
    from sidequest.game.quest_offer import mint_quest_offer

    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    snap = _snap_with_offer()
    mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.9)
    package = _package_with(_quest_offer_dispatch(decision="accept", confidence=0.9))

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert not any(m.subsystem == "quest_offer" for m in mismatches), (
        "watcher false-flagged a quest_offer that actually minted"
    )
