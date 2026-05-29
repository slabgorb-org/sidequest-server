from types import SimpleNamespace

import pytest

import sidequest.game.table.poker  # noqa: F401
from sidequest.game.session import GameSnapshot
from sidequest.game.table.types import TableNeedsOthersError
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    RulesConfig,
    WinCondition,
)
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
    instantiate_table_encounter,
)


def _poker_cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="poker",
        label="Poker",
        category="social",
        resolution_mode=ResolutionMode.table_resolution,
        win_condition=WinCondition.table_showdown,
        table_game="poker",
        max_decision_points=3,
        beats=[
            BeatDef(id="fold", label="Fold", kind="push", stat_check="WIS", base=0),
            BeatDef(id="call", label="Call", kind="push", stat_check="WIS", base=0),
        ],
    )


def test_builds_table_state_with_seats_and_deals():
    cdef = _poker_cdef()
    enc = instantiate_table_encounter(
        cdef=cdef,
        player_names=["Doc"],
        npc_names=["Ringo", "Bart"],
        stake_kind="money",
        stake_descriptor="the pot",
        seed=7,
    )
    assert enc.table_state is not None
    assert enc.win_condition == "table_showdown"
    assert len(enc.table_state.seats) == 3
    # PCs and NPCs both seated; each has a dealt hand
    for seat in enc.table_state.seats:
        assert "strength" in seat.private_state
    # one EncounterActor per seat for barrier/perception plumbing
    assert len(enc.actors) == 3


def test_single_seat_fails_loud():
    cdef = _poker_cdef()
    with pytest.raises(TableNeedsOthersError):
        instantiate_table_encounter(
            cdef=cdef,
            player_names=["Doc"],
            npc_names=[],
            stake_kind="money",
            stake_descriptor="the pot",
            seed=7,
        )


def test_trigger_branch_declines_gracefully_when_no_table_mates():
    """Wiring test: drive the real trigger function with a table cdef, an empty
    npcs_present, and a snapshot whose location yields no NPCs. The branch must
    DECLINE (return None) rather than let deal_table's TableNeedsOthersError
    500 the turn — and must NOT set snapshot.encounter. (Mirrors the
    adversarial no-opponent graceful-decline contract; the full end-to-end
    happy path is exercised in Task 15.)
    """
    cdef = _poker_cdef()
    # Lightweight real pack stand-in: the branch only reads pack.rules
    # (find_confrontation_def over confrontations, plus ruleset on the happy
    # path which this no-mates case never reaches).
    pack = SimpleNamespace(rules=RulesConfig(ruleset="native", confrontations=[cdef]))

    snap = GameSnapshot(genre="spaghetti_western")
    snap.genre_slug = "spaghetti_western"
    # PC has a resolved location, but no NPCs are seated there → location
    # fallback returns (location_available=True, empty mentions).
    snap.character_locations["Doc"] = "saloon"

    result = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,  # type: ignore[arg-type]
        encounter_type="poker",
        player_name="Doc",
        npcs_present=[],
        genre_slug="spaghetti_western",
    )
    assert result is None  # graceful decline, no raise
    assert snap.encounter is None  # encounter was NOT set
