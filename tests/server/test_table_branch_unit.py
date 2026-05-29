"""Unit tests for Task 12 — BeatSelection.amount field + table-branch wiring.

Tests the new optional ``amount`` field on BeatSelection (poker/auction
raise/bet chips) and its ``from_dict`` parse path, PLUS a focused
end-to-end test that drives a ``table_resolution`` encounter through the
REAL ``_apply_narration_result_to_snapshot`` and asserts the PC winner's
gold actually increases (the proof that the money pot award is a real,
auditable state mutation, not just a field set). The broader end-to-end
flow (NPC seats, spans, perception) lives in Task 15's wiring test.
"""

from unittest.mock import MagicMock

import pytest

import sidequest.game.table.poker  # noqa: F401  (registers the poker kind)
from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
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
from sidequest.server.narration_apply import (
    _apply_narration_result_to_snapshot,
    _gate_applies_to_encounter,
)
from tests._helpers.session_room import room_for


def test_beat_selection_parses_amount_and_target():
    bs = BeatSelection.from_dict(
        {"actor": "Doc", "beat_id": "raise", "amount": 5, "target": "seat_2"}
    )
    assert bs.amount == 5
    assert bs.target == "seat_2"


def test_beat_selection_amount_defaults_none():
    bs = BeatSelection.from_dict({"actor": "Doc", "beat_id": "fold"})
    assert bs.amount is None


def test_beat_selection_amount_zero_is_valid():
    bs = BeatSelection.from_dict({"actor": "Doc", "beat_id": "call", "amount": 0})
    assert bs.amount == 0


def test_beat_selection_amount_none_explicit():
    bs = BeatSelection.from_dict({"actor": "Doc", "beat_id": "raise", "amount": None})
    assert bs.amount is None


# ---------------------------------------------------------------------------
# End-to-end: a table_resolution turn reaches showdown via the REAL apply
# path and the PC winner's gold actually increases by the pot total.
# ---------------------------------------------------------------------------


def _poker_cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="poker",
        label="Poker",
        category="social",
        resolution_mode=ResolutionMode.table_resolution,
        win_condition=WinCondition.table_showdown,
        table_game="poker",
        max_decision_points=1,  # single resolve goes straight to showdown
        beats=[
            BeatDef(id="fold", label="Fold", kind="push", stat_check="WIS", base=0),
            BeatDef(id="call", label="Call", kind="push", stat_check="WIS", base=0),
        ],
    )


