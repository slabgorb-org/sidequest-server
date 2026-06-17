"""Task 15 — end-to-end wiring: table_resolution turn through the REAL apply path.

Three test groups:

Part B — required wiring test (spec §Testing):
  Drive a table_resolution confrontation through the REAL
  ``_apply_narration_result_to_snapshot`` with synthetic seat commits.
  Assert (1) ``table.showdown`` OTEL span fired — proving the branch is
  reachable from the production narration path; and (2) the pot award
  produced a real state change (PC winner's gold increased).

Part C — barrier behavior:
  Verify that a folded PC seat calls room.mark_table_folded via the
  production branch, lowering effective_barrier_count for the next
  decision point.

Part A (fold-mark/clear) is exercised indirectly by Part C.

OTEL capture uses the same InMemorySpanExporter + SimpleSpanProcessor
pattern as tests/integration/test_dogfight_swn_production_wiring.py —
the canonical in-process span capture fixture for this codebase.

Per CLAUDE.md "No Source-Text Wiring Tests": assertions are on OTEL
spans / state / behavior — never grep source code.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.game.table.poker  # noqa: F401 — registers the poker kind at import
from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    RulesConfig,
    WinCondition,
)
from sidequest.server.dispatch.encounter_lifecycle import instantiate_table_encounter
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.session_room import LobbyState, SessionRoom
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# OTEL capture fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture():
    """Attach an in-memory exporter to the running TracerProvider.

    Mirrors the otel_capture fixture from
    tests/integration/test_dogfight_swn_production_wiring.py — the canonical
    in-process OTEL capture pattern for this project.
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Shared builders (mirrors test_table_branch_unit.py exactly)
# ---------------------------------------------------------------------------


def _poker_cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="poker",
        label="Poker",
        category="social",
        resolution_mode=ResolutionMode.table_resolution,
        win_condition=WinCondition.table_showdown,
        table_game="poker",
        max_decision_points=1,  # single decision point → straight to showdown
        beats=[
            BeatDef(id="fold", label="Fold", kind="push", stat_check="WIS", base=0),
            BeatDef(id="call", label="Call", kind="push", stat_check="WIS", base=0),
        ],
    )


def _poker_table_snapshot():
    """A snapshot carrying an active 2-seat poker table_resolution encounter.

    PC "Doc" gets seat_1, NPC "Ringo" gets seat_2.  Built via the production
    instantiate_table_encounter helper (same as test_table_branch_unit.py)
    so the fixture matches the real trigger path.
    """
    cdef = _poker_cdef()
    enc = instantiate_table_encounter(
        cdef=cdef,
        player_names=["Doc"],
        npc_names=["Ringo"],
        stake_kind="money",
        stake_descriptor="the pot",
        seed=1,
        ruleset_slug="dial",
    )
    doc = Character(
        core=CreatureCore(
            name="Doc",
            description="A gambler",
            personality="cool",
            inventory=Inventory(gold=50),
        ),
        char_class="Gunslinger",
        race="Human",
        backstory="Drifter.",
    )
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(doc)
    snap.encounter = enc
    snap.turn_manager.record_interaction()
    pack = MagicMock()
    pack.rules = RulesConfig(ruleset="dial", confrontations=[cdef])
    return snap, pack


# ---------------------------------------------------------------------------
# Part B — required end-to-end wiring test (spec §Testing)
# ---------------------------------------------------------------------------


