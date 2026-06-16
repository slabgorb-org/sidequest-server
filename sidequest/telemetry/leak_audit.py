"""Canonical-leak audit — safety-net verification.

Primary defense is structural hiding (``sidequest.agents.prompt_redaction``).
This module verifies the narrator's canonical prose contains no tokens
corresponding to redacted entities. Expected-zero detections in steady
state; any non-zero count is a structural-hiding bug.

The match is entity-token-set vs. prose, NOT regex on arbitrary strings —
tokens are supplied by the caller from the authoritative NPC registry.
This satisfies the SOUL.md Zork constraint: no keyword matching.

Emits a ``narrator.canonical_leak_audit`` OTEL span per turn so the GM
panel can see the audit fire. ``leaks_detected=0`` is the steady-state
telemetry shape — a non-zero count means structural hiding has a hole
and Sebastien's lie-detector just caught it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opentelemetry import trace

from sidequest.protocol.dispatch import DispatchPackage, SubsystemDispatch

_tracer = trace.get_tracer("sidequest.leak_audit")


@dataclass(frozen=True)
class LeakAuditResult:
    """Structured result of a canonical-leak audit.

    Attributes:
        turn_id: From the audited DispatchPackage.
        leaks_detected: Count of redacted entities whose tokens appeared
            in the canonical prose. Expected zero in steady state.
        redact_tag_count: Count of SubsystemDispatch entries with
            ``redact_from_narrator_canonical=True`` — gives the span a
            baseline "we had N things to hide" reading.
        leaked_entities: The entity ids that leaked, in discovery order.
        leaked_fragments: Per-leak ~40-character prose window around the
            first matching token — for GM-panel display.
    """

    turn_id: str
    leaks_detected: int
    redact_tag_count: int
    leaked_entities: list[str] = field(default_factory=list)
    leaked_fragments: list[str] = field(default_factory=list)


def audit_canonical_prose(
    *,
    prose: str,
    package: DispatchPackage,
    entity_tokens_by_id: dict[str, list[str]],
) -> LeakAuditResult:
    """Scan prose for tokens from entities flagged ``redact_from_narrator_canonical``.

    Args:
        prose: Canonical narrator prose for the turn, post-extraction
            and pre-emission.
        package: The ORIGINAL DispatchPackage (pre-redaction). The audit
            needs to know what was supposed to be hidden to check it
            wasn't leaked — the redacted/visible view would pass
            trivially.
        entity_tokens_by_id: ``entity_id -> [display_name, *aliases, role_noun]``
            drawn from the authoritative NPC registry / character sheet.
            A partial token set is still a working audit; exhaustive
            alias coverage is not required.

    Returns:
        ``LeakAuditResult`` with the audit outcome. The OTEL span
        ``narrator.canonical_leak_audit`` is emitted as a side effect.
    """
    redacted_entities: list[str] = []
    for pd in package.per_player:
        for d in pd.dispatch:
            if not isinstance(d, SubsystemDispatch):
                continue
            if d.visibility.redact_from_narrator_canonical:
                target = d.params.get("target") if isinstance(d.params, dict) else None
                if isinstance(target, str):
                    redacted_entities.append(target)

    leaks: list[str] = []
    fragments: list[str] = []
    prose_lower = prose.lower()
    for entity_id in redacted_entities:
        for token in entity_tokens_by_id.get(entity_id, []):
            if not token:
                continue
            idx = prose_lower.find(token.lower())
            if idx != -1:
                leaks.append(entity_id)
                fragments.append(prose[max(0, idx - 20) : idx + len(token) + 20])
                break

    result = LeakAuditResult(
        turn_id=package.turn_id,
        leaks_detected=len(leaks),
        redact_tag_count=len(redacted_entities),
        leaked_entities=leaks,
        leaked_fragments=fragments,
    )

    with _tracer.start_as_current_span("narrator.canonical_leak_audit") as span:
        span.set_attribute("turn_id", result.turn_id)
        span.set_attribute("leaks_detected", result.leaks_detected)
        span.set_attribute("redact_tag_count", result.redact_tag_count)
        span.set_attribute("leaked_entities", result.leaked_entities)
        span.set_attribute("leaked_fragments", result.leaked_fragments)

    return result


__all__ = ["LeakAuditResult", "audit_canonical_prose"]
