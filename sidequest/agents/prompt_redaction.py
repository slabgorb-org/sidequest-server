"""prompt_redaction — Visibility filtering for the narrator prompt.

Called by the orchestrator before narrator prompt assembly to strip
dispatches and directives tagged with ``redact_from_narrator_canonical=True``.
Structural hiding is the primary defense: the narrator cannot leak what
it was never told.

Live on the narrator prompt path since ADR-113 router revival (story 59-4).
"""

from __future__ import annotations

from opentelemetry import trace

from sidequest.protocol.dispatch import (
    CrossAction,
    DispatchPackage,
    LethalityVerdict,
    NarratorDirective,
    PlayerDispatch,
    SubsystemDispatch,
)

_tracer = trace.get_tracer("sidequest.prompt_redaction")


def redact_dispatch_package(
    pkg: DispatchPackage,
) -> tuple[DispatchPackage, list[SubsystemDispatch | NarratorDirective | LethalityVerdict]]:
    """Return (pkg_without_redacted_entries, list_of_removed_entries).

    Called by the narrator before prompt assembly. Removed entries are
    returned so the caller can route them to SECRET_NOTE channels (Task 6).
    """
    removed: list[SubsystemDispatch | NarratorDirective | LethalityVerdict] = []
    new_players: list[PlayerDispatch] = []

    for pd in pkg.per_player:
        kept_dispatch = []
        for d in pd.dispatch:
            if d.visibility.redact_from_narrator_canonical:
                removed.append(d)
            else:
                kept_dispatch.append(d)
        kept_directives = []
        for n in pd.narrator_instructions:
            if n.visibility.redact_from_narrator_canonical:
                removed.append(n)
            else:
                kept_directives.append(n)
        # LethalityVerdict does not carry a VisibilityTag in the current
        # protocol shape — the decomposer spec has it emitting via the
        # sibling SubsystemDispatch. If that changes, add a branch here.
        new_players.append(
            pd.model_copy(
                update={
                    "dispatch": kept_dispatch,
                    "narrator_instructions": kept_directives,
                }
            )
        )

    # cross_player carries the same redactable SubsystemDispatch entries (a
    # shared-target MP interaction can be sealed from the narrator), but
    # CrossAction has no narrator_instructions field — so filter `dispatch`
    # only. Removals land in the SAME `removed` accumulator the span below
    # reports, matching every other cross_player consumer (run_dispatch_bank,
    # the engagement watcher, the idempotency validator). Story 59-9.
    new_cross: list[CrossAction] = []
    for ca in pkg.cross_player:
        kept_dispatch = []
        for d in ca.dispatch:
            if d.visibility.redact_from_narrator_canonical:
                removed.append(d)
            else:
                kept_dispatch.append(d)
        new_cross.append(ca.model_copy(update={"dispatch": kept_dispatch}))

    if removed:
        with _tracer.start_as_current_span("prompt.redaction.structural") as span:
            span.set_attribute("turn_id", pkg.turn_id)
            span.set_attribute("redacted_count", len(removed))
            span.set_attribute(
                "redacted_kinds",
                [type(r).__name__ for r in removed],
            )
            span.set_attribute(
                "redacted_idempotency_keys",
                [r.idempotency_key for r in removed if isinstance(r, SubsystemDispatch)],
            )

    redacted_pkg = pkg.model_copy(update={"per_player": new_players, "cross_player": new_cross})
    return redacted_pkg, removed
