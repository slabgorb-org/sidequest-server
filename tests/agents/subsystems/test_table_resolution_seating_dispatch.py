"""Story 126-2 — re-verify router-driven table_resolution seating (regression lock).

Re-verification outcome: GREEN. The router-driven seating path works under the
real Fate binding; the "card game broken" symptom was the upstream intent-router
degradation, fixed in 126-9. These tests are the regression lock that proves the
seating contract holds — they pass today by design, not fail.


The sq-playtest "card game totally broken" finding was diagnosed as a
DOWNSTREAM effect of the intent-router ``error_max_turns`` blocker (fixed
in 126-9), NOT a regression in the table engine. N-seat table games seat
only when a ``ResolutionMode.table_resolution`` ConfrontationDef arrives
via the router-driven dispatch path (post-ADR-113): the router emits a
``SubsystemDispatch(subsystem="confrontation", params={"type": "poker"})``,
``run_dispatch_bank`` invokes ``run_confrontation_dispatch``, which calls
``instantiate_encounter_from_trigger`` → the ``table_resolution`` branch →
``instantiate_table_encounter`` → ``snapshot.encounter`` becomes a seated
table.

When the router was degraded (``dispatch_package=None``) the confrontation
dispatch never fired, ``run_confrontation_dispatch`` was never called, the
table branch was never reached, and the live save showed ``encounter=None``
— the player saw a "card game" with no seats, no antes, no decision loop.

These tests pin the SEATING CONTRACT at the router-driven dispatch entry
(the exact seam that was bypassed), independent of the live LLM router
(per SM assessment: test the contract, not the Haiku call). The companion
file ``tests/server/test_table_resolution_wiring.py`` already pins the
RESOLUTION half (commits → showdown → pot award); this file pins the
INSTANTIATION half that produced ``encounter=None``.

Per CLAUDE.md "No Source-Text Wiring Tests": every assertion is on the
mutated snapshot, the seated TableState, or an OTEL span — never a grep of
production source. The ``table.dealt`` span is the GM-panel lie-detector
that proves the table was actually seated+dealt, not just narrated.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.game.table.poker  # noqa: F401 — registers the poker kind at import
from sidequest.agents.orchestrator import NpcMention
from sidequest.agents.subsystems import SubsystemOutput, run_dispatch_bank
from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    FateConfig,
    ResolutionMode,
    RulesConfig,
    WinCondition,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# OTEL capture — same canonical fixture as test_table_resolution_wiring.py
# (attach an in-memory exporter to the running global TracerProvider, which
# is what the table spans write into via Span.open / the global tracer).
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture():
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
# Shared builders — poker table_resolution cdef + synthetic pack + snapshot.
# Mirrors the proven fixture shape from test_table_resolution_wiring.py and
# test_confrontation_dispatch.py so the GREEN implementation can't drift.
# ---------------------------------------------------------------------------

POKER_TYPE = "poker"


def _poker_cdef() -> ConfrontationDef:
    """A table_resolution poker confrontation — the kind that seats an
    N-seat table (antes + sealed-commit fold/call loop), NOT a dial duel."""
    return ConfrontationDef(
        type=POKER_TYPE,
        label="Poker",
        category="social",
        resolution_mode=ResolutionMode.table_resolution,
        win_condition=WinCondition.table_showdown,
        table_game="poker",
        max_decision_points=1,
        beats=[
            BeatDef(id="fold", label="Fold", kind="push", stat_check="WIS", base=0),
            BeatDef(id="call", label="Call", kind="push", stat_check="WIS", base=0),
        ],
    )


def _synthetic_pack_with_poker() -> GenrePack:
    """GenrePack carrying the poker cdef bound to the REAL production ruleset.

    The live poker table ships in ``spaghetti_western``, which binds **Fate**
    (ADR-144) — and this story is a Fate-port playtest follow-up, so the
    regression lock must run under the Fate binding, not the vestigial
    ``dial`` one (no live pack binds dial — testing it would verify a path
    production never takes).

    Table resolution is ruleset-agnostic by design: ``FateRulesetModule`` does
    NOT override ``deal_table``/``resolve_table`` — they are concrete on the
    base RulesetModule and delegate to the per-kind ``game/table/engine.py``
    registry ("orthogonal to combat resolution — every ruleset inherits it").
    The sibling Fate-bound auction table (``tea_and_murder``) and the CWN-bound
    ``war_rig_crew`` table (``road_warrior``) ride the same inherited path.
    """
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="fate",
        confrontations=[_poker_cdef()],
        fate=FateConfig(),  # ruleset=fate fails loud without a FateConfig (ADR-144 F4a)
    )
    return pack


def _pc(name: str, gold: int = 50) -> Character:
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


def _snapshot_with_pc(player_name: str = "Doc") -> GameSnapshot:
    """Snapshot with one seated PC and NO active encounter — the precondition
    the table dispatch must satisfy to actually instantiate the table."""
    snap = GameSnapshot(
        genre_slug="spaghetti_western",
        world_slug="test_world",
        encounter=None,
        turn_manager=TurnManager(),
        player_seats={f"player:{player_name}": player_name},
    )
    snap.characters.append(_pc(player_name))
    snap.turn_manager.record_interaction()
    return snap


def _table_dispatch(*, enc_type: str = POKER_TYPE) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="confrontation",
        params={"type": enc_type},
        idempotency_key="k-table-1",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


# ---------------------------------------------------------------------------
# AC1 — the core seating contract (the field that was None in the live save).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_resolution_dispatch_seats_the_table() -> None:
    """THE seating assertion: a router-driven confrontation dispatch for a
    ``table_resolution`` cdef must mutate ``snapshot.encounter`` from None
    into a fully-seated N-seat table.

    This is the exact field that read ``encounter=None`` in the broken live
    save — not because the table engine was broken, but because the degraded
    router never fired this dispatch. With the router restored, the dispatch
    fires and the table seats.

    Contract pinned:
      - encounter goes None → not-None (seating happened)
      - encounter_type honors dispatch.params["type"]
      - a TableState exists with the poker game_kind
      - >= 2 seats (the PC + the named opponent) — a 1-seat hand is not a
        confrontation (ADR-116)
      - exactly one PC seat (the dispatching player) and >= 1 NPC seat
      - win_condition == table_showdown
      - the pot tracks a contribution slot per seat (the ante seam)
    """
    snap = _snapshot_with_pc("Doc")
    pack = _synthetic_pack_with_poker()
    assert snap.encounter is None, "precondition: no encounter seated yet"

    out = await run_confrontation_dispatch(
        _table_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name="Doc",
        npcs_present=[NpcMention(name="Ringo", side="opponent")],
    )

    assert isinstance(out, SubsystemOutput), "handler must return a SubsystemOutput"

    enc = snap.encounter
    assert enc is not None, (
        "table_resolution dispatch did NOT seat an encounter — snapshot.encounter "
        "is still None. This is the live-save bug: the seating path was never "
        "reached. If the router is firing, instantiate_table_encounter must run."
    )
    assert enc.encounter_type == POKER_TYPE, (
        f"encounter must honor dispatch.params['type']={POKER_TYPE!r}; got {enc.encounter_type!r}"
    )

    ts = enc.table_state
    assert ts is not None, (
        "a seated table_resolution encounter must carry a TableState — without "
        "it the engine has no seats/pot/deck to run the card game"
    )
    assert ts.game_kind == "poker", f"game_kind must be 'poker'; got {ts.game_kind!r}"

    assert len(ts.seats) >= 2, (
        f"a table confrontation needs >= 2 seats (PC + other); got {len(ts.seats)}. "
        "A lone-PC table is not a confrontation (ADR-116)."
    )
    pc_seats = [s for s in ts.seats if s.is_pc]
    npc_seats = [s for s in ts.seats if not s.is_pc]
    assert len(pc_seats) == 1, f"exactly one PC seat expected; got {len(pc_seats)}"
    assert pc_seats[0].party_name == "Doc", (
        f"the dispatching player must be seated; got {pc_seats[0].party_name!r}"
    )
    assert len(npc_seats) >= 1, "the named opponent must be seated as an NPC seat"
    assert any(s.party_name == "Ringo" for s in npc_seats), (
        "the router-named opponent 'Ringo' must occupy a seat"
    )

    assert enc.win_condition == WinCondition.table_showdown.value, (
        f"win_condition must be table_showdown; got {enc.win_condition!r}"
    )

    # The pot's contribution map is the ante seam — one slot per seat.
    assert set(ts.pot.contributions.keys()) == {s.seat_id for s in ts.seats}, (
        "pot must track a contribution slot for every seat (the ante ledger); "
        f"seats={sorted(s.seat_id for s in ts.seats)} "
        f"pot={sorted(ts.pot.contributions)}"
    )


# ---------------------------------------------------------------------------
# AC1 (wiring) — the SAME contract end-to-end through the real dispatch bank,
# the path the degraded router bypassed. CLAUDE.md "Every Test Suite Needs a
# Wiring Test": prove the bank routes a table confrontation to seating.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_resolution_seats_through_real_dispatch_bank() -> None:
    """End-to-end through ``run_dispatch_bank`` — a DispatchPackage with one
    confrontation dispatch for a table_resolution cdef must leave the snapshot
    with a seated table.

    This is the production seam: the router builds the package, the bank fans
    it out to ``run_confrontation_dispatch``. If the bank fails to route the
    dispatch (unregistered handler, kwarg-filter mismatch, silent no-op) the
    encounter stays None — exactly the live-save symptom — and this fails.
    """
    snap = _snapshot_with_pc("Doc")
    pack = _synthetic_pack_with_poker()
    package = DispatchPackage(
        turn_id="turn-1",
        per_player=[
            PlayerDispatch(
                player_id="player:Doc",
                raw_action="I sit down at the poker table and ante up.",
                dispatch=[_table_dispatch()],
            )
        ],
        confidence_global=1.0,
    )

    await run_dispatch_bank(
        package,
        context={
            "snapshot": snap,
            "pack": pack,
            "player_name": "Doc",
            "npcs_present": [NpcMention(name="Ringo", side="opponent")],
        },
    )

    enc = snap.encounter
    assert enc is not None and enc.encounter_type == POKER_TYPE, (
        "dispatch bank did not seat the table — the confrontation handler was "
        "unregistered, the kwargs were filtered out, or it no-op'd silently. "
        "This is the router-driven path that showed encounter=None in the save."
    )
    assert enc.table_state is not None and len(enc.table_state.seats) >= 2, (
        "the bank-routed dispatch must produce a fully seated table, not an empty encounter shell"
    )


# ---------------------------------------------------------------------------
# OTEL lie-detector — the table was actually DEALT, not just narrated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_resolution_dispatch_emits_table_dealt_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """The ``table.dealt`` span must fire during seating.

    Per CLAUDE.md's OTEL Observability Principle, the GM panel is the lie
    detector: if seating happened, ``table.dealt`` fires with the real seat
    count and game_kind. A narrator could *say* "you're dealt in" while the
    engine seated nothing; this span is how we tell engagement from prose.
    """
    snap = _snapshot_with_pc("Doc")
    pack = _synthetic_pack_with_poker()

    await run_confrontation_dispatch(
        _table_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name="Doc",
        npcs_present=[NpcMention(name="Ringo", side="opponent")],
    )

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "table.dealt" in span_names, (
        "table.dealt span did not fire — the table was never seated+dealt "
        f"through the dispatch path. Spans captured: {span_names}"
    )
    dealt = next(s for s in otel_capture.get_finished_spans() if s.name == "table.dealt")
    assert dealt.attributes.get("seat_count", 0) >= 2, (
        f"table.dealt must record >= 2 seats; got seat_count={dealt.attributes.get('seat_count')!r}"
    )
    assert dealt.attributes.get("game_kind") == "poker", (
        f"table.dealt must record game_kind='poker'; got {dealt.attributes.get('game_kind')!r}"
    )


# ---------------------------------------------------------------------------
# Guard (paranoid edge) — a lone PC with no other seat must NOT seat a table.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_resolution_declines_when_no_other_seat(
    otel_capture: InMemorySpanExporter,
) -> None:
    """A table_resolution dispatch with no opponent (explicit or via location
    fallback) must DECLINE: leave ``snapshot.encounter`` None and fire the
    ``encounter.no_opponent_available`` span.

    A one-seat hand is not a confrontation (ADR-116, generalized by
    TableNeedsOthersError). The branch returns None rather than 500-ing the
    turn on TableNeedsOthersError — but it must NOT silently seat a lonely PC
    at an empty table (which would be its own flavor of "broken card game").
    """
    snap = _snapshot_with_pc("Doc")  # no NPCs in snapshot → location fallback empty
    pack = _synthetic_pack_with_poker()

    out = await run_confrontation_dispatch(
        _table_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name="Doc",
        npcs_present=[],  # no explicit opponent
    )

    assert isinstance(out, SubsystemOutput)
    assert snap.encounter is None, (
        "a table with no other player must NOT seat — got a seated encounter "
        "for a lone PC, which violates the requires-an-Other invariant (ADR-116)"
    )
    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "encounter.no_opponent_available" in span_names, (
        "the no-opponent guard must fire its OTEL span so the GM panel sees why "
        f"no table seated. Spans captured: {span_names}"
    )
