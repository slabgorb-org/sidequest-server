"""RED tests — Story 117-3 (ADR-146) — deterministic quest-seed minting.

The engine seam beneath the ``quest_offer`` router subsystem. When an authored
opening carries a ``tone.quest_seed``, chargen-complete stashes it on
``snapshot.pending_quest_offers`` (resume-safe). When the router classifies the
player's turn as *accepting* a named offer at high confidence, the engine mints
a ``QuestEntry`` into ``quest_log`` keyed on the seed's ``quest_id`` — no
narrator tool call required — and fires a ``quest.seeded`` OTEL span.

Contract under test (TEA-defined for Dev), from ADR-146 §1/§2/§4:

* ``sidequest.game.quest_offer.stash_quest_offers(snapshot, opening)`` reads
  ``opening.tone.quest_seed`` and populates ``snapshot.pending_quest_offers``
  (a ``dict[str, QuestSeed]`` keyed on ``quest_id``). Openings with no seed
  leave it empty (authoring a seed is optional — ADR-146 "Neutral").
* ``snapshot.pending_quest_offers`` is persisted snapshot state and survives a
  ``model_dump_json`` → ``model_validate_json`` round-trip (resume-safe;
  ADR-146 "Negative/costs": new snapshot state that MUST survive resume).
* ``sidequest.game.quest_offer.mint_quest_offer(snapshot, quest_id,
  confidence=...)`` mints a ``QuestEntry`` from the stashed seed into
  ``quest_log[quest_id]``, copies ``stakes``→``active_stakes`` (fill, not
  clobber), appends the anchor (dedup), CONSUMES the offer, and fires
  ``quest.seeded``. Returns truthy/the minted entry on a real mint.
* Idempotent: minting the same ``quest_id`` twice is a no-op (first writer
  wins; ADR-146 §3 "Authored seed is fill, not clobber").
* The cardinality cap (32, shared with ``record_quest``) is honoured — a mint
  past the cap fails loud, never silently drops the offer (ADR-146 §3).

These import names that do not exist yet (``QuestSeed``,
``sidequest.game.quest_offer``, ``snapshot.pending_quest_offers``) — collection
fails (ImportError) / attribute access fails until Dev (117-3 GREEN) lands the
model + the snapshot field + the engine module. That is the intended RED.

``otel_capture`` is the in-memory span exporter (tests/game/conftest.py).
"""

from __future__ import annotations

import pytest
from sidequest.game.quest_offer import mint_quest_offer, stash_quest_offers

from sidequest.game.session import GameSnapshot
from sidequest.genre.models.narrative import (
    Opening,
    OpeningSetting,
    OpeningTone,
    OpeningTrigger,
    QuestSeed,
)

SPAN_NAME = "quest.seeded"


# ---------------------------------------------------------------------------
# Builders — synthetic fixtures only (no perseus_cloud / real content)
# ---------------------------------------------------------------------------


def _seed(
    *,
    quest_id: str = "floor_boss_missing_person",
    title: str = "The Floor-Boss's Missing Person",
    objective: str = "Find out who the floor-boss has lost in the under-levels.",
    stakes: str = "a corporate favour owed, or a corporate enemy made",
    anchor: str | None = "under_levels_beacon",
    giver: str = "the Conglomerate floor-boss",
) -> QuestSeed:
    return QuestSeed(
        quest_id=quest_id,
        title=title,
        objective=objective,
        stakes=stakes,
        anchor=anchor,
        giver=giver,
    )


def _opening_with_seed(seed: QuestSeed | None) -> Opening:
    """A synthetic, location-anchored Opening carrying (or not) a quest_seed."""
    tone = OpeningTone(
        register="noir",
        complication="a Conglomerate floor-boss two tiers up has been watching you",
        quest_seed=seed,
    )
    return Opening(
        id="solo_synthetic_arrival",
        triggers=OpeningTrigger(),
        setting=OpeningSetting(location_label="New Kowloon docks", situation="just arrived"),
        tone=tone,
        establishing_narration="The lift doors part on a wall of neon and rain.",
    )


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# (b) pending_quest_offers populates from the opening's quest_seed
# ---------------------------------------------------------------------------


