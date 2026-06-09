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
from sidequest.game.war_rig_combat import WarRigHull
from sidequest.game.war_rig_command import (
    CP_ACTION_COSTS,
    CommandPointPool,
    deal_with_crisis,
    roll_crisis,
    spend_command_points,
)
from sidequest.telemetry.spans import table_commit_span, table_seat_seeded_span

# SWN departments → rig stations (spec §4.3). Dealt round-robin onto crew seats;
# the single-seat fighter (degenerate solo rig) gets the first, driver.
WAR_RIG_STATIONS: tuple[str, ...] = ("driver", "gunner", "wrench", "spotter", "road_boss")

# The concurrent station verbs the kind resolves through the custom-beat seam.
WAR_RIG_STATION_VERBS: frozenset[str] = frozenset({"steer", "shoot", "repair", "scan"})

# The road_boss command layer (Story 86-7): the spendable CP actions and the
# Deal-With-a-Crisis verb resolve through custom_beat alongside the station verbs.
WAR_RIG_COMMAND_VERBS: frozenset[str] = frozenset(CP_ACTION_COSTS)
WAR_RIG_DEAL_WITH_CRISIS: str = "deal_with_crisis"

# Starting Command Points seeded for a crewed vessel (minimal playable per spec §6;
# full per-vessel calibration is 86-5). Stored in TableState.shared_state, shared
# by the whole crew and persistent across the hand's decision points.
WAR_RIG_DEFAULT_COMMAND_POINTS: int = 4
_SHARED_CP_KEY = "command_points"

# Starting shared Hull seeded for the crewed vessel (minimal playable; full vessel
# stat blocks are 86-5). Stored alongside the CP pool in TableState.shared_state so a
# failed continuing crisis damages the SAME Hull across the hand's decision points.
WAR_RIG_DEFAULT_HULL: int = 6
_SHARED_HULL_KEY = "war_rig_hull"


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

    def _vessel_id(self, state: TableState) -> str:
        """A non-blank vessel identifier for the crewed Hull/CP attribution.

        The table model carries no first-class vessel id (the Hull is
        vessel-scoped in :mod:`sidequest.game.war_rig_combat`, separate from the
        table). Derive a stable one from the dealer seat so CP/crisis OTEL is
        attributable; a richer binding to the actual vessel is 86-5's job.
        """
        return f"war_rig_crew:{state.dealer_seat}"

    def _shared_cp_pool(self, state: TableState) -> CommandPointPool:
        """Get-or-seed the crew's SHARED Command Point pool on the table state.

        One pool per vessel, shared by every seat and persistent across the
        hand's decision points (stored in ``TableState.shared_state``)."""
        existing = state.shared_state.get(_SHARED_CP_KEY)
        if isinstance(existing, CommandPointPool):
            return existing
        pool = CommandPointPool(
            current=WAR_RIG_DEFAULT_COMMAND_POINTS,
            max=WAR_RIG_DEFAULT_COMMAND_POINTS,
            vessel_id=self._vessel_id(state),
        )
        state.shared_state[_SHARED_CP_KEY] = pool
        return pool

    def _shared_hull(self, state: TableState) -> WarRigHull:
        """Get-or-seed the crew's SHARED vessel Hull on the table state.

        The same vessel-scoped :class:`~sidequest.game.war_rig_combat.WarRigHull`
        used by 86-2's two-pool model, stored on ``TableState.shared_state`` so a
        failed continuing crisis (``deal_with_crisis``) damages the crew's actual
        Hull across the hand's decision points — not a throwaway. Minimal-playable
        starting Hull; the real vessel stat block is 86-5."""
        existing = state.shared_state.get(_SHARED_HULL_KEY)
        if isinstance(existing, WarRigHull):
            return existing
        hull = WarRigHull(
            current=WAR_RIG_DEFAULT_HULL,
            max=WAR_RIG_DEFAULT_HULL,
            base_max=WAR_RIG_DEFAULT_HULL,
            vessel_id=self._vessel_id(state),
        )
        state.shared_state[_SHARED_HULL_KEY] = hull
        return hull

    def custom_beat(
        self,
        state: TableState,
        seat: TableSeat,
        commit: TableCommit,
        *,
        rng: random.Random,
    ) -> None:
        """Resolve a crew station / command verb through the custom-beat seam.

        Fails loud on an unrecognized verb (No Silent Fallbacks). Every resolved
        action emits a ``table.commit`` span; command verbs additionally drive the
        ``command_points.*`` / ``crisis.*`` span families — the GM panel's proof the
        cooperative round actually fired, not improvised prose.
        """
        verb = commit.beat_id
        if verb in WAR_RIG_STATION_VERBS:
            pass  # station verb — table.commit below is the whole resolution
        elif verb in WAR_RIG_COMMAND_VERBS:
            pool = self._shared_cp_pool(state)
            spend_command_points(pool, verb, seat=seat.seat_id, rng=rng)
        elif verb == WAR_RIG_DEAL_WITH_CRISIS:
            vessel_id = self._vessel_id(state)
            # Pass the crew's SHARED Hull so a failed continuing crisis actually
            # escalates into 86-2's two-pool damage model in a live round (AC2) —
            # not just a span. ability_mod=0 (character-stat binding is 86-5).
            hull = self._shared_hull(state)
            rolled = roll_crisis(rng, vessel_id=vessel_id)
            deal_with_crisis(
                rolled.entry,
                seat=seat.seat_id,
                ability_mod=0,
                rng=rng,
                vessel_id=vessel_id,
                hull=hull,
            )
        else:
            raise ValueError(
                f"war_rig_crew has no station verb {verb!r} for seat {seat.seat_id!r} "
                f"(known verbs: {sorted(WAR_RIG_STATION_VERBS | WAR_RIG_COMMAND_VERBS | {WAR_RIG_DEAL_WITH_CRISIS})})"
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
    "WAR_RIG_COMMAND_VERBS",
    "WAR_RIG_DEAL_WITH_CRISIS",
    "WAR_RIG_STATIONS",
    "WAR_RIG_STATION_VERBS",
    "WarRigCrewTableGame",
]
