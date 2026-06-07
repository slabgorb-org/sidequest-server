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
from sidequest.telemetry.spans.encounter import encounter_resolved_span

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
    """Resolve *enc* if a side's combatant is at 0 HP.

    Returns the :class:`HpDepletionResult`, or ``None`` if nobody is down or
    the encounter is already resolved. Mutates ``enc.resolved``,
    ``enc.outcome``, and ``enc.structured_phase`` when it resolves, and emits
    the ``encounter.resolved`` OTEL span with ``source='hp_depletion'``.

    ``extra_span_attrs`` are forwarded onto :func:`encounter_resolved_span` so
    the beat-loop call site can pass ``beat_id`` and preserve the existing span
    attribute contract.
    """
    if enc.resolved:
        return None

    def _any_down(side: str) -> bool:
        for a in enc.actors:
            if a.side != side:
                continue
            core = edge_resolver(a.name)
            if core is not None and core.hp.current <= 0:
                return True
        return False

    player_down = _any_down("player")
    opponent_down = _any_down("opponent")
    if not player_down and not opponent_down:
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
    with encounter_resolved_span(
        encounter_type=enc.encounter_type,
        outcome=outcome,
        source="hp_depletion",
        down_side=down_side,
        **extra_span_attrs,
    ):
        pass
    return HpDepletionResult(outcome=outcome, down_side=down_side)
