import logging
from types import SimpleNamespace

import pytest

import sidequest.game.table.poker  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.disposition import Attitude, Disposition
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.table.types import TableNeedsOthersError
from sidequest.game.turn import TurnManager
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    RulesConfig,
    WinCondition,
)
from sidequest.server.dispatch.encounter_lifecycle import (
    _build_table_seat_seeds,
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


# ---------------------------------------------------------------------------
# C2 integration-gap fix: seat_seeds wiring tests
# ---------------------------------------------------------------------------


def _make_pc(name: str, wis: int, dex: int) -> Character:
    """Helper: build a Character with known WIS + DEX for seed verification."""
    return Character(
        core=CreatureCore(
            name=name,
            description="A gambler",
            personality="cool",
            inventory=Inventory(gold=50),
        ),
        char_class="Gunslinger",
        race="Human",
        backstory="Drifter.",
        stats={"STR": 10, "DEX": dex, "CON": 10, "INT": 10, "WIS": wis, "CHA": 10},
    )


def _make_npc(name: str, *, ocean: dict | None, disposition_value: int) -> Npc:
    """Helper: build an Npc with known OCEAN and disposition for seed verification."""
    return Npc(
        core=CreatureCore(
            name=name,
            description="Gambler",
            personality="Shady",
        ),
        disposition=Disposition(disposition_value),
        ocean=ocean,
    )


def test_seat_seeds_applied_before_deal_and_not_clobbered():
    """Pass explicit seat_seeds into instantiate_table_encounter; verify each
    seat's private_state carries the seeded keys AFTER deal (deal adds its own
    keys without clobbering the seeds)."""
    cdef = _poker_cdef()
    enc = instantiate_table_encounter(
        cdef=cdef,
        player_names=["Doc"],
        npc_names=["Ringo"],
        stake_kind="money",
        stake_descriptor="the pot",
        seed=42,
        seat_seeds={
            "Doc": {"perception": 3, "concealment": 1},
            "Ringo": {"ocean": {"neuroticism": 0.8}, "disposition": "larcenous"},
        },
    )
    ts = enc.table_state
    doc_seat = next(s for s in ts.seats if s.party_name == "Doc")
    ringo_seat = next(s for s in ts.seats if s.party_name == "Ringo")

    # Seeded keys present after deal
    assert doc_seat.private_state["perception"] == 3
    assert doc_seat.private_state["concealment"] == 1
    assert ringo_seat.private_state["ocean"] == {"neuroticism": 0.8}
    assert ringo_seat.private_state["disposition"] == "larcenous"

    # Deal did NOT clobber seeds — dealt keys co-exist
    assert "strength" in doc_seat.private_state, "poker deal must add 'strength'"
    assert "cards" in doc_seat.private_state, "poker deal must add 'cards'"
    assert "cheat_trace" in ringo_seat.private_state, "poker deal must add 'cheat_trace'"
    assert "strength" in ringo_seat.private_state, "poker deal must add 'strength'"


def test_build_table_seat_seeds_pc_perception_concealment():
    """Unit test: _build_table_seat_seeds derives correct WIS/DEX modifiers.

    WIS 14 → modifier (14-10)//2 = 2 → perception 2.
    DEX 8  → modifier  (8-10)//2 = -1 → concealment -1.
    """
    pc = _make_pc("Doc", wis=14, dex=8)
    snap = GameSnapshot(genre="spaghetti_western")
    snap.characters.append(pc)

    seeds = _build_table_seat_seeds(
        player_names=["Doc"],
        npc_names=[],
        snapshot=snap,
    )
    assert seeds["Doc"]["perception"] == 2
    assert seeds["Doc"]["concealment"] == -1


def test_build_table_seat_seeds_npc_ocean_and_disposition_neutral():
    """NPC with neutral disposition → private_state["disposition"] == "neutral"."""
    npc = _make_npc("Ringo", ocean={"neuroticism": 0.6}, disposition_value=0)
    snap = GameSnapshot(genre="spaghetti_western")
    snap.npcs.append(npc)

    seeds = _build_table_seat_seeds(
        player_names=[],
        npc_names=["Ringo"],
        snapshot=snap,
    )
    assert seeds["Ringo"]["ocean"] == {"neuroticism": 0.6}
    assert seeds["Ringo"]["disposition"] == "neutral"


def test_build_table_seat_seeds_hostile_npc_maps_to_larcenous():
    """NPC with HOSTILE disposition → private_state["disposition"] == "larcenous"
    (hostile is the real-model analog of cheat-inclined at the table)."""
    npc = _make_npc("Snake", ocean={"neuroticism": 0.3}, disposition_value=-50)
    snap = GameSnapshot(genre="spaghetti_western")
    snap.npcs.append(npc)
    # Confirm the NPC is actually HOSTILE before asserting the mapping
    assert npc.disposition.attitude() == Attitude.HOSTILE

    seeds = _build_table_seat_seeds(
        player_names=[],
        npc_names=["Snake"],
        snapshot=snap,
    )
    assert seeds["Snake"]["disposition"] == "larcenous"


def test_build_table_seat_seeds_friendly_npc_maps_to_neutral():
    """NPC with FRIENDLY disposition takes the else branch → "neutral"
    (only HOSTILE maps to "larcenous"; friendly NPCs are not cheat-inclined)."""
    npc = _make_npc("Pal", ocean={"neuroticism": 0.2}, disposition_value=50)
    snap = GameSnapshot(genre="spaghetti_western")
    snap.npcs.append(npc)
    # Confirm the NPC is actually FRIENDLY before asserting the mapping
    assert npc.disposition.attitude() == Attitude.FRIENDLY

    seeds = _build_table_seat_seeds(
        player_names=[],
        npc_names=["Pal"],
        snapshot=snap,
    )
    assert seeds["Pal"]["disposition"] == "neutral"


def test_build_table_seat_seeds_missing_pc_seeds_empty_with_warning(caplog):
    """A PC name not in snapshot.characters yields empty seed + WARNING log."""
    snap = GameSnapshot(genre="spaghetti_western")
    # No characters — lookup will miss
    with caplog.at_level(logging.WARNING):
        seeds = _build_table_seat_seeds(
            player_names=["Ghost"],
            npc_names=[],
            snapshot=snap,
        )
    assert seeds["Ghost"] == {}
    assert any("Ghost" in r.message for r in caplog.records)


def test_build_table_seat_seeds_missing_npc_seeds_empty_with_warning(caplog):
    """An NPC name not in snapshot.npcs yields empty seed + WARNING log."""
    snap = GameSnapshot(genre="spaghetti_western")
    with caplog.at_level(logging.WARNING):
        seeds = _build_table_seat_seeds(
            player_names=[],
            npc_names=["Phantom"],
            snapshot=snap,
        )
    assert seeds["Phantom"] == {}
    assert any("Phantom" in r.message for r in caplog.records)


def test_trigger_branch_seeds_from_real_snapshot():
    """Integration: drive instantiate_encounter_from_trigger with a real PC
    (known WIS/DEX) and NPC (known OCEAN/disposition) and assert the seeded
    keys arrive on the correct seats (not from test injection — from the
    production trigger path reading real sheets).

    WIS 16 → perception (16-10)//2 = 3.
    DEX 12 → concealment (12-10)//2 = 1.
    NPC disposition 0 (neutral) → "neutral".
    """
    cdef = _poker_cdef()
    pack = SimpleNamespace(rules=RulesConfig(ruleset="native", confrontations=[cdef]))

    pc = _make_pc("Doc", wis=16, dex=12)
    npc = _make_npc("Ringo", ocean={"neuroticism": 0.4}, disposition_value=0)
    # Seat both at the same location so the location-fallback seats the NPC
    npc.last_seen_location = "saloon"

    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(pc)
    snap.npcs.append(npc)
    snap.character_locations["Doc"] = "saloon"
    snap.genre_slug = "spaghetti_western"

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,  # type: ignore[arg-type]
        encounter_type="poker",
        player_name="Doc",
        npcs_present=[],
        genre_slug="spaghetti_western",
    )
    assert enc is not None, "trigger should seat Doc + location-fallback Ringo"
    ts = enc.table_state
    doc_seat = next(s for s in ts.seats if s.party_name == "Doc")
    ringo_seat = next(s for s in ts.seats if s.party_name == "Ringo")

    # PC seats: WIS-derived perception + DEX-derived concealment
    assert doc_seat.private_state["perception"] == 3
    assert doc_seat.private_state["concealment"] == 1

    # NPC seats: OCEAN dict + disposition string
    assert ringo_seat.private_state["ocean"] == {"neuroticism": 0.4}
    assert ringo_seat.private_state["disposition"] == "neutral"

    # Deal ran on top of seeds — both have dealt cards/strength alongside seeds
    assert "strength" in doc_seat.private_state
    assert "strength" in ringo_seat.private_state
