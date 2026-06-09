"""war_rig_crew table-game kind: the cooperative crewed War Rig (Story 86-6).

The crewed War Rig is the N-seat generalization of 86-2's solo two-pool combat:
multiple PCs man stations (driver / gunner / wrench / spotter / road_boss) on ONE
shared Hull, each committing a concurrent station verb per round — the SOUL
Guitar-Solo *wider-action*. Per the design spec
``docs/superpowers/specs/2026-06-09-road-warrior-war-rig-crew-spec.md`` this REUSES
the ADR-129 N-Seat Table Engine rather than the ADR-077 dogfight sealed-letter
infra: ``TableSeat`` already IS the crew-seat primitive, and ``resolve_table()`` is
already the sealed-concurrent round loop.

Two net-new pieces live here (the engine's custom-beat seam carries the rest):
  - ``deal`` stamps each crew seat with a ``station`` from the SWN→rig mapping.
  - ``custom_beat`` resolves the station verbs (steer/shoot/repair/scan) through
    the engine's per-kind dispatch seam, emitting one ``table.commit`` span per
    station action so the GM panel can confirm the cooperative round is
    mechanically backed (the lie-detector mandate).

The shared Hull pool + Hull→0 crash fan-out live in
:mod:`sidequest.game.war_rig_combat`; this module is the table-engine half.
"""

from __future__ import annotations

import random

from sidequest.game.table.registry import TableGame, register_table_game
from sidequest.game.table.types import TableCommit, TablePot, TableSeat, TableState
from sidequest.telemetry.spans import table_commit_span, table_seat_seeded_span

# SWN departments → rig stations (spec §4.3). Dealt round-robin onto crew seats;
# the single-seat fighter (degenerate solo rig) gets the first, driver.
WAR_RIG_STATIONS: tuple[str, ...] = ("driver", "gunner", "wrench", "spotter", "road_boss")

# The concurrent station verbs the kind resolves through the custom-beat seam.
WAR_RIG_STATION_VERBS: frozenset[str] = frozenset({"steer", "shoot", "repair", "scan"})


class WarRigCrewTableGame(TableGame):
    """Cooperative crewed-vessel table game: N crew, one shared Hull."""

    kind = "war_rig_crew"

    def deal(self, seats: list[TableSeat], pot: TablePot, rng: random.Random) -> None:
        """Assign each crew seat a station role (mutates seats in place).

        Stations cycle through :data:`WAR_RIG_STATIONS` in seat order — with
        fewer crew than stations one PC holds each of the first N; a single-seat
        vessel (solo rig) gets the driver's chair. The Hull is vessel-scoped
        (see :mod:`sidequest.game.war_rig_combat`), so the pot carries no antes —
        this is a cooperative stand against an external threat, not a wager.
        """
        for i, seat in enumerate(seats):
            station = WAR_RIG_STATIONS[i % len(WAR_RIG_STATIONS)]
            seat.private_state["station"] = station
            with table_seat_seeded_span(
                seat_id=seat.seat_id,
                party_name=seat.party_name,
                is_pc=seat.is_pc,
                keys_seeded="station",
            ):
                pass

    def strength(self, seat: TableSeat) -> int:
        """Crew don't compete — there is no inter-crew strength comparison.

        Returns 0 uniformly. NOTE: the engine's ``_showdown`` still runs if a
        war_rig table reaches ``max_decision_points`` — with all crew at
        strength 0 it picks ``contenders[0]`` by position (a degenerate, but
        crash-free, "winner"). The *cooperative* win condition (the threat's
        Hull reaching 0) is NOT yet wired into the table showdown — that
        integration is deferred to 86-5/86-7 (see the vessel-side
        :mod:`sidequest.game.war_rig_combat`). For 86-6 the station-verb round
        and the Hull pool are built but their resolution is not yet joined.
        """
        return 0

    def custom_beat(
        self,
        state: TableState,
        seat: TableSeat,
        commit: TableCommit,
        *,
        rng: random.Random,
    ) -> None:
        """Resolve a crew station verb through the engine's custom-beat seam.

        Fails loud on an unrecognized verb (No Silent Fallbacks). Each resolved
        station action emits a ``table.commit`` span — the GM panel's proof the
        cooperative round actually fired, not improvised prose.
        """
        verb = commit.beat_id
        if verb not in WAR_RIG_STATION_VERBS:
            raise ValueError(
                f"war_rig_crew has no station verb {verb!r} for seat {seat.seat_id!r} "
                f"(known verbs: {sorted(WAR_RIG_STATION_VERBS)})"
            )
        with table_commit_span(
            seat=seat.seat_id,
            beat_id=verb,
            amount=commit.amount,
            decision_point=state.decision_point,
        ):
            pass


register_table_game(WarRigCrewTableGame())

__all__ = [
    "WAR_RIG_STATIONS",
    "WAR_RIG_STATION_VERBS",
    "WarRigCrewTableGame",
]
