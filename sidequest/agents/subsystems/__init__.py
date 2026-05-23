"""__init__ — DORMANT.

This module is not invoked on the live turn path as of 2026-04-28
(see docs/superpowers/specs/2026-04-28-localdm-offline-only-design.md).

It is preserved for two consumers:
  1. The offline LocalDM corpus runner (follow-up story).
  2. Re-engagement on the live path once ADR-073's local fine-tuned
     router replaces the Haiku CLI subprocess.

Unit tests for this module remain in `just check-all` so it does not
bit-rot. If you find yourself adding a live caller, you are landing
ADR-073 (or undoing this design); update both ends.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sidequest.protocol.dispatch import (
    DispatchPackage,
    NarratorDirective,
    SubsystemDispatch,
)
from sidequest.telemetry.spans import (
    intent_router_dispatch_bank_span,
    intent_router_subsystem_span,
)

logger = logging.getLogger(__name__)

SubsystemCallable = Callable[..., Awaitable["SubsystemOutput"]]


def _filter_context_for_callable(fn: SubsystemCallable, context: dict[str, Any]) -> dict[str, Any]:
    """Return only the ``context`` keys that ``fn`` actually accepts.

    Subsystems have heterogeneous signatures — ``run_npc_agency`` requires
    ``npc_pool`` (kw-only; rewired from ``npc_registry`` in story 45-52),
    ``run_distinctive_detail`` takes only the ``dispatch``. Blasting
    ``**context`` into either raises TypeError: the pool-required subsystem
    fails on missing kwarg if context is empty, and the dispatch-only
    subsystem fails on unexpected kwarg if context is full. Filtering by
    signature keeps both happy.

    If ``fn`` declares ``**kwargs``, the full context is forwarded.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(context)
    accepts_var_keyword = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_var_keyword:
        return dict(context)
    accepted_names = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    }
    return {k: v for k, v in context.items() if k in accepted_names}


@dataclass
class SubsystemOutput:
    """Output of one subsystem dispatch.

    Directives feed the narrator prompt. Data feeds downstream subsystems
    (e.g., Group C lethality reads npc_agency disposition from here) and
    the StatePatch phase.

    Convention: when a subsystem produces no useful output (e.g., looks up
    a missing entity), it returns ``directives=[]`` and ``data["error"]`` set
    to a short string code (e.g., ``"npc_not_registered"``). The bank
    executor (Task 7) does not raise on error-only outputs; it records them
    and continues. Subsystems MAY include additional diagnostic fields in
    data alongside the error code.
    """

    directives: list[NarratorDirective] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BankResult:
    """Result of executing a DispatchPackage's subsystem bank."""

    directives: list[NarratorDirective] = field(default_factory=list)
    outputs_by_key: dict[str, SubsystemOutput] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)


# Registry populated at import time in _register_defaults().
_REGISTRY: dict[str, SubsystemCallable] = {}


def register_subsystem(name: str, fn: SubsystemCallable) -> None:
    if name in _REGISTRY:
        raise ValueError(f"subsystem already registered: {name}")
    _REGISTRY[name] = fn


def get_registered() -> dict[str, SubsystemCallable]:
    return dict(_REGISTRY)


def _register_defaults() -> None:
    from sidequest.agents.subsystems.distinctive_detail import run_distinctive_detail
    from sidequest.agents.subsystems.npc_agency import run_npc_agency
    from sidequest.agents.subsystems.reflect_absence import run_reflect_absence

    # Unregister-then-register to keep this import idempotent across test reloads.
    for name, fn in (
        ("reflect_absence", run_reflect_absence),
        ("distinctive_detail_hint", run_distinctive_detail),
        ("npc_agency", run_npc_agency),
    ):
        _REGISTRY.pop(name, None)
        _REGISTRY[name] = fn


_register_defaults()


