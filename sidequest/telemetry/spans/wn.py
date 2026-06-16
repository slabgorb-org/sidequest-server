"""WN-family resolution spans — slug-parametrized (Story 102-5, ADR-117).

One narrator tool contract (``wn_attack`` / ``wn_skill_check`` / ``wn_save`` /
``wn_adjudicate_dead_premise``) spans the four "Without Number" modules
(swn/wwn/cwn/awn). The resolution span carries the BOUND module's slug so the
GM panel reads honest provenance: a wwn pack emits ``wwn.attack.resolved``, an
awn pack ``awn.attack.resolved`` — the slug-honesty invariant the epic pins.

Span names are dynamic (``{slug}.{event}``), so this module registers a route
per (slug, event) pair rather than per-name ``SPAN_*`` constants. The
routing-completeness lint scans ``SPAN_*`` module attributes; the dynamic names
are routed here without minting one constant apiece. Every route emits a
``state_transition`` typed event so the dashboard's Subsystems feed sees the
mechanical decision (the lie detector — CLAUDE.md OTEL Observability Principle).
"""

from __future__ import annotations

import json as _json
from typing import Any

from opentelemetry import trace

from ._core import SPAN_ROUTES, SpanRoute, _SpanLike
from .span import Span

# The four "Without Number" module slugs the WN tool contract serves.
WN_FAMILY_SLUGS: tuple[str, ...] = ("swn", "wwn", "cwn", "awn")

# Event suffix -> the ``state_transition`` field label the GM panel groups on.
_WN_EVENTS: dict[str, str] = {
    "attack.resolved": "wn_attack",
    "skill_check.resolved": "wn_skill_check",
    "save.resolved": "wn_save",
    "dead_premise.adjudicated": "wn_dead_premise",
}


def _make_extract(field: str):
    def _extract(span: _SpanLike) -> dict[str, Any]:
        attrs = dict(span.attributes or {})
        attrs["field"] = field
        return attrs

    return _extract


for _slug in WN_FAMILY_SLUGS:
    for _event, _field in _WN_EVENTS.items():
        SPAN_ROUTES[f"{_slug}.{_event}"] = SpanRoute(
            event_type="state_transition",
            component=_slug,
            extract=_make_extract(_field),
        )


# ---------------------------------------------------------------------------
# Lethality spans — slug-parameterized (ADR-142, Task 6 / DD-4).
#
# The WWN/CWN/AWN lethality stack (System Strain, Trauma, Shock, Mortal Injury,
# Major Injury) is hoisted to ``WithoutNumberRulesetModule`` (ADR-142), so its
# spans are emitted slug-namespaced from the core via ``self.slug`` — exactly
# the precedent ``effort_commit_span(ruleset=...)`` set (spans/psionics.py). The
# per-slug ``extract`` lambdas are copied byte-for-byte from the corresponding
# ``spans/wwn.py`` registrations so the GM-panel projection is unchanged when
# WWN/CWN later switch to these emitters. SWN has no lethality config and never
# emits these — only the three lethality-bearing slugs are routed.
# ---------------------------------------------------------------------------

# Lethality-bearing WN slugs (SWN carries no trauma/system_strain config).
WN_LETHALITY_SLUGS: tuple[str, ...] = ("wwn", "cwn", "awn")


def _system_strain_delta_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "system_strain",
        "actor": attrs.get("actor", ""),
        "source": attrs.get("source", ""),
        "amount": attrs.get("amount", 0),
        "new_total": attrs.get("new_total", 0),
        "max": attrs.get("max", 0),
        "applied": attrs.get("applied", True),
    }


def _trauma_roll_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "trauma",
        "actor": attrs.get("actor", ""),
        "weapon_die": attrs.get("weapon_die", ""),
        "roll": attrs.get("roll", 0),
        "target": attrs.get("target", 0),
        "traumatic": attrs.get("traumatic", False),
        "rating": attrs.get("rating", 1),
        "base": attrs.get("base", 0),
        "final": attrs.get("final", 0),
    }


def _shock_applied_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "shock",
        "actor": attrs.get("actor", ""),
        "amount": attrs.get("amount", 0),
        "melee_ac": attrs.get("melee_ac", 0),
        "shock_rating": attrs.get("shock_rating", 0),
        "shock_ac": attrs.get("shock_ac", 0),
    }