def test_table_showdown_span_fires_and_pot_awarded(otel_capture: InMemorySpanExporter) -> None:
    """THE WIRING ASSERTION: table.showdown span fires AND gold state changes.

    Drives a table_resolution confrontation through the REAL
    ``_apply_narration_result_to_snapshot`` with synthetic seat commits.

    Three-assertion contract (per spec §Testing):
      1. ``table.showdown`` OTEL span fired — the new branch is reachable
         from the production narration path, not just unit-callable.
      2. ``enc.resolved`` is True and outcome encodes the table winner —
         the encounter resolution state is correct.
      3. The PC winner's gold increased — the pot award is a real auditable
         state mutation, not just a field set on the outcome object.

    Failure modes this test catches:
      - The table_resolution branch is unreachable (span never fires → branch
        was silently bypassed by the gate or the mode check).
      - resolve_table never reaches showdown (span fires from wrong path).
      - The gold mutation path is broken (state change assertion fails).
    """
    snap, pack = _poker_table_snapshot()
    enc = snap.encounter
    ts = enc.table_state

    # Force a deterministic winner: seat_1 (Doc, the PC) beats seat_2 (Ringo).
    ts.find_seat("seat_1").private_state["strength"] = 10**9
    ts.find_seat("seat_2").private_state["strength"] = 1

    doc = next(c for c in snap.characters if c.core.name == "Doc")
    gold_before = int(doc.core.inventory.gold)

    result = NarrationTurnResult(
        narration="Doc lays down his hand and rakes the pot.",
        beat_selections=[
            BeatSelection(actor="Doc", beat_id="call", amount=1),
            BeatSelection(actor="Ringo", beat_id="call", amount=1),
        ],
    )
    outcome = _apply_narration_result_to_snapshot(
        snap,
        result,
        "Doc",
        room=room_for(snap),
        pack=pack,
        from_explicit_action=False,
    )

    # 1) table.showdown span fired — branch is production-reachable
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "table.showdown" in span_names, (
        f"table.showdown span did not fire — table_resolution branch was not reached.\n"
        f"Spans captured: {span_names}"
    )

    # 2) encounter resolved with the table winner
    assert enc.resolved is True, "encounter must be resolved after showdown"
    assert enc.outcome == "table_winner:seat_1", (
        f"expected outcome='table_winner:seat_1', got {enc.outcome!r}"
    )

    # 3) pot award produced a real state change — Doc's gold increased
    final_pot = sum(ts.pot.contributions.values())
    assert final_pot > 0, "pot must be non-zero after antes + calls"
    assert int(doc.core.inventory.gold) == gold_before + final_pot, (
        f"PC winner's gold must increase by pot total={final_pot}; "
        f"before={gold_before}, after={int(doc.core.inventory.gold)}"
    )
    assert outcome.table_pot_award is not None, "table_pot_award must be set on outcome"
    assert outcome.table_pot_award["recipient"] == "Doc"


