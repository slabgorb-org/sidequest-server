"""Scenario binding — called from chargen confirmation (Story 2.3 Slice D).

Port of the scenario-initialization block in
``sidequest-api/crates/sidequest-server/src/dispatch/connect.rs``
(lines ~1948-2023). When the genre pack declares at least one
scenario, pick the first one (future: player/DM selection), bind it
to a :class:`ScenarioState`, and seed every matching in-snapshot NPC's
:class:`BeliefState` from the pack's ``initial_beliefs``.

The Rust implementation also stashes the pack clone on the
shared-session holder (``active_scenario``) for cross-player pressure-
event / scene-budget visibility. Python's single-player Phase 1 has no
shared-session analog yet, so this port returns the bound pack to the
caller, which stashes it on the connection-scoped ``_SessionData``.

Failure modes are loud:

- No scenarios in pack → return ``None`` silently (pack isn't using
  the system; not a misconfiguration).
- ``ScenarioPack`` present but malformed → the pydantic model raises
  at pack-load time; binding is a downstream no-op.
"""

from __future__ import annotations

import logging
import random

from opentelemetry import trace

from sidequest.game.belief_state import (
    BeliefFact,
    BeliefSourceInferred,
    BeliefSourceWitnessed,
    BeliefSuspicion,
)
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.scenario import ScenarioPack

logger = logging.getLogger(__name__)


def bind_scenario(
    pack: GenrePack,
    snapshot: GameSnapshot,
    *,
    genre_slug: str,
    world_slug: str,
    rng: random.Random | None = None,
) -> tuple[str, ScenarioPack] | None:
    """Bind the first scenario in ``pack`` to ``snapshot``.

    Mutates ``snapshot`` in place: sets ``snapshot.scenario_state`` and
    seeds matching NPCs' ``belief_state`` with facts/suspicions from
    the scenario's ``ScenarioNpc.initial_beliefs``.

    Returns ``(scenario_id, scenario_pack)`` so the caller can stash
    the chosen pack on its session-scoped state (Rust's
    ``shared_session.active_scenario`` analog). Returns ``None`` when
    the pack declares no scenarios.

    ``rng`` is forwarded to :meth:`ScenarioState.from_genre_pack` for
    deterministic guilty-NPC selection in tests.
    """
    if not pack.scenarios:
        return None

    # Pick the first scenario (dict insertion order is deterministic
    # in Python 3.7+; for now "first" = YAML load order).
    scenario_id, scenario_pack = next(iter(pack.scenarios.items()))

    scenario_state = ScenarioState.from_genre_pack(scenario_pack, rng=rng)

    # Seed scenario NPC belief states from pack data. Matched by name
    # (Rust parity: ``n.core.name.as_str() == snpc.name``).
    for snpc in scenario_pack.npcs:
        target = next((n for n in snapshot.npcs if n.core.name == snpc.name), None)
        if target is None:
            continue
        for fact in snpc.initial_beliefs.facts:
            target.belief_state.add_belief(
                BeliefFact(
                    subject=snpc.name,
                    content=fact,
                    turn_learned=0,
                    source=BeliefSourceWitnessed(),
                )
            )
        for suspicion in snpc.initial_beliefs.suspicions:
            target.belief_state.add_belief(
                BeliefSuspicion.make(
                    subject=suspicion.target,
                    content=suspicion.basis,
                    turn_learned=0,
                    source=BeliefSourceInferred(),
                    confidence=suspicion.confidence,
                )
            )

    snapshot.scenario_state = scenario_state

    span = trace.get_current_span()
    span.add_event(
        "scenario.initialized",
        {
            "event": "scenario_initialized",
            "genre": genre_slug,
            "world": world_slug,
            "scenario_id": scenario_id,
            "guilty_npc": scenario_state.guilty_npc,
            "npc_roles": len(scenario_state.npc_roles),
        },
    )
    logger.info(
        "scenario.initialized genre=%s world=%s scenario=%s guilty=%s roles=%d",
        genre_slug,
        world_slug,
        scenario_id,
        scenario_state.guilty_npc,
        len(scenario_state.npc_roles),
    )

    return scenario_id, scenario_pack


__all__ = ["bind_scenario"]