def _mortal_injury_declared_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "mortal_injury",
        "actor": attrs.get("actor", ""),
        "rounds_to_die": attrs.get("rounds_to_die", 0),
        # #846 (story 108-6): the supersede decision (terminal-dead overrode the
        # stabilizable window) must reach the GM panel, not be dropped from the
        # projection. The emitter already forwards it via **attrs.
        "superseded_by_terminal": attrs.get("superseded_by_terminal", False),
    }


def _dying_window_opened_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "dying_window_opened",
        "actor": attrs.get("actor", ""),
        "created_turn": attrs.get("created_turn", 0),
        "mortal_injury_rounds": attrs.get("mortal_injury_rounds", 0),
        "deadline_round": attrs.get("deadline_round", 0),
        "reason": attrs.get("reason", ""),
    }


def _dying_window_tick_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "dying_window_tick",
        "actor": attrs.get("actor", ""),
        "rounds_elapsed": attrs.get("rounds_elapsed", 0),
        "difficulty": attrs.get("difficulty", 0),
        "action_was_stabilization": attrs.get("action_was_stabilization", False),
        "roll": attrs.get("roll", 0),
        "success": attrs.get("success", False),
    }


def _dying_window_resolved_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "dying_window_resolved",
        "actor": attrs.get("actor", ""),
        "outcome": attrs.get("outcome", ""),
        "final_rounds_elapsed": attrs.get("final_rounds_elapsed", 0),
        "resulting_status": attrs.get("resulting_status", ""),
    }


def _major_injury_roll_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "major_injury",
        "actor": attrs.get("actor", ""),
        "save_made": attrs.get("save_made", True),
        "roll": attrs.get("roll", 0),
        "text": attrs.get("text", ""),
    }


# Event suffix -> the byte-identical extractor copied from spans/wwn.py.
_WN_LETHALITY_EXTRACTS = {
    "system_strain.delta": _system_strain_delta_extract,
    "trauma.roll": _trauma_roll_extract,
    "shock.applied": _shock_applied_extract,
    "mortal_injury.declared": _mortal_injury_declared_extract,
    "major_injury.roll": _major_injury_roll_extract,
    "dying_window.opened": _dying_window_opened_extract,
    "dying_window.tick": _dying_window_tick_extract,
    "dying_window.resolved": _dying_window_resolved_extract,
}

for _slug in WN_LETHALITY_SLUGS:
    for _event, _extract in _WN_LETHALITY_EXTRACTS.items():
        SPAN_ROUTES[f"{_slug}.{_event}"] = SpanRoute(
            event_type="state_transition",
            component=_slug,
            extract=_extract,
        )


# ---------------------------------------------------------------------------
# Chargen spans — prime-aware attribute assignment (ADR-143 Step 2).
#
# Registered for all four WN-family slugs (SWN included): any WN-bound world
# can have classes with prime_requisites even if SWN's lethality isn't
# engaged. The span name is ``{slug}.chargen.attributes_assigned`` (the
# slug-honesty invariant — a wwn pack emits ``wwn.chargen.attributes_assigned``
# and an swn pack ``swn.chargen.attributes_assigned``).
# ---------------------------------------------------------------------------


def _chargen_attributes_assigned_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "chargen_attributes_assigned",
        "prime": attrs.get("prime", ""),
        "top": attrs.get("top", 0),
        "stats": attrs.get("stats", ""),
    }


for _slug in WN_FAMILY_SLUGS:
    SPAN_ROUTES[f"{_slug}.chargen.attributes_assigned"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=_chargen_attributes_assigned_extract,
    )


