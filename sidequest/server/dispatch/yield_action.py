"""YIELD dispatch handler — structured player exit.

Spec 2026-04-25-dual-track-momentum-design.md §Yield action. The yielding
actor is marked ``withdrawn``; the encounter resolves when every
``side="player"`` actor has yielded or been taken out. Edge is refunded by
``1 + count_of_scratch-or-worse-statuses-created-this-encounter``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sidequest.game.encounter_classifier import yield_side_for
from sidequest.game.resolution_signal import ResolutionSignal
from sidequest.game.session import GameSnapshot
from sidequest.game.status import Status
from sidequest.telemetry.spans import (
    encounter_resolution_signal_emitted_span,
    encounter_yield_received_span,
    encounter_yield_resolved_span,
)
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

if TYPE_CHECKING:
    from sidequest.server.session_room import SessionRoom


def _statuses_taken_in_encounter(
    statuses: list[Status],
    encounter_type: str,
) -> int:
    return sum(1 for s in statuses if s.created_in_encounter == encounter_type)


def _refund_edge_for_yielders(
    snapshot: GameSnapshot,
    yielded_names: list[str],
    encounter_type: str,
) -> int:
    total_refund = 0
    for name in yielded_names:
        char = next((c for c in snapshot.characters if c.core.name == name), None)
        if char is None:
            continue
        count = _statuses_taken_in_encounter(char.core.statuses, encounter_type)
        refund = 1 + count
        before = char.core.hp.current
        char.core.apply_hp_delta(refund)
        total_refund += char.core.hp.current - before
    return total_refund


def handle_yield(
    snapshot: GameSnapshot,
    *,
    room: SessionRoom,
    player_id: str,
    player_name: str,
) -> None:
    enc = snapshot.encounter
    if enc is None or enc.resolved:
        raise ValueError("YIELD: no active encounter")
    actor = enc.find_actor_for_player(player_name)
    if actor is None:
        raise ValueError(f"YIELD: no player-side actor named {player_name!r}")
    if actor.withdrawn:
        return  # idempotent

    actor_char = next(
        (c for c in snapshot.characters if c.core.name == player_name),
        None,
    )
    statuses_taken = (
        _statuses_taken_in_encounter(actor_char.core.statuses, enc.encounter_type)
        if actor_char
        else 0
    )
    with encounter_yield_received_span(
        player_id=player_id,
        actor_name=player_name,
        prior_player_metric=enc.player_metric.current,
        prior_opponent_metric=enc.opponent_metric.current,
        statuses_taken_this_encounter=statuses_taken,
    ):
        pass
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "yield_received",
            "encounter_type": enc.encounter_type,
            "player_id": player_id,
            "actor_name": player_name,
            "prior_player_metric": enc.player_metric.current,
            "prior_opponent_metric": enc.opponent_metric.current,
            "statuses_taken_this_encounter": statuses_taken,
        },
        component="encounter",
    )

    actor.withdrawn = True

    # The "is the table done acting?" check counts SEATED PLAYER CHARACTERS
    # only — actor names that appear in ``snapshot.player_seats`` values.
    # NPC allies on the player side (companions / hirelings recruited via
    # the recruiter pipeline; their beats come from the narrator's
    # ``encounter.agent_beat_selection``, not a player click) must NOT
    # block yield resolution. In solo, when Carl yields, Donut goes with
    # him — there's nobody to click "yield" for Donut and the encounter
    # would deadlock at ``remaining_player_actors=1`` forever.
    #
    # Playtest 2026-05-06 (sumpdrake fight in caverns_sunden Grimvault):
    # Carl yielded → encounter persisted because Donut (recruited NPC
    # hireling) was still on the player-side actor list with
    # ``withdrawn=False``. The UI re-presented the action surface but
    # nothing advanced — soft-lock requiring the player to click another
    # action to escape. Filtering by the seat manifest here makes solo
    # yield a deterministic resolver and preserves multiplayer semantics
    # (every seated PC must commit; companions follow their patron).
    seated_pc_names: set[str] = set(snapshot.player_seats.values())
    all_player_actors = [a for a in enc.actors if a.side == "player"]
    if not seated_pc_names:
        # Pre-MP saves (or solo without a populated seat map) treat every
        # player-side actor as a seated PC — back-compat with the legacy
        # path. Companions only exist post-recruiter; pre-recruiter solo
        # had no NPC allies on the player side, so this fallback is the
        # historical behavior. The seat-manifest path is the new norm.
        seated_player_actors = list(all_player_actors)
        companion_actor_names: list[str] = []
    else:
        seated_player_actors = [a for a in all_player_actors if a.name in seated_pc_names]
        companion_actor_names = [a.name for a in all_player_actors if a.name not in seated_pc_names]

    all_done = all(a.withdrawn for a in seated_player_actors)
    # GM-panel visibility for the seat-aware yield gate. Without this
    # span, the GM can't tell whether the encounter persisted because
    # (a) the yield count math is wrong again or (b) a real other PC
    # still has to commit. Surface the seat manifest, the seated-PC
    # actor names, and the all_done decision.
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "yield_seat_gate",
            "encounter_type": enc.encounter_type,
            "yielded_actor": player_name,
            "seated_pc_count": len(seated_player_actors),
            "seated_pcs_remaining": [a.name for a in seated_player_actors if not a.withdrawn],
            "all_seated_pcs_withdrawn": all_done,
            "companion_count_on_player_side": len(companion_actor_names),
            "companions_excluded_from_gate": companion_actor_names,
        },
        component="encounter",
    )
    if not all_done:
        return  # encounter remains active — another seated PC still has to act

    # ``yielded_names`` carries every player-side actor that ended up
    # withdrawn (seated PCs who explicitly yielded plus any companions
    # whose actor was already withdrawn from a prior beat). The edge
    # refund downstream walks ``snapshot.characters`` and only refunds
    # actors that have a Character record — companions don't, so the
    # refund quietly skips them. That preserves the spec semantics:
    # only a yielding seated PC banks the edge refund.
    player_actors = [a for a in enc.actors if a.side == "player"]
    yielded_names = [a.name for a in player_actors if a.withdrawn]
    edge_refreshed = _refund_edge_for_yielders(
        snapshot,
        yielded_names,
        enc.encounter_type,
    )

    enc.resolved = True
    enc.outcome = "yielded"

    # Yielded out of the encounter — scene ended for the party. Sweep
    # Scratch (Playtest 2026-04-26 Bug #1). Wound/Scar persist; this is
    # the same trigger as narrator-beat / dice-beat resolution.
    room.session.end_scene(
        "scene_end",
        turn=snapshot.turn_manager.interaction,
    )

    # Story 59-33: derive yield_side from the outcome (never hand-set) →
    # "player" for a player yield (a LOSS — orthogonal to victory credit).
    yield_side = yield_side_for("yielded")
    snapshot.pending_resolution_signal = ResolutionSignal(
        encounter_type=enc.encounter_type,
        outcome="yielded",
        final_player_metric=enc.player_metric.current,
        final_opponent_metric=enc.opponent_metric.current,
        yielded_actors=tuple(yielded_names),
        edge_refreshed=edge_refreshed,
        yield_side=yield_side,
    )

    with encounter_yield_resolved_span(
        outcome="yielded",
        yielded_actors=tuple(yielded_names),
        edge_refreshed=edge_refreshed,
    ):
        pass
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "yield_resolved",
            "encounter_type": enc.encounter_type,
            "outcome": "yielded",
            "yielded_actors": list(yielded_names),
            "edge_refreshed": edge_refreshed,
        },
        component="encounter",
    )
    with encounter_resolution_signal_emitted_span(
        outcome="yielded",
        final_player_metric=enc.player_metric.current,
        final_opponent_metric=enc.opponent_metric.current,
        yield_side=yield_side,
    ):
        pass
    _watcher_publish(
        "state_transition",
        {
            "field": "encounter",
            "op": "resolved",
            "encounter_type": enc.encounter_type,
            "outcome": "yielded",
            "source": "yield",
            "final_player_metric": enc.player_metric.current,
            "final_opponent_metric": enc.opponent_metric.current,
        },
        component="encounter",
    )
