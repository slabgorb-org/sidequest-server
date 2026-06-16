"""CWN-specific OTEL spans. The GM panel is the lie detector for engine truth."""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

# ---------------------------------------------------------------------------
# Lethality spans (System Strain / Trauma / Shock / Mortal Injury / Major Injury)
# were REMOVED in ADR-142: the CWN lethality stack is hoisted to the WN core
# (WithoutNumberRulesetModule) and emitted slug-namespaced via the core's
# slug-parameterized emitters in spans/wn.py, which own the ``cwn.{...}`` routes.
# The per-slug emitter functions + their SPAN_CWN_* constants + routes that
# previously lived here are gone. The CWN-specific hacking span below remains a
# live sibling-only span.
# ---------------------------------------------------------------------------

SPAN_CWN_HACKING_SECURITY_CHECK = "cwn.hacking.security_check"
SPAN_ROUTES[SPAN_CWN_HACKING_SECURITY_CHECK] = SpanRoute(
    event_type="state_transition",
    component="cwn",
    extract=lambda span: {
        "field": "hacking",
        "actor": (span.attributes or {}).get("actor", ""),
        "verb": (span.attributes or {}).get("verb", ""),
        "tier": (span.attributes or {}).get("tier", ""),
        "base_dc": (span.attributes or {}).get("base_dc", 0),
        "alert_modifier": (span.attributes or {}).get("alert_modifier", 0),
        "effective_dc": (span.attributes or {}).get("effective_dc", 0),
        "result": (span.attributes or {}).get("result", ""),
    },
)


def cwn_hacking_security_check_span(
    *,
    actor: str,
    verb: str,
    tier: str,
    base_dc: int,
    alert_modifier: int,
    effective_dc: int,
    result: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit a cwn.hacking.security_check span (lie-detector for CWN hacking).

    Fires on EVERY resolved net_run verb so the GM panel sees engaged and
    unengaged rolls alike. Point mutation, not a span of work — opens and
    immediately closes so WatcherSpanProcessor routes it to the state_transition
    feed.
    """
    attributes: dict[str, Any] = {
        "field": "hacking",
        "actor": actor,
        "verb": verb,
        "tier": tier,
        "base_dc": base_dc,
        "alert_modifier": alert_modifier,
        "effective_dc": effective_dc,
        "result": result,
        **attrs,
    }
    with Span.open(SPAN_CWN_HACKING_SECURITY_CHECK, attributes, tracer_override=_tracer):
        pass
