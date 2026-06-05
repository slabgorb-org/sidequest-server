"""Engine lull-escalation selector (Story 77-7, ADR-024/025/128).

When the game lulls — ``TensionTracker.boring_streak >= the genre's
escalation_streak`` — the engine should PUSH rather than wait: select one seed
from the ADR-128 deck (or draw one if none are active) and FIRE it, turning its
``narrative_hint`` into the concrete escalation directive for the NEXT narrator
turn (REPLACING the generic "environment shifts" ``escalation_beat`` that
``pacing_hint`` would otherwise emit).

This is the reuse-first v1 slice (NO new ADR): it wires the existing lull SIGNAL
(``tension_tracker.pacing_hint``) to the existing Bang CATALOG (``seed_deck`` /
``seed_tick``), governed by the ADR-128 ``FIRE_COOLDOWN_TURNS`` cap and proved by
the routed ``SPAN_LULL_ESCALATION`` (the GM-panel lie detector for "engine
pushed" vs "narrator improvised").

The producer call site is peer to ``tick_seeds`` in the turn handler; the fired
directive is stored on ``snapshot.pending_escalation_directive`` and consumed by
``_build_turn_context`` on the next turn. Selection is deterministic and
resume-safe (SHA-256 over session_id + turn + the *sorted* candidate ids — no
``random`` / wallclock and order-independent), so a resume re-fires identically.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sidequest.game.seed_tick import draw_engaged_seed
from sidequest.game.session import GameSnapshot
from sidequest.game.tension_tracker import TensionTracker
from sidequest.game.trope_tuning import FIRE_COOLDOWN_TURNS
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.telemetry.spans import SPAN_SEED_FIRED, Span
from sidequest.telemetry.spans.pacing import lull_escalation_span

_LULL_ENGAGEMENT_SIGNAL = "lull_escalation"


@dataclass(frozen=True)
class LullEscalationResult:
    """Outcome of one lull-escalation step.

    ``reason`` is ``"not_triggered"`` (below threshold, no-op, no span),
    ``"fired"``, ``"cooldown"``, or ``"none_available"``. ``selected_seed_id``
    and ``directive`` are populated only on ``"fired"``.
    """

    fired: bool
    selected_seed_id: str | None
    reason: str
    directive: str | None


def _select_seed_id(session_id: str, now_turn: int, candidate_ids: list[str]) -> str:
    """Deterministic, resume-safe, order-independent pick from ``candidate_ids``.

    Hashes ``(session_id, turn, sorted ids)`` via SHA-256 (the ``seed_deck``
    reproducibility pattern) — no ``random``/wallclock, and sorting the ids first
    makes the choice independent of ``active_seeds`` list order, so a save/load
    reorder re-fires the same seed.
    """
    ordered = sorted(candidate_ids)
    key = f"{session_id}|{now_turn}|{','.join(ordered)}".encode()
    index = int.from_bytes(hashlib.sha256(key).digest(), "big") % len(ordered)
    return ordered[index]


def _narrative_hint_for(pack: Any, seed_id: str) -> str:
    for trope in getattr(pack, "seed_tropes", []) or []:
        if trope.id == seed_id:
            return trope.narrative_hint
    return ""


def apply_lull_escalation(
    snapshot: GameSnapshot,
    pack: Any,
    *,
    tracker: TensionTracker,
    thresholds: DramaThresholds,
    session_id: str,
    now_turn: int,
) -> LullEscalationResult:
    """Fire a seed as the next turn's escalation directive when the game lulls.

    Runs peer to ``tick_seeds`` at the end of a turn. Below the escalation
    threshold it is a no-op (no span). When engaged it respects the ADR-128
    cooldown, selects (or draws) one seed, stores its ``narrative_hint`` on
    ``snapshot.pending_escalation_directive`` for the next turn, and emits
    ``SPAN_LULL_ESCALATION`` (plus ``SPAN_SEED_FIRED`` for the fired seed).
    """
    hint = tracker.pacing_hint(thresholds)
    # AC1: ride the live ADR-024 signal — below the escalation threshold, no-op.
    if hint.escalation_beat is None:
        return LullEscalationResult(
            fired=False, selected_seed_id=None, reason="not_triggered", directive=None
        )

    boring_streak = tracker.boring_streak()
    drama_weight = hint.drama_weight

    # AC3: ADR-128 governor — never fire two turns running.
    last = snapshot.last_lull_fire_turn
    if last is not None and now_turn - last < FIRE_COOLDOWN_TURNS:
        with lull_escalation_span(
            boring_streak=boring_streak,
            drama_weight=drama_weight,
            fired=False,
            selected_seed_id="",
            reason="cooldown",
        ):
            pass
        return LullEscalationResult(
            fired=False, selected_seed_id=None, reason="cooldown", directive=None
        )

    # AC2: select an active seed; if none are active, draw one first.
    if not snapshot.active_seeds:
        draw_engaged_seed(
            snapshot,
            pack,
            session_id=session_id,
            engagement_signal=_LULL_ENGAGEMENT_SIGNAL,
            now_turn=now_turn,
        )
    if not snapshot.active_seeds:
        with lull_escalation_span(
            boring_streak=boring_streak,
            drama_weight=drama_weight,
            fired=False,
            selected_seed_id="",
            reason="none_available",
        ):
            pass
        return LullEscalationResult(
            fired=False, selected_seed_id=None, reason="none_available", directive=None
        )

    seed_id = _select_seed_id(session_id, now_turn, [s.id for s in snapshot.active_seeds])
    directive = _narrative_hint_for(pack, seed_id)

    # AC2/AC4: fire — store the directive for the next turn, mark the seed fired,
    # arm the cooldown. SPAN_SEED_FIRED gains its first real engine consumer here.
    snapshot.pending_escalation_directive = directive
    snapshot.last_lull_fire_turn = now_turn
    with Span.open(SPAN_SEED_FIRED, {"seed_id": seed_id}):
        pass
    with lull_escalation_span(
        boring_streak=boring_streak,
        drama_weight=drama_weight,
        fired=True,
        selected_seed_id=seed_id,
        reason="fired",
    ):
        pass
    return LullEscalationResult(
        fired=True, selected_seed_id=seed_id, reason="fired", directive=directive
    )