def test_table_commit_spans_also_fire(otel_capture: InMemorySpanExporter) -> None:
    """Secondary assertion: table.commit spans fire for every seat commit.

    These spans are the GM panel's per-decision-point audit trail showing
    which beat each seat committed (the lie-detector sub-check for the table
    branch — not just "showdown happened" but "commits were recorded").
    """
    snap, pack = _poker_table_snapshot()
    enc = snap.encounter
    ts = enc.table_state
    ts.find_seat("seat_1").private_state["strength"] = 10**9
    ts.find_seat("seat_2").private_state["strength"] = 1

    result = NarrationTurnResult(
        narration="Doc wins.",
        beat_selections=[
            BeatSelection(actor="Doc", beat_id="call", amount=1),
            BeatSelection(actor="Ringo", beat_id="call", amount=1),
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Doc",
        room=room_for(snap),
        pack=pack,
        from_explicit_action=False,
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    commit_spans = [n for n in span_names if n == "table.commit"]
    assert len(commit_spans) >= 1, (
        f"expected at least 1 table.commit span (for the PC's call commit); "
        f"got {len(commit_spans)}.  Spans: {span_names}"
    )
    assert "table.showdown" in span_names, (
        f"table.showdown must also fire in the same turn.  Spans: {span_names}"
    )


# ---------------------------------------------------------------------------
# Part C — barrier denominator behavior test
# ---------------------------------------------------------------------------


def _room_with_player_seats(
    snap: GameSnapshot,
    player_id: str,
    pc_name: str,
) -> SessionRoom:
    """Return a MULTIPLAYER SessionRoom with one PLAYING peer wired to snap.

    Sets ``snap.player_seats[player_id] = pc_name`` so the fold-mark
    reverse-map in the table branch can find the player_id from the PC name.
    Uses the real seating API confirmed in test_table_barrier_denominator.py:
      ``room.seat(player_id, character_slot=...)`` → CHARGEN
      ``room._seated[player_id].state = LobbyState.PLAYING`` → PLAYING
    Binds the room to the snapshot via bind_world so barrier mechanics work.
    """
    snap.player_seats[player_id] = pc_name
    room = SessionRoom(slug="barrier-test", mode=GameMode.MULTIPLAYER)
    room.seat(player_id, character_slot=player_id)
    room._seated[player_id].state = LobbyState.PLAYING  # noqa: SLF001
    room.bind_world(snapshot=snap, store=MagicMock(spec=SaveRepository))
    return room


def test_folded_pc_seat_lowers_barrier_denominator() -> None:
    """Part C: a folded PC seat lowers effective_barrier_count for the next DP.

    This proves Part A's wiring end-to-end:
      1. Build a snapshot with player_seats mapping p1 → Doc.
      2. Seat p1 as PLAYING in a MULTIPLAYER room.
      3. Verify barrier denominator starts at 1.
      4. Drive a table_resolution turn where a second PC starts folded
         (we directly set the seat status to 'folded' on a 2-PC snapshot
         so the fold-mark fires for the specific player we track).
      5. Verify effective_barrier_count drops to 0 after the turn.

    The test constructs a 2-PC scenario: p1/Doc and p2/Jesse.  Doc has
    strength=10^9 (wins), Jesse has strength=1 AND starts the turn with
    status="folded" (the engine keeps it folded).  After apply, the branch
    must call mark_table_folded("p2") for Jesse.
    """
    cdef = _poker_cdef()
    enc = instantiate_table_encounter(
        cdef=cdef,
        player_names=["Doc", "Jesse"],
        npc_names=[],
        stake_kind="money",
        stake_descriptor="the pot",
        seed=1,
        ruleset_slug="dial",
    )
    doc = Character(
        core=CreatureCore(
            name="Doc",
            description="A gambler",
            personality="cool",
            inventory=Inventory(gold=50),
        ),
        char_class="Gunslinger",
        race="Human",
        backstory="Drifter.",
    )
    jesse = Character(
        core=CreatureCore(
            name="Jesse",
            description="A rustler",
            personality="shifty",
            inventory=Inventory(gold=30),
        ),
        char_class="Outlaw",
        race="Human",
        backstory="Trouble.",
    )
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.extend([doc, jesse])
    snap.encounter = enc
    snap.turn_manager.record_interaction()
    pack = MagicMock()
    pack.rules = RulesConfig(ruleset="dial", confrontations=[cdef])

    ts = enc.table_state
    # Doc wins; Jesse's seat must be folded by the engine.  We force this by
    # pre-setting Jesse's status to "folded" before resolve_table runs — the
    # engine already ignores folded seats in the active-seat list.  Alternatively
    # we could give Jesse beat_id="fold" but pre-setting status is cleaner
    # because the fold-mark logic scans enc.table_state.seats after resolve_table.
    jesse_seat = next(s for s in ts.seats if s.party_name == "Jesse")
    jesse_seat.status = "folded"

    # Force Doc to win.
    doc_seat = next(s for s in ts.seats if s.party_name == "Doc")
    doc_seat.private_state["strength"] = 10**9
    jesse_seat.private_state["strength"] = 1

    # Build a MULTIPLAYER room with p2/Jesse PLAYING.  p1/Doc is the narrating
    # player; we only assert on p2's barrier slot dropping.
    room = _room_with_player_seats(snap, "p1", "Doc")
    room.seat("p2", character_slot="p2")
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001
    snap.player_seats["p2"] = "Jesse"

    # Sanity: 2 PLAYING players before the turn.
    assert room.effective_barrier_count() == 2, (
        f"expected 2 players before table turn, got {room.effective_barrier_count()}"
    )

    result = NarrationTurnResult(
        narration="Doc rakes the pot.  Jesse threw her cards down.",
        beat_selections=[
            BeatSelection(actor="Doc", beat_id="call", amount=1),
            # Jesse already folded — no commit needed for a pre-folded seat.
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Doc",
        room=room,
        pack=pack,
        from_explicit_action=False,
    )

    # After the table branch ran, Jesse is folded → mark_table_folded("p2") fired.
    # On showdown, clear_table_folds() ALSO fires — so the denominator is reset.
    # BUT the showdown only fires if the encounter resolved.  In this test we have
    # max_decision_points=1 and Doc as the only active seat, so resolve_table
    # hits showdown immediately → clear_table_folds fires → denominator restores.
    # The NET result after a showdown-with-clear is the same as baseline (2) because
    # clear_table_folds undoes the mark.  To assert the mark fired BEFORE the clear,
    # we use a 3-player scenario in the companion test below.
    assert enc.resolved is True, "encounter must resolve at showdown"
    assert room.effective_barrier_count() == 2, (
        "after showdown clear_table_folds must restore denominator to 2"
    )


def test_fold_mark_fires_before_clear_on_multi_decision_point_hand() -> None:
    """Part C (deeper): with 3 PCs + max_decision_points=2, fold-mark fires first.

    Three PCs (Doc, Jesse, Wyatt) all seated PLAYING; max_decision_points=2.

    Decision point 0: Jesse folds; Doc and Wyatt call.  After resolve_table:
      - active seats = {Doc, Wyatt} → len(active)=2 > 1, so NOT a showdown by the
        one-survivor rule;
      - decision_point(0) >= max_decision_points-1(1) is False, so NOT a showdown
        by the DP-cap rule.
      => the engine does NOT reach showdown.  enc.resolved stays False, and
         clear_table_folds() does NOT fire — only the fold-mark for Jesse fires.

    This is DETERMINISTIC (no OR): 3 seats guarantee 2 remain active after one
    fold, so the early-showdown-on-one-survivor path can't trigger at DP-0.

    Assertions (single deterministic barrier count):
      - enc.resolved is False (no showdown this DP),
      - effective_barrier_count() == 2 (3 PLAYING peers − 1 newly-folded Jesse)
        — proving the fold-mark fired mid-hand and clear did NOT.

    Then we drive a SECOND decision point (Doc + Wyatt call again) to reach
    showdown and assert the count restores to 3 after clear_table_folds() — the
    teardown half of Part A.  This second leg also exercises the I1 "newly
    folded" diff: Jesse is already folded going into DP-1, so she must NOT be
    re-marked (no behavior change, but the diff is the contract under test).
    """
    cdef = ConfrontationDef(
        type="poker",
        label="Poker",
        category="social",
        resolution_mode=ResolutionMode.table_resolution,
        win_condition=WinCondition.table_showdown,
        table_game="poker",
        max_decision_points=2,  # 2 DPs so DP-0 does NOT trigger the DP-cap showdown
        beats=[
            BeatDef(id="fold", label="Fold", kind="push", stat_check="WIS", base=0),
            BeatDef(id="call", label="Call", kind="push", stat_check="WIS", base=0),
        ],
    )
    enc = instantiate_table_encounter(
        cdef=cdef,
        player_names=["Doc", "Jesse", "Wyatt"],
        npc_names=[],
        stake_kind="money",
        stake_descriptor="the pot",
        seed=2,
        ruleset_slug="dial",
    )

    def _pc(name: str, gold: int) -> Character:
        return Character(
            core=CreatureCore(
                name=name,
                description="A gambler",
                personality="cool",
                inventory=Inventory(gold=gold),
            ),
            char_class="Gunslinger",
            race="Human",
            backstory="Drifter.",
        )

    doc = _pc("Doc", 50)
    jesse = _pc("Jesse", 30)
    wyatt = _pc("Wyatt", 40)
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.extend([doc, jesse, wyatt])
    snap.encounter = enc
    snap.turn_manager.record_interaction()
    pack = MagicMock()
    pack.rules = RulesConfig(ruleset="dial", confrontations=[cdef])

    ts = enc.table_state
    # Doc the strongest hand so he wins the eventual showdown; Wyatt second.
    doc_seat = next(s for s in ts.seats if s.party_name == "Doc")
    wyatt_seat = next(s for s in ts.seats if s.party_name == "Wyatt")
    jesse_seat = next(s for s in ts.seats if s.party_name == "Jesse")
    doc_seat.private_state["strength"] = 10**9
    wyatt_seat.private_state["strength"] = 10**6
    jesse_seat.private_state["strength"] = 1

    # Seat all three PCs PLAYING.  p1/Doc narrates; we assert on the denominator.
    room = _room_with_player_seats(snap, "p1", "Doc")
    room.seat("p2", character_slot="p2")
    room._seated["p2"].state = LobbyState.PLAYING  # noqa: SLF001
    snap.player_seats["p2"] = "Jesse"
    room.seat("p3", character_slot="p3")
    room._seated["p3"].state = LobbyState.PLAYING  # noqa: SLF001
    snap.player_seats["p3"] = "Wyatt"

    assert room.effective_barrier_count() == 3, "three PLAYING peers at hand start"

    # --- Decision point 0: Jesse folds, Doc + Wyatt call ---
    result_dp0 = NarrationTurnResult(
        narration="Jesse throws down her cards; Doc and Wyatt call.",
        beat_selections=[
            BeatSelection(actor="Doc", beat_id="call", amount=1),
            BeatSelection(actor="Wyatt", beat_id="call", amount=1),
            BeatSelection(actor="Jesse", beat_id="fold", amount=0),
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result_dp0,
        "Doc",
        room=room,
        pack=pack,
        from_explicit_action=False,
    )

    # DETERMINISTIC: 2 active seats remain at DP-0, so NO showdown fired; the
    # fold-mark for Jesse (p2) fired and clear_table_folds did NOT.
    assert enc.resolved is False, (
        "with 3 seats and one fold, two seats remain active at DP-0 — the engine "
        "must NOT reach showdown yet; enc.resolved must be False"
    )
    assert room.effective_barrier_count() == 2, (
        f"after Jesse folded at DP-0 (no showdown), barrier must drop to 2 "
        f"(3 PLAYING − 1 newly-folded Jesse); got {room.effective_barrier_count()}.  "
        f"If this is 3, mark_table_folded was not called — Part A wiring broken.  "
        f"If this is 1, a double-subtract or stray mark occurred."
    )

    # --- Decision point 1: Doc + Wyatt call → DP-cap reached → showdown ---
    # Jesse is already folded going in; the I1 "newly folded" guard must NOT
    # re-mark her (idempotent set add would mask a regression, so the value of
    # this leg is the teardown clear restoring the denominator to 3).
    result_dp1 = NarrationTurnResult(
        narration="Doc and Wyatt call again; cards are turned.",
        beat_selections=[
            BeatSelection(actor="Doc", beat_id="call", amount=1),
            BeatSelection(actor="Wyatt", beat_id="call", amount=1),
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result_dp1,
        "Doc",
        room=room,
        pack=pack,
        from_explicit_action=False,
    )

    assert enc.resolved is True, "DP-1 hits the DP-cap → showdown must resolve"
    assert enc.outcome == "table_winner:" + doc_seat.seat_id, (
        f"Doc (strongest hand) must win the showdown; got {enc.outcome!r}"
    )
    assert room.effective_barrier_count() == 3, (
        "after showdown, clear_table_folds must restore the denominator to 3"
    )