def _topo_sort(dispatches: list[SubsystemDispatch]) -> list[SubsystemDispatch]:
    by_key = {d.idempotency_key: d for d in dispatches}
    order: list[SubsystemDispatch] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            raise ValueError(f"cycle in depends_on involving {key}")
        if key not in by_key:
            visited.add(key)
            return
        visiting.add(key)
        for dep in by_key[key].depends_on:
            visit(dep)
        visiting.remove(key)
        visited.add(key)
        order.append(by_key[key])

    for d in dispatches:
        visit(d.idempotency_key)
    return order


async def run_dispatch_bank(
    package: DispatchPackage,
    *,
    context: dict[str, Any] | None = None,
) -> BankResult:
    """Execute every SubsystemDispatch in the package.

    Runs sequentially in topological order. Unknown subsystems are logged
    and skipped. Exceptions are caught per-dispatch and logged; never
    re-raised.
    """
    context = context or {}
    result = BankResult()

    all_dispatches: list[SubsystemDispatch] = []
    for pd in package.per_player:
        all_dispatches.extend(pd.dispatch)
    # CrossAction has no narrator_instructions field (Group B; may extend in Group G).
    # Authored directives flow only through per_player[*].narrator_instructions.
    for ca in package.cross_player:
        all_dispatches.extend(ca.dispatch)

    with intent_router_dispatch_bank_span(
        turn_id=package.turn_id,
        dispatch_count=len(all_dispatches),
    ) as bank_span:
        if not all_dispatches:
            # Still include decomposer-authored narrator_instructions even when no
            # subsystem dispatches ran.
            for pd in package.per_player:
                result.directives.extend(pd.narrator_instructions)
            return result

        try:
            ordered = _topo_sort(all_dispatches)
        except ValueError as exc:
            logger.error("subsystems.bank_topo_sort_failed exc=%s", exc)
            result.errors.append(("__bank__", repr(exc)))
            bank_span.set_attribute("error", "topo_sort_failure")
            # Authored directives still flow; zero subsystem dispatches run.
            for pd in package.per_player:
                result.directives.extend(pd.narrator_instructions)
            return result

        seen: set[str] = set()
        for d in ordered:
            if d.idempotency_key in seen:
                continue
            seen.add(d.idempotency_key)

            with intent_router_subsystem_span(
                subsystem=d.subsystem,
                idempotency_key=d.idempotency_key,
            ) as sub_span:
                fn = _REGISTRY.get(d.subsystem)
                if fn is None:
                    logger.warning(
                        "subsystems.unknown subsystem=%s key=%s",
                        d.subsystem,
                        d.idempotency_key,
                    )
                    sub_span.set_attribute("error", "unknown_subsystem")
                    sub_span.set_attribute("produced_directives", 0)
                    continue
                # Filter ``context`` to only the kwargs ``fn`` declares —
                # subsystems have heterogeneous signatures (e.g.,
                # ``run_npc_agency`` requires ``npc_pool`` but
                # ``run_distinctive_detail`` accepts only ``dispatch``).
                # Without filtering, blasting ``**context`` into the latter
                # raises ``TypeError: unexpected keyword argument``.
                fn_kwargs = _filter_context_for_callable(fn, context)
                try:
                    out = await fn(d, **fn_kwargs)
                except Exception as exc:
                    logger.warning(
                        "subsystems.dispatch_failed subsystem=%s key=%s exc=%s",
                        d.subsystem,
                        d.idempotency_key,
                        exc,
                    )
                    result.errors.append((d.idempotency_key, repr(exc)))
                    sub_span.set_attribute("error", type(exc).__name__)
                    sub_span.set_attribute("produced_directives", 0)
                    continue

                result.outputs_by_key[d.idempotency_key] = out
                result.directives.extend(out.directives)
                sub_span.set_attribute("produced_directives", len(out.directives))
                # Surface subsystem-level errors that returned via data["error"]
                # rather than raising (e.g., npc_not_registered).
                err_code = out.data.get("error") if isinstance(out.data, dict) else None
                if err_code:
                    sub_span.set_attribute("error", str(err_code))

        for pd in package.per_player:
            result.directives.extend(pd.narrator_instructions)

        return result


__all__ = [
    "BankResult",
    "SubsystemCallable",
    "SubsystemOutput",
    "get_registered",
    "register_subsystem",
    "run_dispatch_bank",
]
