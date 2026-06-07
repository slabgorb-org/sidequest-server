"""Shared HP-depletion win check (ADR-114).

Callable from the beat loop and the dogfight sealed-letter shot path.
UNCONDITIONAL — the caller decides WHEN to run it; this only reads HP and
resolves. Emits ``encounter.resolved`` with ``source='hp_depletion'`` so the
GM panel can distinguish an HP kill from a dial victory (the lie detector).

Mutual KO -> ``mutual_destruction`` outcome (a beat-loop fight can't drop both
sides in one call, but a dogfight shot can).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sidequest.game.encounter import EncounterPhase, StructuredEncounter
from sidequest.telemetry.spans.encounter import (
    encounter_resolved_span,
    win_condition_evaluated_span,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HpDepletionResult:
    outcome: str  # player_victory | opponent_victory | mutual_destruction
    down_side: str  # opponent | player | both


def check_hp_depletion(
    enc: StructuredEncounter,
    edge_resolver: Callable[[str], Any | None],
    **extra_span_attrs: Any,
) -> HpDepletionResult | None:
    """Resolve *enc* if a side has no standing combatant left (all at 0 HP).

    Side-defeat semantics per ADR-139 Invariant 1 (win-condition liveness):
    a side is down only when EVERY non-withdrawn seated actor with a
    resolvable core sits at 0 HP. One downed PC is NOT party defeat while a
    party-mate fights on — sq-playtest 2026-06-07 (perseus_cloud MP): a
    pre-existing 0-HP seat's first beat commit auto-resolved
    ``opponent_victory`` against a party whose other PC was actively winning.

    Returns the :class:`HpDepletionResult`, or ``None`` if no side is fully
    down or the encounter is already resolved. Mutates ``enc.resolved``,
    ``enc.outcome``, and ``enc.structured_phase`` when it resolves, and emits
    the ``encounter.resolved`` OTEL span with ``source='hp_depletion'``.
    Whenever ANY seated actor sits at 0 HP it also emits
    ``confrontation.win_condition_evaluated`` (resolve or not) so the GM
    panel can see the liveness decision itself.

    ``extra_span_attrs`` are forwarded onto :func:`encounter_resolved_span` so
    the beat-loop call site can pass ``beat_id`` and preserve the existing span
    attribute contract.
    """
    if enc.resolved:
        return None

    down_names: list[str] = []
    standing_names: list[str] = []

    def _side_down(side: str) -> bool:
        """True iff the side has at least one resolvable core and ALL are at 0 HP."""
        any_core = False
        all_down = True
        for a in enc.actors:
            if a.side != side or a.withdrawn:
                continue
            core = edge_resolver(a.name)
            if core is None:
                continue
            any_core = True
            if core.hp.current <= 0:
                down_names.append(a.name)
            else:
                standing_names.append(a.name)
                all_down = False
        return any_core and all_down

    player_down = _side_down("player")
    opponent_down = _side_down("opponent")
    if not down_names:
        return None

    if not player_down and not opponent_down:
        # Somebody is at 0 HP but their side still has a standing combatant —
        # the fight goes on (ADR-139: one downed PC ≠ party defeat). Loud,
        # observable non-resolution: span + log so forensics can distinguish
        # this branch from the check never running.
        logger.info(
            "hp_depletion.partial_down encounter=%s down=%s standing=%s extra=%s",
            enc.encounter_type,
            down_names,
            standing_names,
            extra_span_attrs or {},
        )
        with win_condition_evaluated_span(
            win_condition="hp_depletion",
            terminal_reached=False,
            outcome="",
            down_actors=",".join(down_names),
            standing_actors=",".join(standing_names),
            **extra_span_attrs,
        ):
            pass
        return None

    if player_down and opponent_down:
        outcome, down_side = "mutual_destruction", "both"
    elif opponent_down:
        outcome, down_side = "player_victory", "opponent"
    else:
        outcome, down_side = "opponent_victory", "player"

    enc.resolved = True
    enc.outcome = outcome
    enc.structured_phase = EncounterPhase.Resolution
    # Text-log forensics line (sq-playtest 2026-06-07 silent death-spiral): the
    # span below is Jaeger-only — a log grep on the dead session must be able to
    # see WHO resolved the fight and WHY without a trace store.
    logger.info(
        "hp_depletion.resolved encounter=%s outcome=%s down_side=%s extra=%s",
        enc.encounter_type,
        outcome,
        down_side,
        extra_span_attrs or {},
    )
    with win_condition_evaluated_span(
        win_condition="hp_depletion",
        terminal_reached=True,
        outcome=outcome,
        down_actors=",".join(down_names),
        standing_actors=",".join(standing_names),
        **extra_span_attrs,
    ):
        pass
    with encounter_resolved_span(
        encounter_type=enc.encounter_type,
        outcome=outcome,
        source="hp_depletion",
        down_side=down_side,
        **extra_span_attrs,
    ):
        pass
    return HpDepletionResult(outcome=outcome, down_side=down_side)
