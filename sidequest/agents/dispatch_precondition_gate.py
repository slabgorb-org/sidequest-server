"""Pre-narrator precondition gate — drop structurally-inert dispatches
(Story 59-8, ADR-113).

Some subsystem dispatches CANNOT engage on a given snapshot no matter what the
narrator does, because a world-level precondition is unmet. The clearest case
(playtest 59-8, Glenross): the Intent Router routes investigative actions to
``scenario_clue``, but Glenross ships no ADR-053 scenario graph, so
``snapshot.scenario_state is None`` — ``consume_clue_footnotes`` no-ops and the
post-turn dispatch-engagement watcher
(:mod:`sidequest.agents.dispatch_engagement_watcher`) reports a GUARANTEED
``dispatch_engagement.scenario_clue.mismatch`` on every investigative turn.

That mismatch is a TRUE positive (the clue had zero mechanical backing) but an
UNAVOIDABLE one: there is no authored clue graph to engage and re-running the
dispatch can never change the outcome. Rather than silence the watcher (which
would blind it to genuine mismatches in real scenario worlds), this gate
removes the structurally-inert dispatch from the ``DispatchPackage`` BEFORE the
dispatch bank runs and BEFORE the watcher reads it — and emits a loud
``intent_router.dispatch.gated`` OTEL span per drop so the GM panel sees the
skip (NOT a silent fallback, per CLAUDE.md "No Silent Fallbacks").

The gate fires ONLY when the precondition is structurally unmet. In a real
ADR-053 scenario world (``scenario_state`` present) the scenario_clue dispatch
passes through untouched and the watcher's genuine-mismatch detection is
unaffected.

Shape mirrors the sibling watcher: a pure decision
(:func:`gate_inert_dispatches`, no OTEL) plus a thin span-emitting wrapper
(:func:`run_dispatch_precondition_gate`). The pre-narrator pass
(``execute_intent_router_pre_narrator_pass``) calls the wrapper between
``decompose`` and ``run_dispatch_bank``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from opentelemetry import trace

from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import DispatchPackage, SubsystemDispatch
from sidequest.telemetry.spans.intent_router import (
    intent_router_dispatch_gated_span,
    intent_router_dispatch_unregistered_span,
)


@dataclass(frozen=True)
class GatedDispatch:
    """One dispatch the gate dropped, with the precondition that was unmet.

    Carries enough for the wrapper to emit a useful span AND for callers that
    consume the pure function directly to introspect the decision.
    """

    subsystem: str
    idempotency_key: str
    reason: str


# ---------------------------------------------------------------------------
# Per-subsystem structural preconditions
#
# Each predicate answers: on this snapshot, is the subsystem STRUCTURALLY inert
# — i.e. can it never engage regardless of narrator behaviour? Returns a short
# reason string when inert (shown on the gated span), else ``None``. A
# subsystem absent from the map is never gated.
# ---------------------------------------------------------------------------


def _scenario_clue_precondition_unmet(snapshot: GameSnapshot) -> str | None:
    if snapshot.scenario_state is None:
        return "snapshot.scenario_state is None (world ships no ADR-053 scenario graph)"
    return None


_INERT_PRECONDITIONS: dict[str, Callable[[GameSnapshot], str | None]] = {
    "scenario_clue": _scenario_clue_precondition_unmet,
}


def gate_inert_dispatches(
    *,
    package: DispatchPackage,
    snapshot: GameSnapshot,
) -> tuple[DispatchPackage, list[GatedDispatch]]:
    """Return ``(filtered_package, gated)`` without touching OTEL.

    Drops every dispatch whose subsystem has an :data:`_INERT_PRECONDITIONS`
    predicate that reports the precondition unmet on ``snapshot``. When nothing
    is gated the original ``package`` is returned unchanged (no copy).
    Uniqueness of idempotency keys is preserved (the gate only removes keys),
    so the package-level validator's invariant still holds.
    """
    gated: list[GatedDispatch] = []

    def _keep(dispatch: SubsystemDispatch) -> bool:
        predicate = _INERT_PRECONDITIONS.get(dispatch.subsystem)
        if predicate is None:
            return True
        reason = predicate(snapshot)
        if reason is None:
            return True
        gated.append(
            GatedDispatch(
                subsystem=dispatch.subsystem,
                idempotency_key=dispatch.idempotency_key,
                reason=reason,
            )
        )
        return False

    new_per_player = [
        pd.model_copy(update={"dispatch": [d for d in pd.dispatch if _keep(d)]})
        for pd in package.per_player
    ]
    new_cross_player = [
        ca.model_copy(update={"dispatch": [d for d in ca.dispatch if _keep(d)]})
        for ca in package.cross_player
    ]

    if not gated:
        return package, []

    filtered = package.model_copy(
        update={"per_player": new_per_player, "cross_player": new_cross_player}
    )
    return filtered, gated


def run_dispatch_precondition_gate(
    *,
    package: DispatchPackage,
    snapshot: GameSnapshot,
    tracer: trace.Tracer | None = None,
) -> DispatchPackage:
    """Gate the package and emit one ``intent_router.dispatch.gated`` span per drop.

    Returns the filtered package for the caller to feed to both the dispatch
    bank and (via ``turn_context.dispatch_package``) the post-turn watcher.
    """
    filtered, gated = gate_inert_dispatches(package=package, snapshot=snapshot)
    for g in gated:
        with intent_router_dispatch_gated_span(
            subsystem=g.subsystem,
            idempotency_key=g.idempotency_key,
            reason=g.reason,
            _tracer=tracer,
        ):
            pass
    return filtered


# ---------------------------------------------------------------------------
# Unregistered-subsystem gate (Story 71-27)
#
# The Intent Router emits ``subsystem`` as a free string (the protocol layer is
# deliberately permissive — runtime registration is the authority, and tests
# construct dispatches with arbitrary placeholder names). When the router names
# a subsystem with no registered handler — the canonical case is ``combat``,
# which is a confrontation *type* (``params["type"]``) routed through the
# ``confrontation`` subsystem, NOT a subsystem key — the dispatch can never
# engage. The dispatch bank already drops it, but only AFTER it has polluted
# ``turn_context.dispatch_package`` (read by narrator redaction and the
# post-turn watcher). This gate removes it in the pre-narrator pass, before the
# bank, and emits a loud ``intent_router.dispatch.unregistered`` span — the
# "stop emitting" half of Story 71-27 (registering a ``combat`` handler would
# be a stub for a non-subsystem; CLAUDE.md "No Stubbing").
#
# The registered-name set is INJECTED by the caller (the pass) rather than
# imported here, so this module stays a pure, snapshot/registry-free decision
# layer that tests can drive with an explicit vocabulary.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnregisteredDispatch:
    """One dispatch the gate dropped because its subsystem has no handler."""

    subsystem: str
    idempotency_key: str


def gate_unregistered_subsystems(
    *,
    package: DispatchPackage,
    registered: Collection[str],
) -> tuple[DispatchPackage, list[UnregisteredDispatch]]:
    """Return ``(filtered_package, dropped)`` without touching OTEL.

    Drops every dispatch whose ``subsystem`` is absent from ``registered`` (the
    live dispatch-bank registry keys). When nothing is dropped the original
    ``package`` is returned unchanged (no copy). Uniqueness of idempotency keys
    is preserved (the gate only removes keys), so the package-level validator's
    invariant still holds.
    """
    dropped: list[UnregisteredDispatch] = []

    def _keep(dispatch: SubsystemDispatch) -> bool:
        if dispatch.subsystem in registered:
            return True
        dropped.append(
            UnregisteredDispatch(
                subsystem=dispatch.subsystem,
                idempotency_key=dispatch.idempotency_key,
            )
        )
        return False

    new_per_player = [
        pd.model_copy(update={"dispatch": [d for d in pd.dispatch if _keep(d)]})
        for pd in package.per_player
    ]
    new_cross_player = [
        ca.model_copy(update={"dispatch": [d for d in ca.dispatch if _keep(d)]})
        for ca in package.cross_player
    ]

    if not dropped:
        return package, []

    filtered = package.model_copy(
        update={"per_player": new_per_player, "cross_player": new_cross_player}
    )
    return filtered, dropped


def run_unregistered_subsystem_gate(
    *,
    package: DispatchPackage,
    registered: Collection[str],
    tracer: trace.Tracer | None = None,
) -> DispatchPackage:
    """Gate the package and emit one ``intent_router.dispatch.unregistered``
    span per dropped dispatch.

    Returns the filtered package for the caller to feed to both the dispatch
    bank and (via ``turn_context.dispatch_package``) the post-turn watcher.
    """
    filtered, dropped = gate_unregistered_subsystems(package=package, registered=registered)
    for d in dropped:
        with intent_router_dispatch_unregistered_span(
            subsystem=d.subsystem,
            idempotency_key=d.idempotency_key,
            _tracer=tracer,
        ):
            pass
    return filtered


__all__ = [
    "GatedDispatch",
    "UnregisteredDispatch",
    "gate_inert_dispatches",
    "gate_unregistered_subsystems",
    "run_dispatch_precondition_gate",
    "run_unregistered_subsystem_gate",
]