def _poker_table_snapshot():
    """A snapshot carrying an active 2-seat poker table_resolution encounter.

    PC "Doc" gets seat_1 (PCs seated before NPCs), NPC "Ringo" gets seat_2.
    Built via the production instantiate_table_encounter helper so the
    fixture matches the real trigger path.
    """
    cdef = _poker_cdef()
    enc = instantiate_table_encounter(
        cdef=cdef,
        player_names=["Doc"],
        npc_names=["Ringo"],
        stake_kind="money",
        stake_descriptor="the pot",
        seed=1,
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
    # The table branch only reads pack.rules (find_confrontation_def over
    # confrontations + ruleset for get_ruleset_module). A MagicMock pack with
    # a real RulesConfig matches the live shape without GenrePack's full
    # required-field surface (mirrors test_space_opera_hp_e2e).
    pack = MagicMock()
    pack.rules = RulesConfig(ruleset="native", confrontations=[cdef])
    return snap, pack


def test_table_turn_reaches_showdown_and_pc_winner_gains_gold():
    snap, pack = _poker_table_snapshot()
    enc = snap.encounter
    ts = enc.table_state
    # Force a deterministic winner: seat_1 (Doc, the PC) beats seat_2 (Ringo).
    ts.find_seat("seat_1").private_state["strength"] = 10**9
    ts.find_seat("seat_2").private_state["strength"] = 1
    ante_total = sum(ts.pot.contributions.values())
    assert ante_total > 0  # antes seeded the pot at deal time

    doc = next(c for c in snap.characters if c.core.name == "Doc")
    gold_before = int(doc.core.inventory.gold)

    # Both seats call for 1 chip; the FINAL pot (antes + this decision point's
    # contributions) is what the winner rakes.
    result = NarrationTurnResult(
        narration="Doc lays down his hand and rakes the pot.",
        beat_selections=[
            BeatSelection(actor="Doc", beat_id="call", amount=1),
            BeatSelection(actor="Ringo", beat_id="call", amount=1),
        ],
    )
    # from_explicit_action=False — the REAL production narrator path. The PC's
    # classified "call" commit MUST survive the SOUL "The Test" gate
    # (_gate_applies_to_encounter exempts table_resolution, since per-seat
    # commits are the player's explicit consent frame, not inferred prose).
    # If the exemption regresses, _filter_inferred_pc_beats drops Doc's commit,
    # the showdown never fires, and these assertions fail.
    outcome = _apply_narration_result_to_snapshot(
        snap,
        result,
        "Doc",
        room=room_for(snap),
        pack=pack,
        from_explicit_action=False,
    )

    # The final pot is the antes plus each seat's call this decision point.
    final_pot = sum(ts.pot.contributions.values())
    assert final_pot > ante_total  # the calls grew the pot

    # (a) the award is recorded on the outcome with the PC recipient
    assert outcome.table_pot_award is not None
    assert outcome.table_pot_award["recipient"] == "Doc"
    assert outcome.table_pot_award["amount"] == final_pot

    # (b) the REAL state changed — Doc's gold increased by the final pot total
    assert int(doc.core.inventory.gold) == gold_before + final_pot

    # (c) the encounter resolved with the table winner
    assert enc.resolved is True
    assert enc.outcome == "table_winner:seat_1"


def test_table_turn_npc_winner_leaves_pc_gold_unchanged():
    """Error-path coverage: when the NPC seat (seat_2 / Ringo) wins, the award
    is recorded on the outcome but NO PC gold mutates (NPCs aren't in
    snapshot.characters) — the gold-ledger no-op path. Drives the REAL
    production path (from_explicit_action=False)."""
    snap, pack = _poker_table_snapshot()
    enc = snap.encounter
    ts = enc.table_state
    # Force the NPC (seat_2 / Ringo) to win over the PC (seat_1 / Doc).
    ts.find_seat("seat_1").private_state["strength"] = 1
    ts.find_seat("seat_2").private_state["strength"] = 10**9

    doc = next(c for c in snap.characters if c.core.name == "Doc")
    gold_before = int(doc.core.inventory.gold)

    result = NarrationTurnResult(
        narration="Ringo flips his cards and takes the pot.",
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

    # (a) the award names the NPC winner
    assert outcome.table_pot_award is not None
    assert outcome.table_pot_award["recipient"] == "Ringo"

    # (b) the PC's gold is UNCHANGED — NPC win is a gold-ledger no-op
    assert int(doc.core.inventory.gold) == gold_before

    # (c) the encounter resolved with the NPC as winner
    assert enc.resolved is True
    assert enc.outcome == "table_winner:seat_2"


def test_soul_gate_exempts_table_resolution():
    """Lock the gate exemption: _gate_applies_to_encounter must return False
    for a table_resolution confrontation so PC per-seat commits survive the
    live narrator path (from_explicit_action=False). Mirrors the sealed-letter
    exemption rationale — the commit IS the player's consent frame."""
    snap, pack = _poker_table_snapshot()
    assert _gate_applies_to_encounter(snap.encounter, pack) is False


def test_beat_selection_malformed_amount_degrades_to_none():
    """Defensive parse (Minor 1): a malformed amount degrades to None instead
    of throwing an opaque error out of from_dict — parity with gold_change /
    declared_tier handling."""
    bs = BeatSelection.from_dict({"actor": "Doc", "beat_id": "raise", "amount": "lots"})
    assert bs.amount is None


# ---------------------------------------------------------------------------
# I1 — authored-beat validation in the table branch
# ---------------------------------------------------------------------------


def test_pc_unauthored_beat_raises_valueerror():
    """I1: a PC commit with a beat_id not in cdef.beats raises ValueError.

    This locks the table branch's authored-beat validation: an unauthored
    beat (narrator hallucination or NPC-policy bug) is rejected BEFORE
    resolve_table mutates state.  The error message must name the beat and
    the encounter type so the GM panel can surface the drift.
    """
    snap, pack = _poker_table_snapshot()
    # NOTE: relies on _poker_cdef NOT authoring "cheat" (its beats are
    # [fold, call]). If "cheat" is ever added to that fixture, this assertion
    # silently inverts — keep "cheat" out of _poker_cdef for this test to mean
    # what it says. "cheat" not in cdef.beats → committing it from a PC raises.
    result = NarrationTurnResult(
        narration="Doc reaches under the table.",
        beat_selections=[
            BeatSelection(actor="Doc", beat_id="cheat", amount=0),
        ],
    )
    with pytest.raises(ValueError, match="not authored"):
        _apply_narration_result_to_snapshot(
            snap,
            result,
            "Doc",
            room=room_for(snap),
            pack=pack,
            from_explicit_action=False,
        )
