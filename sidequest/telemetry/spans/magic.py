"""Magic spans — narrator-described workings (Coyote Star iter 3, Task 3.5).

Routes to the dashboard event feed via SPAN_ROUTES with
``event_type=state_transition``, ``component=magic``. The OTEL span
carries the structured attributes; the WatcherSpanProcessor re-emits
them through the route's ``extract`` lambda so the GM panel sees the
working as part of its existing event feed without needing a new UI tab.

Span attributes are JSON-encoded for the structured payloads (``costs``,
``flags``, ``ledger_after``) because OTEL silently drops dict/list
attribute values; the route extractor decodes them back at the boundary.
This mirrors the ``audio.skipped`` / ``lore.established`` precedent.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from sidequest.magic.models import Flag

from ._core import SPAN_ROUTES, SpanRoute
from .span import Span

SPAN_MAGIC_WORKING = "magic.working"
SPAN_INNATE_V1_CAST = "innate_v1.cast"

SPAN_ROUTES[SPAN_INNATE_V1_CAST] = SpanRoute(
    event_type="state_transition",
    component="magic",
    extract=lambda span: {
        "field": "magic_state",
        "op": "innate_v1_cast",
        "actor_id": (span.attributes or {}).get("actor_id", ""),
        "spell_id": (span.attributes or {}).get("spell_id", ""),
        "validator_outcome": (span.attributes or {}).get("validator_outcome", ""),
        "slot_consumed": (span.attributes or {}).get("slot_consumed", False),
        "save_skipped": (span.attributes or {}).get("save_skipped", False),
        "save_stat": (span.attributes or {}).get("save_stat", ""),
        "save_result": (span.attributes or {}).get("save_result", ""),
        "damage_applied": (span.attributes or {}).get("damage_applied", ""),
    },
)


@contextmanager
def innate_v1_cast_span(
    *,
    actor_id: str,
    spell_id: str,
    validator_outcome: str,
    slot_consumed: bool,
    save_skipped: bool,
    save_stat: str | None = None,
    save_result: str | None = None,
    damage_applied: str | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Story 47-10 — innate_v1.cast OTEL span.

    Emitted on every successful cast_spell beat resolution. Carries the
    spell-catalog-driven outcome shape (auto-apply vs save branch). Pairs
    with the existing learned_v1.cast span (which fires from direct
    learned_ops.cast paths used by tests) — innate_v1.cast is the
    production player-surface span; learned_v1.cast survives for any
    plugin or test that drives the data layer directly.
    """
    attributes: dict[str, Any] = {
        "actor_id": actor_id,
        "spell_id": spell_id,
        "validator_outcome": validator_outcome,
        "slot_consumed": slot_consumed,
        "save_skipped": save_skipped,
        **attrs,
    }
    if save_stat is not None:
        attributes["save_stat"] = save_stat
    if save_result is not None:
        attributes["save_result"] = save_result
    if damage_applied is not None:
        attributes["damage_applied"] = damage_applied
    with Span.open(SPAN_INNATE_V1_CAST, attributes) as span:
        yield span


SPAN_ROUTES[SPAN_MAGIC_WORKING] = SpanRoute(
    event_type="state_transition",
    component="magic",
    extract=lambda span: {
        "field": "magic_state",
        "op": "working",
        "plugin": (span.attributes or {}).get("plugin", ""),
        "actor": (span.attributes or {}).get("actor", ""),
        "mechanism_engaged": (span.attributes or {}).get("mechanism_engaged", ""),
        "domain": (span.attributes or {}).get("domain", ""),
        "narrator_basis": (span.attributes or {}).get("narrator_basis", ""),
        "costs_debited": _json.loads((span.attributes or {}).get("costs_debited_json", "{}")),
        "flags": _json.loads((span.attributes or {}).get("flags_json", "[]")),
        "ledger_after": _json.loads((span.attributes or {}).get("ledger_after_json", "{}")),
        "flavor": (span.attributes or {}).get("flavor", ""),
        "consent_state": (span.attributes or {}).get("consent_state", ""),
        "item_id": (span.attributes or {}).get("item_id", ""),
        "alignment_with_item_nature": (span.attributes or {}).get(
            "alignment_with_item_nature", 0.0
        ),
    },
)


@contextmanager
def magic_working_span(
    *,
    plugin: str,
    mechanism: str,
    actor: str,
    domain: str,
    narrator_basis: str,
    costs_debited: dict[str, float],
    flags: list[Flag],
    ledger_after: dict[str, float],
    flavor: str | None = None,
    consent_state: str | None = None,
    item_id: str | None = None,
    alignment_with_item_nature: float | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``magic.working`` OTEL span.

    Plugin-specific extras (``flavor`` / ``consent_state`` for innate_v1,
    ``item_id`` / ``alignment_with_item_nature`` for item_legacy_v1) are
    optional — None values coerce to the OTEL-safe defaults the route
    extractor falls back to. ``flags`` is serialised via ``model_dump``
    so the route can rehydrate the validator's ``Flag`` records on the
    other side.
    """
    attributes: dict[str, Any] = {
        "plugin": plugin,
        "mechanism_engaged": mechanism,
        "actor": actor,
        "domain": domain,
        "narrator_basis": narrator_basis,
        "costs_debited_json": _json.dumps(dict(costs_debited), sort_keys=True),
        "flags_json": _json.dumps([f.model_dump() for f in flags]),
        "ledger_after_json": _json.dumps(dict(ledger_after), sort_keys=True),
        "flavor": flavor or "",
        "consent_state": consent_state or "",
        "item_id": item_id or "",
        "alignment_with_item_nature": (
            float(alignment_with_item_nature) if alignment_with_item_nature is not None else 0.0
        ),
        **attrs,
    }
    with Span.open(SPAN_MAGIC_WORKING, attributes, tracer_override=_tracer) as span:
        yield span