def chargen_attributes_assigned_span(
    *,
    ruleset: str,
    prime: str | None,
    top: int,
    stats: dict[str, int],
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.chargen.attributes_assigned`` — lie-detector for
    prime-aware stat assignment (ADR-143 Step 2). Fires on every WN chargen
    whether prime is set or not, so the GM panel can confirm which path ran."""
    attributes: dict[str, Any] = {
        "prime": prime or "",
        "top": top,
        # Serialize the stats dict as JSON so the OTEL attribute type is a
        # simple string — OTEL SDK rejects nested dicts as attribute values.
        "stats": _json.dumps(dict(stats), sort_keys=True),
        **attrs,
    }
    with Span.open(f"{ruleset}.chargen.attributes_assigned", attributes, tracer_override=_tracer):
        pass


# ---------------------------------------------------------------------------
# Chargen contribution spans — background_skills + foci_applied (ADR-143 Task 10).
#
# Registered for all four WN-family slugs (every WN-bound pack may author
# backgrounds and foci). Span names are ``{slug}.chargen.background_skills``
# and ``{slug}.chargen.foci_applied`` — the slug-honesty invariant: a wwn
# pack emits ``wwn.chargen.background_skills``, an awn pack ``awn.*``.
# ---------------------------------------------------------------------------


def _chargen_background_skills_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "chargen_background_skills",
        "background": attrs.get("background", ""),
        "skills": attrs.get("skills", ""),
    }


def _chargen_foci_applied_extract(span: _SpanLike) -> dict[str, Any]:
    attrs = span.attributes or {}
    return {
        "field": "chargen_foci_applied",
        "foci": attrs.get("foci", ""),
        "skills": attrs.get("skills", ""),
    }


for _slug in WN_FAMILY_SLUGS:
    SPAN_ROUTES[f"{_slug}.chargen.background_skills"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=_chargen_background_skills_extract,
    )
    SPAN_ROUTES[f"{_slug}.chargen.foci_applied"] = SpanRoute(
        event_type="state_transition",
        component=_slug,
        extract=_chargen_foci_applied_extract,
    )


def chargen_background_skills_span(
    *,
    ruleset: str,
    background: str,
    skills: dict[str, int],
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.chargen.background_skills`` — lie-detector for
    background skill grants applied at chargen (ADR-143 Task 10)."""
    attributes: dict[str, Any] = {
        "background": background,
        "skills": _json.dumps(dict(skills), sort_keys=True),
        **attrs,
    }
    with Span.open(f"{ruleset}.chargen.background_skills", attributes, tracer_override=_tracer):
        pass


def chargen_foci_applied_span(
    *,
    ruleset: str,
    foci: list[str],
    skills: dict[str, int],
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.chargen.foci_applied`` — lie-detector for focus skill
    and ability grants applied at chargen (ADR-143 Task 10)."""
    attributes: dict[str, Any] = {
        "foci": _json.dumps(list(foci)),
        "skills": _json.dumps(dict(skills), sort_keys=True),
        **attrs,
    }
    with Span.open(f"{ruleset}.chargen.foci_applied", attributes, tracer_override=_tracer):
        pass


def system_strain_delta_span(
    *,
    ruleset: str,
    actor: str,
    source: str,
    amount: float,
    new_total: float,
    max: int,
    applied: bool,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.system_strain.delta`` (lie-detector for WN System Strain)."""
    attributes: dict[str, Any] = {
        "field": "system_strain",
        "actor": actor,
        "source": source,
        "amount": amount,
        "new_total": new_total,
        "max": max,
        "applied": applied,
        **attrs,
    }
    with Span.open(f"{ruleset}.system_strain.delta", attributes, tracer_override=_tracer):
        pass


def trauma_roll_span(
    *,
    ruleset: str,
    actor: str,
    weapon_die: str,
    roll: int,
    target: int,
    traumatic: bool,
    rating: int,
    base: int,
    final: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.trauma.roll`` (lie-detector for the WN trauma threshold check)."""
    attributes: dict[str, Any] = {
        "field": "trauma",
        "actor": actor,
        "weapon_die": weapon_die,
        "roll": roll,
        "target": target,
        "traumatic": traumatic,
        "rating": rating,
        "base": base,
        "final": final,
        **attrs,
    }
    with Span.open(f"{ruleset}.trauma.roll", attributes, tracer_override=_tracer):
        pass


def shock_applied_span(
    *,
    ruleset: str,
    actor: str,
    amount: int,
    melee_ac: int,
    shock_rating: int,
    shock_ac: int | None = None,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.shock.applied`` (lie-detector for WN shock damage)."""
    attributes: dict[str, Any] = {
        "field": "shock",
        "actor": actor,
        "amount": amount,
        "melee_ac": melee_ac,
        "shock_rating": shock_rating,
        "shock_ac": shock_ac,
        **attrs,
    }
    with Span.open(f"{ruleset}.shock.applied", attributes, tracer_override=_tracer):
        pass


def mortal_injury_declared_span(
    *,
    ruleset: str,
    actor: str,
    rounds_to_die: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.mortal_injury.declared`` (lie-detector for WN mortal wound)."""
    attributes: dict[str, Any] = {
        "field": "mortal_injury",
        "actor": actor,
        "rounds_to_die": rounds_to_die,
        **attrs,
    }
    with Span.open(f"{ruleset}.mortal_injury.declared", attributes, tracer_override=_tracer):
        pass


def dying_window_opened_span(
    *,
    ruleset: str,
    actor: str,
    created_turn: int,
    mortal_injury_rounds: int,
    deadline_round: int,
    reason: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.dying_window.opened`` — the WWN dying window opened (vs terminal)."""
    attributes: dict[str, Any] = {
        "field": "dying_window_opened",
        "actor": actor,
        "created_turn": created_turn,
        "mortal_injury_rounds": mortal_injury_rounds,
        "deadline_round": deadline_round,
        "reason": reason,
        **attrs,
    }
    with Span.open(f"{ruleset}.dying_window.opened", attributes, tracer_override=_tracer):
        pass


def dying_window_tick_span(
    *,
    ruleset: str,
    actor: str,
    rounds_elapsed: int,
    difficulty: int,
    action_was_stabilization: bool,
    roll: int = 0,
    success: bool = False,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.dying_window.tick`` — one engine-owned dying-window round."""
    attributes: dict[str, Any] = {
        "field": "dying_window_tick",
        "actor": actor,
        "rounds_elapsed": rounds_elapsed,
        "difficulty": difficulty,
        "action_was_stabilization": action_was_stabilization,
        "roll": roll,
        "success": success,
        **attrs,
    }
    with Span.open(f"{ruleset}.dying_window.tick", attributes, tracer_override=_tracer):
        pass


def dying_window_resolved_span(
    *,
    ruleset: str,
    actor: str,
    outcome: str,
    final_rounds_elapsed: int,
    resulting_status: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.dying_window.resolved`` — window exit (stabilized | died)."""
    attributes: dict[str, Any] = {
        "field": "dying_window_resolved",
        "actor": actor,
        "outcome": outcome,
        "final_rounds_elapsed": final_rounds_elapsed,
        "resulting_status": resulting_status,
        **attrs,
    }
    with Span.open(f"{ruleset}.dying_window.resolved", attributes, tracer_override=_tracer):
        pass


def major_injury_roll_span(
    *,
    ruleset: str,
    actor: str,
    save_made: bool,
    roll: int,
    text: str,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> None:
    """Emit ``{ruleset}.major_injury.roll`` (lie-detector for the WN Major Injury table)."""
    attributes: dict[str, Any] = {
        "field": "major_injury",
        "actor": actor,
        "save_made": save_made,
        "roll": roll,
        "text": text,
        **attrs,
    }
    with Span.open(f"{ruleset}.major_injury.roll", attributes, tracer_override=_tracer):
        pass


def wn_attack_resolved_span(
    *,
    slug: str,
    actor: str,
    target: str,
    weapon: str,
    hit: bool,
    d20: int,
    modifier: int,
    attack_total: int,
    target_ac: int,
    damage: int,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.attack.resolved`` — the GM-panel record of a WN attack roll."""
    with Span.open(
        f"{slug}.attack.resolved",
        {
            "actor": actor,
            "target": target,
            "weapon": weapon,
            "hit": hit,
            "d20": d20,
            "modifier": modifier,
            "attack_total": attack_total,
            "target_ac": target_ac,
            "damage": damage,
        },
        tracer_override=_tracer,
    ):
        pass


def wn_skill_check_resolved_span(
    *,
    slug: str,
    actor: str,
    skill: str,
    attribute: str,
    difficulty: int,
    total: int,
    modifier: int,
    success: bool,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.skill_check.resolved`` — the GM-panel record of a 2d6 check."""
    with Span.open(
        f"{slug}.skill_check.resolved",
        {
            "actor": actor,
            "skill": skill,
            "attribute": attribute,
            "difficulty": difficulty,
            "total": total,
            "modifier": modifier,
            "success": success,
        },
        tracer_override=_tracer,
    ):
        pass


def wn_save_resolved_span(
    *,
    slug: str,
    actor: str,
    save: str,
    effect: str,
    target: int,
    d20: int,
    modifier: int,
    success: bool,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.save.resolved`` — the GM-panel record of a d20 save."""
    with Span.open(
        f"{slug}.save.resolved",
        {
            "actor": actor,
            "save": save,
            "effect": effect,
            "target": target,
            "d20": d20,
            "modifier": modifier,
            "success": success,
        },
        tracer_override=_tracer,
    ):
        pass


def wn_dead_premise_adjudicated_span(
    *,
    slug: str,
    actor: str,
    action: str,
    gone_target: str,
    ruling: str,
    reason: str,
    _tracer: trace.Tracer | None = None,
) -> None:
    """Emit ``{slug}.dead_premise.adjudicated`` — records redirect-vs-fizzle AND why."""
    with Span.open(
        f"{slug}.dead_premise.adjudicated",
        {
            "actor": actor,
            "action": action,
            "gone_target": gone_target,
            "ruling": ruling,
            "reason": reason,
        },
        tracer_override=_tracer,
    ):
        pass