def test_stash_populates_pending_offer_from_seed() -> None:
    snap = GameSnapshot()
    assert snap.pending_quest_offers == {}, "snapshot must start with no pending offers"

    stash_quest_offers(snap, _opening_with_seed(_seed()))

    assert "floor_boss_missing_person" in snap.pending_quest_offers
    offer = snap.pending_quest_offers["floor_boss_missing_person"]
    # The offer must surface quest_id/title/giver (the router's acceptance
    # context — ADR-146 §2 "pending_quest_offers: [{quest_id, title, giver}]").
    assert offer.quest_id == "floor_boss_missing_person"
    assert offer.title == "The Floor-Boss's Missing Person"
    assert offer.giver == "the Conglomerate floor-boss"


def test_stash_is_noop_when_opening_has_no_seed() -> None:
    """Authoring a seed is optional (ADR-146 'Neutral'): an opening with no
    quest_seed leaves pending_quest_offers empty — no phantom offer."""
    snap = GameSnapshot()
    stash_quest_offers(snap, _opening_with_seed(None))
    assert snap.pending_quest_offers == {}


def test_stash_does_not_mint_yet() -> None:
    """A stashed offer is bait, not a quest (ADR-146 Alternative B rejected):
    stashing alone must NOT touch quest_log — minting waits for acceptance."""
    snap = GameSnapshot()
    stash_quest_offers(snap, _opening_with_seed(_seed()))
    assert snap.quest_log == {}, "stashing an offer must not mint a quest"


# ---------------------------------------------------------------------------
# (b cont.) resume-safe — survives a snapshot round-trip
# ---------------------------------------------------------------------------


def test_pending_offers_survive_snapshot_round_trip() -> None:
    """pending_quest_offers is persisted snapshot state (ADR-146 'Negative'):
    it MUST survive resume, not live only on the ephemeral _SessionData."""
    snap = GameSnapshot()
    stash_quest_offers(snap, _opening_with_seed(_seed()))

    restored = GameSnapshot.model_validate_json(snap.model_dump_json())

    assert "floor_boss_missing_person" in restored.pending_quest_offers
    offer = restored.pending_quest_offers["floor_boss_missing_person"]
    assert offer.title == "The Floor-Boss's Missing Person"
    assert offer.giver == "the Conglomerate floor-boss"


# ---------------------------------------------------------------------------
# (c) accept at high confidence mints into quest_log
# ---------------------------------------------------------------------------


def test_accept_mints_quest_entry(otel_capture) -> None:
    snap = GameSnapshot()
    stash_quest_offers(snap, _opening_with_seed(_seed()))

    mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.9)

    assert "floor_boss_missing_person" in snap.quest_log
    entry = snap.quest_log["floor_boss_missing_person"]
    assert entry.title == "The Floor-Boss's Missing Person"
    assert entry.objective.startswith("Find out who")
    assert entry.status == "active"
    assert entry.anchor_id == "under_levels_beacon"
    # The anchor flows into quest_anchors (dedup), exactly like record_quest.
    assert "under_levels_beacon" in snap.quest_anchors
    # stakes seeds active_stakes when empty (fill-don't-clobber).
    assert snap.active_stakes == "a corporate favour owed, or a corporate enemy made"
    # The offer is consumed on accept.
    assert "floor_boss_missing_person" not in snap.pending_quest_offers


