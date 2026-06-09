"""Story 86-6 (RED): War Rig crew table-game kind + custom-beat dispatch seam.

The crewed War Rig is the cooperative N-seat generalization of 86-2's solo
two-pool combat: multiple PCs man stations (driver / gunner / wrench / spotter /
road_boss) on ONE shared Hull, each committing a concurrent station verb per
round (the SOUL Guitar-Solo wider-action). Per the design spec
``docs/superpowers/specs/2026-06-09-road-warrior-war-rig-crew-spec.md`` the fork
is settled: crewed combat REUSES the ADR-129 N-Seat Table Engine
(``resolution_mode: table_resolution``) as a new ``war_rig_crew`` table-game
kind — NOT the ADR-077 dogfight sealed-letter infra, NOT ``beat_selection``, and
NOT a ``seat`` field on ``EncounterActor`` (``TableSeat`` already IS the seat
primitive).

This file pins the UNIT contract for two of the four gaps:
  • G1 — the ``war_rig_crew`` kind is registered and deals crew stations.
  • G2 — the engine grows a custom-beat dispatch seam so a kind's station verbs
    (``steer`` / ``shoot`` / ``repair`` / ``scan``) resolve, while a kind that
    does NOT handle a beat STILL fails loud (No Silent Fallbacks). Today
    ``engine._apply_signature_beat`` raises ``ValueError`` on any non-pot,
    non-signature beat — there is no per-kind dispatch.

**Proposed seam (TEA contract, open to Dev refinement):**
  - module ``sidequest.game.table.war_rig`` registering
    ``WarRigCrewTableGame(TableGame)`` with ``kind = "war_rig_crew"``;
  - ``TableGame.custom_beat(state, seat, commit, *, rng) -> None`` — default
    raises (preserving fail-loud); ``engine._apply_signature_beat`` dispatches
    unknown beats to ``game.custom_beat(...)`` before its terminal ``raise``.

RED until Dev registers the kind and wires the seam. Imports live inside the
test bodies so a missing module fails THESE tests, not collection of the suite.
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.table.types import (
    TableCommit,
    TablePot,
    TableSeat,
    TableState,
)

# Station roles per the design spec §4.3 mapping (SWN departments → rig stations).
WAR_RIG_STATIONS = {"driver", "gunner", "wrench", "spotter", "road_boss"}
# Crew station verbs the kind must resolve through the custom-beat seam.
WAR_RIG_VERBS = {"steer", "shoot", "repair", "scan"}


def _war_rig_state(n_crew: int = 3, max_dp: int = 4) -> TableState:
    """A war_rig_crew table with ``n_crew`` PC station seats.

    max_decision_points is set high so a single resolved decision point does NOT
    trip the showdown branch — this isolates the custom-beat dispatch from the
    cooperative win-condition semantics (Dev's design; out of unit scope here).
    """
    seats = [
        TableSeat(
            seat_id=f"seat_{i}",
            party_name=f"Crew{i}",
            is_pc=True,
            status="active",
            private_state={},
        )
        for i in range(1, n_crew + 1)
    ]
    return TableState(
        game_kind="war_rig_crew",
        seats=seats,
        pot=TablePot(
            stake_kind="information",
            stake_descriptor="the convoy's fate",
            contributions={s.seat_id: 0 for s in seats},
        ),
        order=[s.seat_id for s in seats],
        dealer_seat="seat_1",
        max_decision_points=max_dp,
    )


# ---------------------------------------------------------------------------
# G1 — kind registration
# ---------------------------------------------------------------------------


def test_war_rig_crew_kind_is_registered():
    """Importing the table PACKAGE (the production path) registers
    ``war_rig_crew``, resolvable by the same path poker/auction use.

    Imports ``sidequest.game.table`` — NOT ``sidequest.game.table.war_rig``
    directly — because production reaches the kind only via the package
    ``__init__`` side-effect imports, never by importing the kind module by
    hand. (The pollution-proof proof is
    :func:`test_war_rig_crew_registered_via_production_package_import`, which
    runs in a fresh interpreter; this in-process check is the fast guard.)
    """
    import sidequest.game.table  # noqa: F401  (package __init__ registers built-in kinds)
    from sidequest.game.table.registry import get_table_game

    game = get_table_game("war_rig_crew")
    assert game.kind == "war_rig_crew", (
        f"war_rig_crew kind must self-identify as 'war_rig_crew', got {game.kind!r}"
    )


def test_war_rig_crew_registered_via_production_package_import():
    """Pollution-proof wiring proof: a FRESH interpreter importing ONLY the
    production package ``sidequest.game.table`` must resolve ``war_rig_crew``.

    In-process tests cannot prove this — any sibling test that does
    ``import sidequest.game.table.war_rig`` registers the kind process-wide and
    masks a missing ``__init__`` wiring. (Story 86-6 review: that exact
    false-green shipped a kind that raised ``UnknownTableGameError`` in
    production.) A subprocess is the only way to prove the package's ``__init__``
    side-effect imports register the kind. Mirrors
    ``tests/magic/test_production_registration_wiring.py``. The script MUST NOT
    import ``sidequest.game.table.war_rig`` directly — doing so would prove
    nothing (that IS the bug).
    """
    import subprocess
    import sys
    import textwrap

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sidequest.game.table  # production package entrypoint ONLY
                from sidequest.game.table.registry import get_table_game

                game = get_table_game("war_rig_crew")
                assert game.kind == "war_rig_crew", game.kind
                print("OK", game.kind)
                """
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "production package import sidequest.game.table did not register "
        f"war_rig_crew (subprocess exit={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


def test_unregistered_kind_still_fails_loud():
    """Regression guard: registering war_rig_crew must NOT weaken the fail-loud
    contract for genuinely unknown kinds."""
    import sidequest.game.table.war_rig  # noqa: F401
    from sidequest.game.table.registry import UnknownTableGameError, get_table_game

    with pytest.raises(UnknownTableGameError):
        get_table_game("no_such_table_game")


# ---------------------------------------------------------------------------
# G1 — crew stations dealt onto seats
# ---------------------------------------------------------------------------


def test_deal_assigns_a_station_role_to_every_crew_seat():
    """Each crew seat's ``private_state`` must carry a recognized ``station``
    after the deal — the station is what gives a seat its concurrent verb."""
    import sidequest.game.table.war_rig  # noqa: F401
    from sidequest.game.table.engine import deal_table

    st = _war_rig_state(n_crew=3)
    deal_table(st, rng=random.Random(7))

    stations = [s.private_state.get("station") for s in st.seats]
    assert all(stations), f"every crew seat needs a station, got {stations!r}"
    assert set(stations) <= WAR_RIG_STATIONS, (
        f"stations must come from the SWN→rig mapping {sorted(WAR_RIG_STATIONS)}, got {stations!r}"
    )


def test_single_seat_crew_is_the_degenerate_solo_rig():
    """Spec §4.3: a single-seat fighter == solo rig — one crew member holding
    all stations. The kind must deal a 1-crew vessel WITHOUT relying on the
    >=2 *crew* assumption (the 'other' is the external threat, seated
    separately). The lone seat still receives a station.

    NOTE (TEA contract): this asserts the kind can seed a one-crew vessel; how
    the opposing threat is seated is Dev's modeling choice and is NOT pinned
    here. If Dev models the threat as a second seat, this test's vessel-build
    helper is the seam that adapts.
    """
    import sidequest.game.table.war_rig as war_rig  # noqa: F401
    from sidequest.game.table.registry import get_table_game

    game = get_table_game("war_rig_crew")
    lone = TableSeat(
        seat_id="seat_1",
        party_name="Lone Wolf",
        is_pc=True,
        status="active",
        private_state={},
    )
    pot = TablePot(stake_kind="information", stake_descriptor="survival", contributions={})
    # Deal directly onto the lone crew seat via the kind (bypassing the engine's
    # >=2 *seat* guard, which is about needing an Other, not about crew size).
    game.deal([lone], pot, random.Random(1))
    assert lone.private_state.get("station") in WAR_RIG_STATIONS, (
        f"the solo driver must still get a station, got {lone.private_state!r}"
    )


# ---------------------------------------------------------------------------
# G2 — custom-beat dispatch seam
# ---------------------------------------------------------------------------


def test_war_rig_station_verb_resolves_without_unsupported_beat_error():
    """A ``war_rig_crew`` seat committing a station verb (e.g. ``shoot``) must
    resolve through the new per-kind custom-beat seam — NOT trip the engine's
    'unsupported table beat' ValueError. One decision point, >1 active crew,
    so the showdown branch (and its strength() call) does not fire.
    """
    import sidequest.game.table.war_rig  # noqa: F401
    from sidequest.game.table.engine import deal_table, resolve_table

    st = _war_rig_state(n_crew=3, max_dp=4)
    deal_table(st, rng=random.Random(3))
    commits = {
        "seat_1": TableCommit(seat_id="seat_1", beat_id="steer"),
        "seat_2": TableCommit(seat_id="seat_2", beat_id="shoot", target_seat="seat_3"),
        "seat_3": TableCommit(seat_id="seat_3", beat_id="repair"),
    }
    # RED: today this raises ValueError("unsupported table beat 'steer' …").
    outcome = resolve_table(st, commits=commits, rng=random.Random(3))
    assert outcome.showdown is False, (
        "one decision point with multiple active crew should not be a showdown"
    )


def test_war_rig_unknown_station_verb_fails_loud():
    """No Silent Fallbacks (kind-level): a verb war_rig_crew does NOT register
    (e.g. ``fly``) must raise through ``WarRigCrewTableGame.custom_beat`` — a
    DISTINCT fail-loud path from the ``TableGame`` ABC default that poker hits.
    Guards against a future widening of WAR_RIG_STATION_VERBS or a dropped
    guard silently turning an unknown station verb into a no-op."""
    import sidequest.game.table.war_rig  # noqa: F401
    from sidequest.game.table.engine import deal_table, resolve_table

    st = _war_rig_state(n_crew=3, max_dp=4)
    deal_table(st, rng=random.Random(3))
    commits = {"seat_1": TableCommit(seat_id="seat_1", beat_id="fly")}
    with pytest.raises(ValueError):
        resolve_table(st, commits=commits, rng=random.Random(3))


def test_poker_unsupported_beat_still_fails_loud_after_seam():
    """No Silent Fallbacks: the custom-beat seam must NOT turn an unknown beat
    into a silent no-op for kinds that don't handle it. Poker does not register
    a station verb, so committing ``shoot`` to a poker table MUST still raise.

    Regression guard — green today (the terminal ValueError), and it must STAY
    green after Dev adds the seam. If this flips to passing-silently, the seam
    swallowed an error.
    """
    import sidequest.game.table.poker  # noqa: F401  (registers poker)
    from sidequest.game.table.engine import deal_table, resolve_table

    seats = [
        TableSeat(
            seat_id=f"seat_{i}",
            party_name=f"P{i}",
            is_pc=True,
            status="active",
            private_state={},
        )
        for i in (1, 2)
    ]
    st = TableState(
        game_kind="poker",
        seats=seats,
        pot=TablePot(
            stake_kind="money",
            stake_descriptor="the pot",
            contributions={s.seat_id: 0 for s in seats},
        ),
        order=[s.seat_id for s in seats],
        dealer_seat="seat_1",
        max_decision_points=3,
    )
    deal_table(st, rng=random.Random(5))
    commits = {"seat_1": TableCommit(seat_id="seat_1", beat_id="shoot")}
    with pytest.raises(ValueError):
        resolve_table(st, commits=commits, rng=random.Random(5))
