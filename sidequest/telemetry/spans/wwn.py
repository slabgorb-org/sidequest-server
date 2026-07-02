"""WWN-specific OTEL spans. The GM panel is the lie detector for engine truth.

Copied from telemetry/spans/cwn.py (the WWN lethality layer is the shared
"Without Number" core) with the cwn->wwn namespace rename and the hacking span
dropped (WWN has no cyberspace). Duplication is intentional per the WWN spec §2.1.
"""

from __future__ import annotations

import json as _json
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# Lethality spans (System Strain / Trauma / Shock / Mortal Injury / Major Injury)
# were REMOVED in ADR-142: the WWN lethality stack is hoisted to the WN core
# (WithoutNumberRulesetModule) and emitted slug-namespaced via the core's
# slug-parameterized emitters in spans/wn.py (``trauma_roll_span(ruleset=...)``
# etc.), which own the ``wwn.{system_strain.delta,trauma.roll,shock.applied,
# mortal_injury.declared,major_injury.roll}`` routes. The per-slug emitter
# functions + their SPAN_WWN_* constants + routes that previously lived here are
# gone. The WWN-specific spans below (spell.cast, effort.*, killing_blow,
# veterans_luck, long_rest, magic_hydrated) remain live sibling-only spans.


# ---------------------------------------------------------------------------
# Magic spans
# ---------------------------------------------------------------------------

SPAN_WWN_SPELL_CAST = "wwn.spell.cast"
SPAN_ROUTES[SPAN_WWN_SPELL_CAST] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "spell_cast",
        "actor": (span.attributes or {}).get("actor", ""),
        "spell_id": (span.attributes or {}).get("spell_id", ""),
        "level": (span.attributes or {}).get("level", 0),
        "refused": (span.attributes or {}).get("refused", False),
        "casts_before": (span.attributes or {}).get("casts_before", 0),
        "casts_remaining": (span.attributes or {}).get("casts_remaining", 0),
        "save": (span.attributes or {}).get("save", ""),
        # save_made is OMITTED from the span attributes when no save was resolved
        # (None) — the GM panel must read "unresolved", never a misleading False.
        "save_made": (span.attributes or {}).get("save_made", None),
        "damage": (span.attributes or {}).get("damage", ""),
    },
)

SPAN_WWN_EFFORT_COMMIT = "wwn.effort.commit"
SPAN_ROUTES[SPAN_WWN_EFFORT_COMMIT] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "effort_commit",
        "actor": (span.attributes or {}).get("actor", ""),
        "source": (span.attributes or {}).get("source", ""),
        "points": (span.attributes or {}).get("points", 0),
        "duration": (span.attributes or {}).get("duration", ""),
        "available": (span.attributes or {}).get("available", 0),
        "applied": (span.attributes or {}).get("applied", True),
    },
)

SPAN_WWN_EFFORT_RECLAIM = "wwn.effort.reclaim"
SPAN_ROUTES[SPAN_WWN_EFFORT_RECLAIM] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "effort_reclaim",
        "actor": (span.attributes or {}).get("actor", ""),
        "source": (span.attributes or {}).get("source", ""),
        "points": (span.attributes or {}).get("points", 0),
        "trigger": (span.attributes or {}).get("trigger", ""),
        "available": (span.attributes or {}).get("available", 0),
    },
)

SPAN_WWN_KILLING_BLOW = "wwn.killing_blow"
SPAN_ROUTES[SPAN_WWN_KILLING_BLOW] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "killing_blow",
        "actor": (span.attributes or {}).get("actor", ""),
        "level": (span.attributes or {}).get("level", 0),
        "bonus": (span.attributes or {}).get("bonus", 0),
        "base": (span.attributes or {}).get("base", 0),
        "total": (span.attributes or {}).get("total", 0),
    },
)

SPAN_WWN_VETERANS_LUCK = "wwn.veterans_luck"
SPAN_ROUTES[SPAN_WWN_VETERANS_LUCK] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "veterans_luck",
        "actor": (span.attributes or {}).get("actor", ""),
        "mode": (span.attributes or {}).get("mode", ""),
        "applied": (span.attributes or {}).get("applied", False),
    },
)