def test_accept_does_not_clobber_existing_stakes() -> None:
    """Fill, don't clobber (ADR-146 §3, consistent with seed_quest_spine): if
    active_stakes is already authored, the offer's stakes do NOT overwrite it."""
    snap = GameSnapshot()
    snap.active_stakes = "the world-authored spine wins"
    stash_quest_offers(snap, _opening_with_seed(_seed()))

    mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.9)

    assert snap.active_stakes == "the world-authored spine wins"
    # But the quest itself still mints.
    assert "floor_boss_missing_person" in snap.quest_log


# ---------------------------------------------------------------------------
# (f) the quest.seeded span fires with the ADR-146 §4 attrs
# ---------------------------------------------------------------------------


def test_accept_emits_quest_seeded_span(otel_capture) -> None:
    snap = GameSnapshot()
    stash_quest_offers(snap, _opening_with_seed(_seed()))

    mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.9)

    spans = _spans_named(otel_capture, SPAN_NAME)
    assert len(spans) == 1, (
        f"exactly one {SPAN_NAME!r} span must fire on an authored-hook mint; "
        f"got {len(spans)}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("quest_id") == "floor_boss_missing_person"
    assert attrs.get("title") == "The Floor-Boss's Missing Person"
    # source distinguishes the authored mint from narrator / drive mints.
    assert attrs.get("source") == "authored_seed"
    assert attrs.get("anchor_count") == 1
    # the router score that crossed the gate is recorded (a marginal mint is
    # visible on the GM panel).
    assert attrs.get("confidence") == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# (d) idempotency — minting the same quest_id twice is a no-op
# ---------------------------------------------------------------------------


def test_mint_is_idempotent(otel_capture) -> None:
    snap = GameSnapshot()
    stash_quest_offers(snap, _opening_with_seed(_seed()))

    mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.9)
    first_entry = snap.quest_log["floor_boss_missing_person"]
    # Re-stash so the offer is present again, then accept a second time.
    stash_quest_offers(snap, _opening_with_seed(_seed()))
    mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.95)

    assert len(snap.quest_log) == 1, "second accept must not double-mint"
    assert snap.quest_log["floor_boss_missing_person"] is first_entry or (
        snap.quest_log["floor_boss_missing_person"].title == first_entry.title
    )
    # Only the first mint fired a span; the idempotent no-op does not re-fire.
    assert len(_spans_named(otel_capture, SPAN_NAME)) == 1, (
        "idempotent re-accept must not emit a second quest.seeded span"
    )


def test_mint_idempotent_when_narrator_front_ran_record_quest() -> None:
    """ADR-146 §3: if the narrator already minted this quest_id via record_quest,
    the authored-seed mint no-ops (first writer wins) — no double-mint, no
    overwrite of the narrator's entry."""
    from sidequest.game.session import QuestEntry

    snap = GameSnapshot()
    snap.quest_log["floor_boss_missing_person"] = QuestEntry(
        title="Narrator's version", objective="narrator wrote this", status="active"
    )
    stash_quest_offers(snap, _opening_with_seed(_seed()))

    mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.9)

    assert snap.quest_log["floor_boss_missing_person"].title == "Narrator's version", (
        "authored-seed mint must not clobber a narrator-minted quest of the same id"
    )


# ---------------------------------------------------------------------------
# Cardinality cap honoured (ADR-146 §3 — fail loud past 32, never silent-drop)
# ---------------------------------------------------------------------------


def test_mint_honours_cardinality_cap() -> None:
    from sidequest.game.session import QuestEntry

    snap = GameSnapshot()
    # Fill quest_log to the cap with unrelated quests.
    for i in range(32):
        snap.quest_log[f"q{i}"] = QuestEntry(title=f"Q{i}", status="active")
    stash_quest_offers(snap, _opening_with_seed(_seed()))

    with pytest.raises(Exception):  # noqa: B017 — loud failure, exact type is Dev's call
        mint_quest_offer(snap, "floor_boss_missing_person", confidence=0.9)

    # The offer was NOT silently consumed by a failed mint.
    assert "floor_boss_missing_person" not in snap.quest_log
