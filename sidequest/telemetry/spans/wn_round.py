"""WN sealed-round OTEL spans (story 102-4). GM panel = lie detector.

The WN turn model (sealed commitment → initiative-ordered resolution) is
family behavior shared by all four sisters, so the span names are
slug-parametrized per the epic's ``{ruleset}.{surface}`` invariant — a pack
bound to ``awn`` emits ``awn.round.resolved``, never ``cwn.*`` (honest slug
per binding). Routes are registered for every family slug at import time so
the watcher translator can type each one.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# The WN family binding slugs (registry keys). WWN/CWN subclass SWN and AWN
# subclasses CWN, so one turn-model implementation serves all four — but each
# binding keeps its own honest span namespace.
WN_FAMILY_SLUGS = ("swn", "wwn", "cwn", "awn")

for _slug in WN_FAMILY_SLUGS:
    SPAN_ROUTES[f"{_slug}.round.committed"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "round_committed",
            "committed_actors": (span.attributes or {}).get("committed_actors", ""),
            "exempt_allies": (span.attributes or {}).get("exempt_allies", ""),
        },
    )
    SPAN_ROUTES[f"{_slug}.round.initiative"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "round_initiative",
            "initiative_order": (span.attributes or {}).get("initiative_order", ""),
        },
    )
    SPAN_ROUTES[f"{_slug}.round.resolved"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "round_resolved",
            "resolution_order": (span.attributes or {}).get("resolution_order", ""),
        },
    )
    SPAN_ROUTES[f"{_slug}.dead_premise"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "dead_premise",
            "actor": (span.attributes or {}).get("actor", ""),
            "target": (span.attributes or {}).get("target", ""),
        },
    )
    SPAN_ROUTES[f"{_slug}.native_scaffolding_suppressed"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "native_scaffolding_suppressed",
            "actor": (span.attributes or {}).get("actor", ""),
            "beat_id": (span.attributes or {}).get("beat_id", ""),
            "suppressed": (span.attributes or {}).get("suppressed", ""),
            "hp_removed": (span.attributes or {}).get("hp_removed", 0),
        },
    )
    SPAN_ROUTES[f"{_slug}.action.flavor_rider"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=lambda span: {
            "field": "flavor_rider",
            "actor": (span.attributes or {}).get("actor", ""),
            "beat_id": (span.attributes or {}).get("beat_id", ""),
            "attached": (span.attributes or {}).get("attached", False),
            "affected_mechanics": (span.attributes or {}).get("affected_mechanics", False),
        },
    )


def _require_family_slug(slug: str) -> None:
    if slug not in WN_FAMILY_SLUGS:
        raise ValueError(
            f"WN round spans are family-only; got slug {slug!r} "
            f"(expected one of {WN_FAMILY_SLUGS}) — No Silent Fallbacks"
        )


def wn_round_committed_span(
    *,
    slug: str,
    committed_actors: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.round.committed — the sealed-commit barrier closed."""
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "round_committed",
        "committed_actors": committed_actors,
        **attrs,
    }
    with Span.open(f"{slug}.round.committed", attributes, tracer_override=_tracer):
        pass


def wn_round_initiative_span(
    *,
    slug: str,
    initiative_order: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.round.initiative — the persisted order this round resolves in."""
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "round_initiative",
        "initiative_order": initiative_order,
        **attrs,
    }
    with Span.open(f"{slug}.round.initiative", attributes, tracer_override=_tracer):
        pass


def wn_round_resolved_span(
    *,
    slug: str,
    resolution_order: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.round.resolved — the round walk finished.

    ``resolution_order`` is the comma-space-joined token_id sequence the
    engine actually walked (descending persisted initiative) — the
    lie-detector for "whose shot lands first".
    """
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "round_resolved",
        "resolution_order": resolution_order,
        **attrs,
    }
    with Span.open(f"{slug}.round.resolved", attributes, tracer_override=_tracer):
        pass


def wn_dead_premise_span(
    *,
    slug: str,
    actor: str,
    target: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.dead_premise — a committed action's target was already down.

    The engine surfaces the event and does NOT resolve the action; the
    narrator adjudicates redirect-or-fizzle in fiction (SOUL: The Test).
    """
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "dead_premise",
        "actor": actor,
        "target": target,
        **attrs,
    }
    with Span.open(f"{slug}.dead_premise", attributes, tracer_override=_tracer):
        pass


def wn_native_scaffolding_suppressed_span(
    *,
    slug: str,
    actor: str,
    beat_id: str,
    hp_removed: int,
    suppressed: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.native_scaffolding_suppressed (story 108-1, ADR-143).

    The GM-panel lie-detector that the native combat engine did NOT resolve
    this committed WN action: under a Without-Number binding ``run_wn_round``
    applies the WN math (weapon dice → HP, the hp_depletion win check) directly
    and never reaches ``ruleset.apply_beat`` — so no native fleeting tag
    (Opening / Counter Stance), no dial-metric advance, no composure rider, and
    no Brace-as-an-action fired. ``suppressed`` names the riders that were cut;
    ``hp_removed`` is the WN damage that DID resolve, so a reader can tell a
    real strike from a no-op. Slug-honest per the WN family invariant (a wwn
    pack emits ``wwn.native_scaffolding_suppressed``, never ``native.*``).
    """
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "native_scaffolding_suppressed",
        "actor": actor,
        "beat_id": beat_id,
        "hp_removed": hp_removed,
        "suppressed": suppressed,
        **attrs,
    }
    with Span.open(f"{slug}.native_scaffolding_suppressed", attributes, tracer_override=_tracer):
        pass


def wn_flavor_rider_span(
    *,
    slug: str,
    actor: str,
    beat_id: str,
    affected_mechanics: bool = False,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit {slug}.action.flavor_rider (story 108-5, ADR-143).

    The GM-panel lie-detector that the player's RP-flavor rider — the freeform
    "chandelier swing" the player typed alongside a WN action button and that
    rides ``DICE_THROW.player_action`` — was attached as narrator color ONLY and
    did NOT enter mechanical resolution. Under a Without-Number binding the roll
    is already resolved on the button (the closed verb set is the scaffold that
    forces the engine to be the authority); the rider is downstream cosmetic
    context the narrator uses to flavor the resolved outcome (spec 108-5). The
    span fires ONLY when text is actually attached, so ``attached`` is always
    True. ``affected_mechanics`` is the structural attestation that the dispatch
    resolved the d20/weapon dice without consulting the rider text — the green
    guard ``test_rider_removes_identical_opponent_hp`` is its runtime proof.

    Without this span the GM panel cannot tell an inert RP affordance from a
    covert freeform-adjudication regression — the El Dorado failure ADR-143
    exists to prevent. Pairs with {slug}.native_scaffolding_suppressed (108-1):
    together they prove the WN round resolved on the button and only on the
    button. Slug-honest per the WN family invariant (a wwn pack emits
    ``wwn.action.flavor_rider``, never ``native.*``).
    """
    _require_family_slug(slug)
    attributes: dict[str, Any] = {
        "field": "flavor_rider",
        "actor": actor,
        "beat_id": beat_id,
        "attached": True,
        "affected_mechanics": affected_mechanics,
        **attrs,
    }
    with Span.open(f"{slug}.action.flavor_rider", attributes, tracer_override=_tracer):
        pass