def wwn_spell_cast_span(
    *,
    actor: str,
    spell_id: str,
    level: int,
    refused: bool,
    casts_before: int,
    casts_remaining: int,
    save: str,
    save_made: bool | None,
    damage: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.spell.cast span (lie-detector for WWN spell casting).

    ``casts_before`` / ``casts_remaining`` are the charge counts BEFORE and AFTER
    the cast — the GM panel reads the spend delta off one span (a real 2->1 spend
    vs a refused 2->2 no-op). On a refusal they are equal (no cast spent); on a
    successful cast ``casts_before == casts_remaining + 1`` (story 158-53, AC #2).

    ``save_made`` is ``None`` when NO save was resolved (no-save spell, or a save
    spell with no defender/stats). In that case the attribute is OMITTED entirely
    rather than coerced to False — OTEL attributes must not carry a misleading
    bool that tells the GM panel the defender "failed" a save that never rolled.
    """
    attributes: dict[str, Any] = {
        "field": "spell_cast",
        "actor": actor,
        "spell_id": spell_id,
        "level": level,
        "refused": refused,
        "casts_before": casts_before,
        "casts_remaining": casts_remaining,
        "save": save,
        "damage": damage,
        **attrs,
    }
    if save_made is not None:
        attributes["save_made"] = save_made
    with Span.open(SPAN_WWN_SPELL_CAST, attributes, tracer_override=_tracer):
        pass


# NOTE: wwn.effort.commit / wwn.effort.reclaim are emitted by the slug-namespaced
# ``effort_commit_span`` / ``effort_reclaim_span`` in spans/psionics.py (the Effort
# engine lifted to the SWN family base in Story 102-6, ``{ruleset}.effort.*``). The
# SPAN_WWN_EFFORT_* constants + routes above stay so the wwn-slug output still routes.


def wwn_killing_blow_span(
    *,
    actor: str,
    level: int,
    bonus: int,
    base: int,
    total: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.killing_blow span (lie-detector for WWN Killing Blow bonus damage)."""
    attributes: dict[str, Any] = {
        "field": "killing_blow",
        "actor": actor,
        "level": level,
        "bonus": bonus,
        "base": base,
        "total": total,
        **attrs,
    }
    with Span.open(SPAN_WWN_KILLING_BLOW, attributes, tracer_override=_tracer):
        pass


def wwn_veterans_luck_span(
    *,
    actor: str,
    mode: str,
    applied: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.veterans_luck span (lie-detector for WWN Veteran's Luck activation)."""
    attributes: dict[str, Any] = {
        "field": "veterans_luck",
        "actor": actor,
        "mode": mode,
        "applied": applied,
        **attrs,
    }
    with Span.open(SPAN_WWN_VETERANS_LUCK, attributes, tracer_override=_tracer):
        pass


# ---------------------------------------------------------------------------
# Long rest span (Plan 3 — party-wide Effort reclaim + casts refresh)
# ---------------------------------------------------------------------------

SPAN_WWN_LONG_REST = "wwn.long_rest"
SPAN_ROUTES[SPAN_WWN_LONG_REST] = SpanRoute(
    event_type="state_transition",
    component="wwn",
    extract=lambda span: {
        "field": "long_rest",
        "actor": (span.attributes or {}).get("actor", ""),
        "day_effort_reclaimed": (span.attributes or {}).get("day_effort_reclaimed", False),
        "casts_refreshed_to": (span.attributes or {}).get("casts_refreshed_to", 0),
        "reprepared": (span.attributes or {}).get("reprepared", False),
        "comfortable": (span.attributes or {}).get("comfortable", True),
    },
)


def wwn_long_rest_span(
    *,
    actor: str,
    day_effort_reclaimed: bool,
    casts_refreshed_to: int,
    reprepared: bool,
    comfortable: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.long_rest span (lie-detector for WWN long rest per PC)."""
    attributes: dict[str, Any] = {
        "field": "long_rest",
        "actor": actor,
        "day_effort_reclaimed": day_effort_reclaimed,
        "casts_refreshed_to": casts_refreshed_to,
        "reprepared": reprepared,
        "comfortable": comfortable,
        **attrs,
    }
    with Span.open(SPAN_WWN_LONG_REST, attributes, tracer_override=_tracer):
        pass


# ---------------------------------------------------------------------------
# Scene-harness hydration lie-detector (story 90-8 — 90-7 fast-follow)
# ---------------------------------------------------------------------------

SPAN_WWN_MAGIC_HYDRATED = "wwn.magic_hydrated"
SPAN_ROUTES[SPAN_WWN_MAGIC_HYDRATED] = SpanRoute(
    event_type="state_transition",
    component="magic",
    extract=lambda span: {
        "field": "magic_hydrated",
        "actor": (span.attributes or {}).get("actor", ""),
        "has_spellcasting": (span.attributes or {}).get("has_spellcasting", False),
        "prepared": (span.attributes or {}).get("prepared", 0),
        "casts_per_day": (span.attributes or {}).get("casts_per_day", 0),
        # JSON-encoded per the magic.py structured-payload convention — OTEL
        # attribute handling of empty sequences is a footgun; a JSON string
        # round-trips the empty list reliably.
        "effort_sources": _json.loads((span.attributes or {}).get("effort_sources_json", "[]")),
    },
)


def wwn_magic_hydrated_span(
    *,
    actor: str,
    has_spellcasting: bool,
    prepared: int,
    casts_per_day: int,
    effort_sources: list[str],
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a wwn.magic_hydrated span (story 90-8).

    Lie-detector proving a scene-harness fixture seeded real WWN crunch
    (spellcasting and/or Effort) rather than the narrator improvising it.
    Replaces 90-7's raw ``watcher_hub.publish_event`` emit so the event
    reaches the typed GM-panel Subsystems feed via the SPAN_ROUTES
    translation, not just the dashboard RAW console.
    """
    attributes: dict[str, Any] = {
        "field": "magic_hydrated",
        "actor": actor,
        "has_spellcasting": has_spellcasting,
        "prepared": prepared,
        "casts_per_day": casts_per_day,
        "effort_sources_json": _json.dumps(list(effort_sources)),
        **attrs,
    }
    with Span.open(SPAN_WWN_MAGIC_HYDRATED, attributes, tracer_override=_tracer):
        pass
