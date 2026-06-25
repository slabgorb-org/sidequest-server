"""subsystems — Live-path dispatch handler registry for the Intent
Router engagement spine (ADR-113).

The Intent Router (``sidequest/agents/intent_router.py``) decomposes a
player action into a ``DispatchPackage``; ``run_dispatch_bank`` (this
module) executes each ``SubsystemDispatch`` against the registered
handler for its subsystem key, BEFORE the narrator runs.

Registered handlers (post-Story 59-6):
  - ``confrontation`` → ``run_confrontation_dispatch`` — engages a
    structured encounter on the canonical snapshot (the live engager
    that replaced the retired ``begin_confrontation`` sidecar tool).
  - ``magic_working`` → ``run_magic_working_dispatch`` — engages
    magic on the canonical snapshot via ``apply_magic_working``
    (replaces the retired ``result.magic_working`` sidecar consumer
    in ``narration_apply.py``).
  - ``scenario_clue`` → ``run_scenario_clue_dispatch`` — proactively
    advances prerequisite-eligible scenario clues via
    ``consume_clue_footnotes`` (supplements the narrator-footnote
    path; no retirement).
  - ``reflect_absence`` → ``run_reflect_absence`` — narrator directive
    forcing honest-absence framing when the player addresses someone
    not present.
  - ``distinctive_detail_hint`` → ``run_distinctive_detail`` — narrator
    directive naming a referent by a distinctive detail.
  - ``npc_agency`` → ``run_npc_agency`` — NPC disposition update.
  - ``movement`` → ``run_movement_dispatch`` — room-graph / location
    movement on the canonical snapshot.
  - ``witnessed_act`` → ``run_witnessed_act_dispatch`` — wry_whimsy
    political substrate (Plan 2): applies a publicly-witnessed act to the
    live ``PoliticalState`` belief/defiance dials, injects the ADR-053
    witness contradiction, and emits the premise/bloc OTEL spans.
  - ``fate_action`` → ``run_fate_action_dispatch`` — engages one classified
    Fate action (overcome/create_advantage/attack/concede) via
    ``dispatch_fate_action`` on a ``ruleset: fate`` pack (ADR-144 F2a).
  - ``dogfight`` → ``run_dogfight_dispatch`` — seats the ADR-077 sealed-letter
    ship-combat dogfight from a natural-language ship-combat intent, reusing
    the shared ``instantiate_encounter_from_trigger`` primitive (story 153-6).

All registered subsystems are live on the turn path (see ``_register_defaults``
for the authoritative list). The Intent Router's system prompt names them as
valid dispatch types and the dispatch engagement watcher (story 59-3) has
engagement witnesses for the snapshot-observable ones.
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

# ADR-113 confidence gate (Story 71-16). A dispatch engages its engine only
# when its router confidence meets the per-subsystem threshold; a subsystem
# with no pack-authored override uses this default. Packs tune per-subsystem
# values in rules.yaml (RulesConfig.dispatch_confidence_thresholds).
DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD = 0.6


def _threshold_for(subsystem: str, context: dict[str, Any]) -> float:
    """Resolve the engagement threshold for ``subsystem``.

    Reads the per-subsystem override from the genre pack's
    ``RulesConfig.dispatch_confidence_thresholds`` (the pack flows into the
    bank context via ``intent_router_pass``). Falls back to the documented
    0.6 default when no pack/override is present — an explicit default per
    ADR-113, not a silent guess at a malformed value (malformed thresholds
    fail loud at pack load, in RulesConfig validation).
    """
    pack = context.get("pack")
    rules = getattr(pack, "rules", None)
    thresholds = getattr(rules, "dispatch_confidence_thresholds", None)
    # The override source must be a real mapping (RulesConfig types this field
    # as dict[str, float] with a dict default, so production always satisfies
    # this). Anything else — no pack, or a non-RulesConfig stand-in — uses the
    # documented default.
    if isinstance(thresholds, dict):
        return thresholds.get(subsystem, DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD)
    return DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD


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
    # sq-playtest 2026-06-12: the durable per-dispatch audit trail. The
    # engage/degrade verdicts previously lived only in OTEL spans — never
    # persisted, evicted from the watcher ring buffer within minutes — so
    # "did the engine engage or did the narrator improvise?" cost an
    # offline turn replay to answer. The session handler threads this into
    # TurnRecord.dispatches and the validator persists it in turn_complete.
    # Entries: {subsystem, idempotency_key, confidence, threshold, decision
    # [, error]}, decision ∈ engaged | degraded_to_hint | unknown_subsystem.
    decisions: list[dict[str, Any]] = field(default_factory=list)


# Registry populated at import time in _register_defaults().
_REGISTRY: dict[str, SubsystemCallable] = {}


def register_subsystem(name: str, fn: SubsystemCallable) -> None:
    if name in _REGISTRY:
        raise ValueError(f"subsystem already registered: {name}")
    _REGISTRY[name] = fn


def get_registered() -> dict[str, SubsystemCallable]:
    return dict(_REGISTRY)


def _register_defaults() -> None:
    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch
    from sidequest.agents.subsystems.course import run_course_dispatch
    from sidequest.agents.subsystems.distinctive_detail import run_distinctive_detail
    from sidequest.agents.subsystems.dogfight import run_dogfight_dispatch
    from sidequest.agents.subsystems.environment_clock import run_environment_clock_dispatch
    from sidequest.agents.subsystems.equip import run_equip_dispatch
    from sidequest.agents.subsystems.fate_action import run_fate_action_dispatch
    from sidequest.agents.subsystems.magic_working import run_magic_working_dispatch
    from sidequest.agents.subsystems.movement import run_movement_dispatch
    from sidequest.agents.subsystems.npc_agency import run_npc_agency
    from sidequest.agents.subsystems.quest_offer import run_quest_offer_dispatch
    from sidequest.agents.subsystems.reflect_absence import run_reflect_absence
    from sidequest.agents.subsystems.scenario_clue import run_scenario_clue_dispatch
    from sidequest.agents.subsystems.witnessed_act import run_witnessed_act_dispatch

    # Unregister-then-register to keep this import idempotent across test reloads.
    for name, fn in (
        ("confrontation", run_confrontation_dispatch),
        ("magic_working", run_magic_working_dispatch),
        ("scenario_clue", run_scenario_clue_dispatch),
        ("reflect_absence", run_reflect_absence),
        ("distinctive_detail_hint", run_distinctive_detail),
        ("npc_agency", run_npc_agency),
        ("movement", run_movement_dispatch),
        ("witnessed_act", run_witnessed_act_dispatch),
        ("equip", run_equip_dispatch),
        ("environment_clock", run_environment_clock_dispatch),
        ("fate_action", run_fate_action_dispatch),
        ("quest_offer", run_quest_offer_dispatch),
        ("course", run_course_dispatch),
        ("dogfight", run_dogfight_dispatch),
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


_CONFRONTATION_SUBSYSTEM = "confrontation"


def _confrontation_types(pack: Any) -> frozenset[str]:
    """The confrontation TYPE names the active pack authors (combat, negotiation,
    chase, …) — these are ``params["type"]`` values for the ``confrontation``
    subsystem, NOT subsystem keys. Empty when the pack/rules are unavailable
    (the normalization simply no-ops)."""
    rules = getattr(pack, "rules", None)
    cdefs = getattr(rules, "confrontations", None) or []
    return frozenset(c.confrontation_type for c in cdefs if getattr(c, "confrontation_type", None))


def _normalize_confrontation_type_subsystems(
    dispatches: list[SubsystemDispatch], context: dict[str, Any]
) -> tuple[list[SubsystemDispatch], dict[str, str]]:
    """Repair a dispatch whose ``subsystem`` is actually a confrontation TYPE.

    The Intent Router (a Haiku LLM) intermittently emits ``subsystem="combat"``
    for a blunt combat verb ("I attack the banth") — conflating the confrontation
    *type* with the *subsystem* key. ``combat`` is not a registered subsystem, so
    the bank would silently drop it and the narrator would confabulate a kill over
    an empty encounter (story 158-28). Rewrite such a dispatch to the registered
    ``confrontation`` engager, carrying the type through ``params["type"]``.

    Loud, not silent (No Silent Fallbacks): the caller logs each repair and stamps
    ``normalized_from`` on the per-subsystem OTEL span so the GM panel sees the
    router-vocabulary repair (reuse the existing ``intent_router.subsystem`` span —
    one mechanism per problem). Only an UNREGISTERED subsystem that matches a pack
    confrontation type is rewritten, so a real registered subsystem is never
    hijacked. Returns the (possibly-rewritten) dispatch list and a
    ``{idempotency_key: original_subsystem}`` map for the span/audit.
    """
    types = _confrontation_types(context.get("pack"))
    if not types:
        return dispatches, {}
    normalized: dict[str, str] = {}
    out: list[SubsystemDispatch] = []
    for d in dispatches:
        if (
            d.subsystem != _CONFRONTATION_SUBSYSTEM
            and d.subsystem not in _REGISTRY
            and d.subsystem in types
        ):
            new_params = dict(d.params)
            new_params.setdefault("type", d.subsystem)
            normalized[d.idempotency_key] = d.subsystem
            d = d.model_copy(
                update={"subsystem": _CONFRONTATION_SUBSYSTEM, "params": new_params}
            )
        out.append(d)
    return out, normalized


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

    # Turn number for span attribution. The dispatch bank runs BEFORE
    # record_interaction() bumps the counter, but turn_complete emits
    # turn_id=interaction AFTER the bump — so reading the raw interaction here
    # stamps the PRIOR turn and the GM-panel grid shows intent_router/inventory
    # dark on the just-completed turn (off-by-one, DRIVER 2026-06-04). The
    # caller threads the EFFECTIVE turn number (interaction+1 for a player turn)
    # via ``context["turn_number"]``; we prefer it when present. Direct callers
    # (tests, non-pre-pass sites) that omit it fall back to interaction.
    _turn_number: int = 0
    _ctx_turn_number = context.get("turn_number")
    if _ctx_turn_number:
        _turn_number = int(_ctx_turn_number)
    else:
        _snapshot = context.get("snapshot")
        if _snapshot is not None:
            _tm = getattr(_snapshot, "turn_manager", None)
            if _tm is not None:
                _turn_number = int(getattr(_tm, "interaction", 0))

    all_dispatches: list[SubsystemDispatch] = []
    for pd in package.per_player:
        all_dispatches.extend(pd.dispatch)
    # CrossAction has no narrator_instructions field (Group B; may extend in Group G).
    # Authored directives flow only through per_player[*].narrator_instructions.
    for ca in package.cross_player:
        all_dispatches.extend(ca.dispatch)

    # Repair a router-vocabulary slip (158-28): a dispatch whose subsystem is
    # actually a confrontation TYPE ("combat") is rewritten to the registered
    # ``confrontation`` engager BEFORE topo-sort/dispatch, so a misclassified
    # combat verb seats instead of being silently dropped into a phantom kill.
    all_dispatches, _normalized_subsystems = _normalize_confrontation_type_subsystems(
        all_dispatches, context
    )
    for _ik, _orig in _normalized_subsystems.items():
        logger.warning(
            "subsystems.confrontation_type_subsystem_normalized original=%s -> confrontation key=%s",
            _orig,
            _ik,
        )

    with intent_router_dispatch_bank_span(
        turn_id=package.turn_id,
        dispatch_count=len(all_dispatches),
        turn_number=_turn_number,
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
                turn_number=_turn_number,
            ) as sub_span:
                # ADR-113 confidence gate (Story 71-16): engage the engine only
                # at/above the per-subsystem threshold. Below threshold the
                # dispatch degrades to a narrator hint — the player's intent
                # still reaches the narrator, but no engine fires on a weak
                # inference. Every gate decision is recorded on this span so the
                # GM panel (lie detector) can audit it.
                threshold = _threshold_for(d.subsystem, context)
                sub_span.set_attribute("confidence", float(d.confidence))
                sub_span.set_attribute("threshold", float(threshold))
                # 158-28: a confrontation-type-as-subsystem slip was repaired
                # upstream — stamp the original name on the span + audit so the GM
                # panel sees the router-vocabulary normalization (loud, not silent).
                _normalized_from = _normalized_subsystems.get(d.idempotency_key)
                if _normalized_from is not None:
                    sub_span.set_attribute("normalized_from", _normalized_from)
                # The durable audit entry (see BankResult.decisions) —
                # mutated in place as the gate/handler outcome lands below.
                decision_entry: dict[str, Any] = {
                    "subsystem": d.subsystem,
                    "idempotency_key": d.idempotency_key,
                    "confidence": float(d.confidence),
                    "threshold": float(threshold),
                    "decision": "engaged",
                    # The router's typed input — without it the audit says a
                    # movement dispatch failed but not WHAT was asked
                    # (descriptor/direction), forcing an offline replay
                    # (the exact hole that hid the flavor-descriptor veto).
                    "params": dict(d.params),
                }
                if _normalized_from is not None:
                    decision_entry["normalized_from"] = _normalized_from
                result.decisions.append(decision_entry)
                if d.confidence < threshold:
                    hint = NarratorDirective(
                        kind="must_narrate",
                        payload=(
                            f"The player's action suggested the {d.subsystem} "
                            f"subsystem, but the intent router's confidence "
                            f"({d.confidence:.2f}) was below the engagement "
                            f"threshold ({threshold:.2f}). Narrate the attempt "
                            f"naturally; do NOT treat the {d.subsystem} engine as "
                            f"having fired."
                        ),
                        visibility=d.visibility,
                    )
                    result.directives.append(hint)
                    sub_span.set_attribute("decision", "degraded_to_hint")
                    sub_span.set_attribute("produced_directives", 1)
                    decision_entry["decision"] = "degraded_to_hint"
                    continue
                sub_span.set_attribute("decision", "engaged")
                fn = _REGISTRY.get(d.subsystem)
                if fn is None:
                    logger.warning(
                        "subsystems.unknown subsystem=%s key=%s",
                        d.subsystem,
                        d.idempotency_key,
                    )
                    sub_span.set_attribute("error", "unknown_subsystem")
                    sub_span.set_attribute("produced_directives", 0)
                    decision_entry["decision"] = "unknown_subsystem"
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
                    decision_entry["error"] = type(exc).__name__
                    continue

                result.outputs_by_key[d.idempotency_key] = out
                result.directives.extend(out.directives)
                sub_span.set_attribute("produced_directives", len(out.directives))
                # Surface subsystem-level errors that returned via data["error"]
                # rather than raising (e.g., npc_not_registered).
                err_code = out.data.get("error") if isinstance(out.data, dict) else None
                if err_code:
                    sub_span.set_attribute("error", str(err_code))
                    decision_entry["error"] = str(err_code)

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
