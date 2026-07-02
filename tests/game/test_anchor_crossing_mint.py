"""RED tests — Story 158-43 (ADR-146 amendment) — anchor-crossing acceptance.

ADR-146's Intent Router path covers GIVER-HOOK offers ("yeah, I'll take the
job"). Self-directed / giver-less seeds (beneath_sunden's ``the_unspent_hold``:
"acceptance is the descent, not a yes to anyone") never route through
``quest_offer`` — the player descends, the router classifies ``movement``, and
the offer sits in ``pending_quest_offers`` forever (sq-playtest 2026-06-27,
beneath_sunden, Harpo). The approved fix (Keith 2026-07-02) is a DETERMINISTIC
second mint trigger: when a seated PC genuinely TRANSITIONS INTO a pending
seed's ``anchor`` region, the engine mints via the existing idempotent
``mint_quest_offer`` — no LLM in the loop.

Contract under test (TEA-defined for Dev), from the approved design:

* ``sidequest.game.quest_offer.mint_on_anchor_crossing(snapshot, *, pc_name,
  from_region, to_region)`` — scans ``pending_quest_offers`` for a seed whose
  ``anchor == to_region``; mints via ``mint_quest_offer`` when ``from_region``
  is truthy. Trigger scope is ANY anchor-bearing seed (giver-less AND
  giver-hook — approved fork #1).
* WIRED at the ``pc_region`` genuine-change block of
  ``GameSnapshot._apply_world_patch_inner`` (beside
  ``notify_region_transition``) — the choke point ALL transition paths route
  through: movement dispatch, seam descent
  (``game/seams/deep_descent.py:54`` applies
  ``WorldStatePatch(pc_region=...)``), and the two procedural relocation
  paths (session.py's own ``pc_region`` block comment).
* The ``quest.seeded`` span fires with ``source="anchor_crossed"``,
  ``confidence=1.0`` — the GM panel must distinguish "engine watched the
  crossing" from the router's ``authored_seed`` verbal accept (OTEL
  Observability Principle; AC-2 is the lie detector).
* First-placement exclusion (approved fork #4): a falsy ``from_region``
  (spawn / turn-0 placement) NEVER mints — acceptance requires a genuine
  transition.
* Respect the decline (approved fork #3): a declined/consumed offer is NOT
  resurrected by a later crossing.
* First-writer-wins: router / narrator ``record_quest`` front-runs reconcile
  through the existing idempotent mint (no double-mint, no second span).
* The router's verbal-acceptance path is UNCHANGED (AC-6): a router accept
  still mints with ``source="authored_seed"`` and the router's confidence.

``mint_on_anchor_crossing`` does not exist yet — this module fails collection
(ImportError) until Dev (158-43 GREEN) lands the engine function and the
session wiring. That is the intended RED.

``otel_capture`` is the in-memory span exporter (tests/game/conftest.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sidequest.agents.subsystems.quest_offer import run_quest_offer_dispatch
from sidequest.game.quest_offer import (
    mint_on_anchor_crossing,
    mint_quest_offer,
    stash_quest_offers,
)
from sidequest.game.session import (
    QUEST_LOG_CARDINALITY_CAP,
    GameSnapshot,
    QuestEntry,
    WorldStatePatch,
)
from sidequest.game.turn import TurnManager
from sidequest.genre.models.narrative import (
    Opening,
    OpeningSetting,
    OpeningTone,
    OpeningTrigger,
    QuestSeed,
)
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

SPAN_NAME = "quest.seeded"
ANCHOR = "deep_shaft"
CAMP = "camp_saddle"
QUEST_ID = "the_ropes_promise"


# ---------------------------------------------------------------------------
# Builders — synthetic fixtures (the one real-content test loads beneath_sunden)
# ---------------------------------------------------------------------------


def _seed(
    *,
    quest_id: str = QUEST_ID,
    title: str = "The Rope's Promise",
    objective: str = "Go down the shaft after the rumor's hold and come back up.",
    stakes: str = "",
    anchor: str | None = ANCHOR,
    giver: str = "",
) -> QuestSeed:
    """Default builder is the FAILING shape: a self-directed, giver-less seed
    whose acceptance is the descent (mirrors ``the_unspent_hold``)."""
    return QuestSeed(
        quest_id=quest_id,
        title=title,
        objective=objective,
        stakes=stakes,
        anchor=anchor,
        giver=giver,
    )


def _opening_with_seed(seed: QuestSeed | None) -> Opening:
    return Opening(
        id="solo_synthetic_ropefoot",
        triggers=OpeningTrigger(),
        setting=OpeningSetting(
            location_label="the saddle camp", situation="at the fire, before the rope"
        ),
        tone=OpeningTone(register="grave", quest_seed=seed),
        establishing_narration="The fire is high; the rope is rigged and waiting.",
    )


def _snap(**kwargs) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=7),
        **kwargs,
    )


def _snap_with_offer(seed: QuestSeed | None = None) -> GameSnapshot:
    snap = _snap()
    stash_quest_offers(snap, _opening_with_seed(seed if seed is not None else _seed()))
    return snap


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _anchor_crossed_spans(otel_capture) -> list:
    return [
        s
        for s in _spans_named(otel_capture, SPAN_NAME)
        if dict(s.attributes or {}).get("source") == "anchor_crossed"
    ]


# ===========================================================================
# Unit — mint_on_anchor_crossing (the engine seam)
# ===========================================================================


def test_crossing_into_anchor_mints_quest_entry() -> None:
    """AC-1 (unit): a genuine crossing into the seed's anchor mints the
    QuestEntry deterministically — straight copy of the seed, offer consumed."""
    snap = _snap_with_offer()

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert QUEST_ID in snap.quest_log
    entry = snap.quest_log[QUEST_ID]
    assert entry.title == "The Rope's Promise"
    assert entry.objective.startswith("Go down the shaft")
    assert entry.status == "active"
    assert entry.anchor_id == ANCHOR
    # The anchor flows into quest_anchors exactly like every other mint path.
    assert ANCHOR in snap.quest_anchors
    # Taken bait leaves the pending pool (ADR-014 / ADR-146 consume-on-mint).
    assert QUEST_ID not in snap.pending_quest_offers


def test_crossing_mint_fires_anchor_crossed_span(otel_capture) -> None:
    """AC-2: the quest.seeded span fires with source="anchor_crossed" and
    confidence=1.0 — the GM panel must see "engine watched the crossing", not
    trust narration (OTEL lie-detector requirement)."""
    snap = _snap_with_offer()

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    spans = _spans_named(otel_capture, SPAN_NAME)
    assert len(spans) == 1, f"exactly one {SPAN_NAME!r} span must fire; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("quest_id") == QUEST_ID
    assert attrs.get("title") == "The Rope's Promise"
    assert attrs.get("source") == "anchor_crossed", (
        "the anchor path must be distinguishable from the router's "
        "authored_seed verbal accept on the GM panel"
    )
    assert attrs.get("confidence") == pytest.approx(1.0), (
        "a deterministic state-watch is certainty, not a classifier score"
    )
    assert attrs.get("anchor_count") == 1


def test_first_placement_does_not_mint(otel_capture) -> None:
    """AC-3 (unit): a falsy from_region (spawn / first placement) is NOT a
    transition — no mint, no span, and the offer stays live for a later
    genuine crossing. Both falsy shapes the session site produces ("" and
    None via ``prev or None``) are excluded."""
    for from_region in ("", None):
        snap = _snap_with_offer()

        mint_on_anchor_crossing(snap, pc_name="Rux", from_region=from_region, to_region=ANCHOR)

        assert snap.quest_log == {}, f"from_region={from_region!r} must not mint"
        assert QUEST_ID in snap.pending_quest_offers, (
            "a non-minting placement must leave the offer live, never consume it"
        )
    assert _spans_named(otel_capture, SPAN_NAME) == []


def test_crossing_unrelated_region_does_not_mint(otel_capture) -> None:
    """Crossing into a region that anchors no pending seed is just movement —
    no mint, no span, offer stays pending."""
    snap = _snap_with_offer()

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region="old_mill")

    assert snap.quest_log == {}
    assert QUEST_ID in snap.pending_quest_offers
    assert _spans_named(otel_capture, SPAN_NAME) == []


def test_anchorless_seed_never_mints_on_crossing(otel_capture) -> None:
    """A seed with no anchor has no crossing trigger — only the router's
    verbal path can accept it. It must stay pending through any movement."""
    snap = _snap_with_offer(_seed(anchor=None))

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert snap.quest_log == {}
    assert QUEST_ID in snap.pending_quest_offers
    assert _spans_named(otel_capture, SPAN_NAME) == []


def test_empty_to_region_never_matches_empty_anchor(otel_capture) -> None:
    """Defensive: the session gate guarantees a truthy to_region, but the unit
    seam must not treat anchor="" == to_region="" as a crossing — an
    empty-string match would mint on garbage input."""
    snap = _snap_with_offer(_seed(anchor=""))

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region="")

    assert snap.quest_log == {}
    assert _spans_named(otel_capture, SPAN_NAME) == []


def test_giver_hook_seed_with_anchor_also_mints(otel_capture) -> None:
    """Approved fork #1: trigger scope is ANY anchor-bearing seed — a
    giver-hook offer that carries an anchor also mints on crossing (walking
    into the job site accepts the job, same as saying yes)."""
    snap = _snap_with_offer(_seed(giver="the winch-keeper"))

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert QUEST_ID in snap.quest_log
    assert len(_anchor_crossed_spans(otel_capture)) == 1


def test_only_the_matching_anchor_offer_mints(otel_capture) -> None:
    """With several pending offers, only the seed anchored on to_region mints;
    the others stay live in the pending pool."""
    snap = _snap()
    snap.pending_quest_offers[QUEST_ID] = _seed()
    snap.pending_quest_offers["the_old_mill_job"] = _seed(
        quest_id="the_old_mill_job", title="The Old Mill Job", anchor="old_mill"
    )

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert QUEST_ID in snap.quest_log
    assert "the_old_mill_job" not in snap.quest_log
    assert "the_old_mill_job" in snap.pending_quest_offers, "non-matching offers must stay live"
    assert len(_spans_named(otel_capture, SPAN_NAME)) == 1


async def test_declined_offer_is_not_resurrected_by_crossing(otel_capture) -> None:
    """AC-4 (approved fork #3, respect the decline): a decline through the
    REAL dispatch handler consumes the offer; a later crossing into the
    anchor mints nothing — the dead job stays dead."""
    snap = _snap_with_offer()

    await run_quest_offer_dispatch(
        SubsystemDispatch(
            subsystem="quest_offer",
            params={"quest_id": QUEST_ID, "decision": "decline"},
            idempotency_key="k-decline-1",
            confidence=0.9,
            visibility=VisibilityTag(visible_to="all"),
        ),
        snapshot=snap,
    )
    assert QUEST_ID not in snap.pending_quest_offers, "decline consumes the offer"

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert snap.quest_log == {}, "anchor-crossing must not resurrect a declined offer"
    assert _spans_named(otel_capture, SPAN_NAME) == []


def test_router_minted_first_anchor_crossing_no_double_mint(otel_capture) -> None:
    """AC-5: the router's verbal accept minted first; the later crossing
    no-ops (first writer wins) — one entry, one span, and it is the router's."""
    snap = _snap_with_offer()
    minted = mint_quest_offer(snap, QUEST_ID, confidence=0.9)
    assert minted is not None, "precondition: the router path minted"

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert len(snap.quest_log) == 1, "the crossing must not double-mint"
    assert _anchor_crossed_spans(otel_capture) == [], (
        "a no-op crossing must not fire a second quest.seeded span"
    )
    assert len(_spans_named(otel_capture, SPAN_NAME)) == 1


def test_narrator_front_ran_crossing_consumes_offer_without_clobber(
    otel_capture,
) -> None:
    """AC-5 edge: the narrator front-ran via record_quest while the offer was
    still pending. The crossing must not clobber the narrator's entry and must
    not span — but it DOES consume the offer (mint_quest_offer's leak-prevention
    doctrine: a taken job never re-surfaces in the router's offer list)."""
    snap = _snap_with_offer()
    snap.quest_log[QUEST_ID] = QuestEntry(
        title="Narrator's version", objective="narrator wrote this", status="active"
    )

    mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert snap.quest_log[QUEST_ID].title == "Narrator's version", (
        "the crossing must not overwrite a narrator-minted quest"
    )
    assert QUEST_ID not in snap.pending_quest_offers, (
        "the already-taken job must be consumed, not leaked back to the router"
    )
    assert _spans_named(otel_capture, SPAN_NAME) == []


def test_cap_overflow_fails_loud_and_offer_survives() -> None:
    """No Silent Fallbacks: a crossing that would push quest_log past the
    cardinality cap fails LOUD (mint_quest_offer's existing contract), and the
    offer is never silently consumed by the failed mint."""
    snap = _snap_with_offer()
    for i in range(QUEST_LOG_CARDINALITY_CAP):
        snap.quest_log[f"q{i}"] = QuestEntry(title=f"Q{i}", status="active")

    with pytest.raises(Exception):  # noqa: B017 — loud failure; exact type is Dev's call
        mint_on_anchor_crossing(snap, pc_name="Rux", from_region=CAMP, to_region=ANCHOR)

    assert QUEST_ID not in snap.quest_log
    assert QUEST_ID in snap.pending_quest_offers, "a failed mint must never silently drop the offer"


# ===========================================================================
# Wiring — the apply_world_patch(pc_region=...) choke point (session.py)
# ===========================================================================
# ALL genuine region transitions route through this block: movement dispatch,
# seam descent (deep_descent.py:54 applies WorldStatePatch(pc_region=...)),
# and both procedural relocation paths. Driving the choke point IS the
# integration test for every producer (fixture-driven behavior test per
# CLAUDE.md "No Source-Text Wiring Tests").


def test_world_patch_genuine_transition_into_anchor_mints(otel_capture) -> None:
    """AC-1 (wired): a genuine per-PC region change applied through the REAL
    production patch path mints the offer and fires the anchor_crossed span —
    no LLM, no narrator tool call, anywhere in the loop."""
    snap = _snap_with_offer()
    snap.pc_regions["Rux"] = CAMP

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": ANCHOR}))

    assert QUEST_ID in snap.quest_log, (
        "the pc_region genuine-change block must engage the anchor-crossing mint"
    )
    entry = snap.quest_log[QUEST_ID]
    assert entry.status == "active"
    assert entry.anchor_id == ANCHOR
    spans = _anchor_crossed_spans(otel_capture)
    assert len(spans) == 1
    assert dict(spans[0].attributes or {}).get("confidence") == pytest.approx(1.0)


def test_world_patch_first_placement_does_not_mint(otel_capture) -> None:
    """AC-3 (wired): a PC with NO prior pc_regions entry patched into the
    anchor is first placement, not a transition — no mint, offer stays live."""
    snap = _snap_with_offer()

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": ANCHOR}))

    assert snap.quest_log == {}
    assert QUEST_ID in snap.pending_quest_offers
    assert _anchor_crossed_spans(otel_capture) == []


def test_world_patch_noop_repatch_does_not_mint(otel_capture) -> None:
    """Standing in the anchor region is not a crossing: re-patching a PC to
    the region they are already in must not mint (the genuine-change gate) —
    otherwise every turn spent at the anchor re-attempts the mint."""
    snap = _snap_with_offer()
    snap.pc_regions["Rux"] = ANCHOR

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": ANCHOR}))

    assert snap.quest_log == {}
    assert QUEST_ID in snap.pending_quest_offers
    assert _anchor_crossed_spans(otel_capture) == []


def test_world_patch_spawn_current_region_does_not_mint(otel_capture) -> None:
    """AC-3 (spawn path): the current_region party-anchor branch (spawn /
    turn-0 placement) seeds pc_regions but is NOT an acceptance — spawning
    INTO the anchor region must not mint (approved fork #4)."""
    snap = _snap_with_offer()
    snap.player_seats["p1"] = "Rux"

    snap.apply_world_patch(WorldStatePatch(current_region=ANCHOR))

    assert snap.quest_log == {}
    assert QUEST_ID in snap.pending_quest_offers
    assert _anchor_crossed_spans(otel_capture) == []


def test_world_patch_recrossing_does_not_double_mint(otel_capture) -> None:
    """AC-5 (wired): descend (mint), climb back out, descend again — exactly
    one quest, exactly one anchor_crossed span."""
    snap = _snap_with_offer()
    snap.pc_regions["Rux"] = CAMP

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": ANCHOR}))
    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": CAMP}))
    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": ANCHOR}))

    assert len(snap.quest_log) == 1
    assert len(_anchor_crossed_spans(otel_capture)) == 1


def test_world_patch_batch_two_pcs_crossing_mints_exactly_once(otel_capture) -> None:
    """A batch patch moving two PCs into the anchor in the same apply mints
    exactly once — the first crossing consumes the offer; the second finds
    nothing pending (the split-party primitive must not double-mint)."""
    snap = _snap_with_offer()
    snap.pc_regions["Rux"] = CAMP
    snap.pc_regions["Brama"] = CAMP

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": ANCHOR, "Brama": ANCHOR}))

    assert len(snap.quest_log) == 1
    assert len(_anchor_crossed_spans(otel_capture)) == 1


# ===========================================================================
# Real content — the exact sq-playtest 2026-06-27 failing case
# ===========================================================================


def _beneath_sunden_openings() -> list[dict]:
    root = Path(__file__).resolve().parents[3]
    path = (
        root
        / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
        / "openings.yaml"
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["openings"]


def test_beneath_sunden_unspent_hold_descent_mints(otel_capture) -> None:
    """THE playtest bug (sq-playtest 2026-06-27, Harpo): the real
    ``the_unspent_hold`` seed — giver-less, anchored on ``the_dropmouth`` —
    stashed from the REAL beneath_sunden opening, then the delver descends.
    The descent IS the acceptance: the offer must mint into quest_log."""
    entries = [
        e
        for e in _beneath_sunden_openings()
        if ((e.get("tone") or {}).get("quest_seed") or {}).get("quest_id") == "the_unspent_hold"
    ]
    assert entries, "beneath_sunden must still author the_unspent_hold (content moved?)"

    opening = Opening.model_validate(entries[0])
    seed = opening.tone.quest_seed
    assert seed is not None
    assert seed.giver == "", (
        "the_unspent_hold is the giver-less self-directed shape this story exists for"
    )
    assert seed.anchor == "the_dropmouth"

    snap = _snap()
    stash_quest_offers(snap, opening)
    snap.pc_regions["Harpo"] = "ropefoot_camp"

    snap.apply_world_patch(WorldStatePatch(pc_region={"Harpo": "the_dropmouth"}))

    assert "the_unspent_hold" in snap.quest_log, (
        "the descent must mint the offer — the Quests tab stays blank without this"
    )
    assert snap.quest_log["the_unspent_hold"].title == "The Unspent Hold"
    assert "the_unspent_hold" not in snap.pending_quest_offers
    spans = _anchor_crossed_spans(otel_capture)
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("quest_id") == "the_unspent_hold"
    assert attrs.get("confidence") == pytest.approx(1.0)


# ===========================================================================
# AC-6 regression — the router's verbal-acceptance path is UNCHANGED
# ===========================================================================


def test_router_verbal_accept_path_unchanged(otel_capture) -> None:
    """AC-6: the ADR-146 giver-hook verbal accept still mints exactly as
    before — source stays "authored_seed" and the span carries the ROUTER's
    confidence, not 1.0. Pins that Dev's source-plumbing does not change the
    existing path's span vocabulary."""
    snap = _snap_with_offer(_seed(giver="the winch-keeper"))

    entry = mint_quest_offer(snap, QUEST_ID, confidence=0.9)

    assert entry is not None
    assert QUEST_ID in snap.quest_log
    spans = _spans_named(otel_capture, SPAN_NAME)
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("source") == "authored_seed", (
        "the router path's span source must not change under the amendment"
    )
    assert attrs.get("confidence") == pytest.approx(0.9)
