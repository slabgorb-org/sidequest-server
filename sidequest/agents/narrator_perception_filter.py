"""NarratorPerceptionFilter — concrete filter with per-tool rules.

Each per-tool rule is a function `(payload, perspective_pc) -> payload`.
Phase C tool conversions register their rule via the _RULES table.
Write tools are unfiltered (mutations are objective).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sidequest.agents.tool_registry import ToolCategory, ToolResult, ToolResultStatus

_RuleFn = Callable[[Any, str | None], Any]
_RULES: dict[str, _RuleFn] = {}


def register_rule(tool_name: str, fn: _RuleFn) -> None:
    if tool_name in _RULES:
        raise ValueError(f"Perception rule for {tool_name!r} already registered")
    _RULES[tool_name] = fn


class NarratorPerceptionFilter:
    def filter_result(
        self,
        *,
        tool_name: str,
        category: ToolCategory,
        result: ToolResult,
        perspective_pc: str | None,
    ) -> ToolResult:
        if category is ToolCategory.WRITE:
            return result
        if result.status is not ToolResultStatus.OK:
            return result
        rule = _RULES.get(tool_name)
        if rule is None:
            return result
        new_payload = rule(result.payload, perspective_pc)
        return ToolResult.ok(new_payload)


# ---------------------------------------------------------------------------
# Per-tool rules
# ---------------------------------------------------------------------------


# query_character — Phase C Task 6.
#
# Self / no-perspective: exact payload, untouched.
# Other party member: identity + visible status kept; sensitive sections
# (stats / inventory / backstory) dropped; exact hp numbers replaced
# with an hp_band (ADR-114).

_QC_KEEP_ALWAYS = frozenset(
    {
        "character_id",
        "name",
        "race",
        "char_class",
        "pronouns",
        "is_friendly",
        "status",
    }
)


def _hp_band(fraction: float) -> str:
    """Map an hp fraction to a severity band.

    Boundaries match the boundaries in the per-tool spec for Task 6:
    ``unwounded`` >0.75 · ``wounded`` >0.5 · ``bloodied`` >0.25 ·
    ``staggering`` >0 · ``down`` ==0. Negative hp (over-broken)
    collapses to ``down``.
    """
    if fraction <= 0.0:
        return "down"
    if fraction > 0.75:
        return "unwounded"
    if fraction > 0.5:
        return "wounded"
    if fraction > 0.25:
        return "bloodied"
    return "staggering"


def _coarsen_query_character(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: v for k, v in payload.items() if k in _QC_KEEP_ALWAYS}
    fraction = payload.get("hp_fraction")
    if isinstance(fraction, int | float):
        out["hp_band"] = _hp_band(float(fraction))
    return out


def _rule_query_character(payload: Any, perspective_pc: str | None) -> Any:
    if not isinstance(payload, dict):
        return payload  # defensive — handler always returns dict
    target_id = payload.get("character_id")
    if perspective_pc is None or target_id == perspective_pc:
        return payload
    return _coarsen_query_character(payload)


register_rule("query_character", _rule_query_character)


# query_npc — Phase C Task 7.
#
# v1: when ``perspective_pc`` is set, drop the raw ``disposition_value``
# (integer score in -100..+100) but keep the qualitative ``attitude``
# band. Omniscient views (``perspective_pc is None``) get the raw value
# untouched. Per-PC disposition tracks are forward-looking — the rule
# does the coarsening with the single global Disposition until ADR-020
# grows real per-PC observed views.


def _rule_query_npc(payload: Any, perspective_pc: str | None) -> Any:
    if not isinstance(payload, dict):
        return payload  # defensive — handler always returns dict
    if perspective_pc is None:
        return payload  # omniscient (test / debug)
    if "disposition_value" not in payload:
        return payload  # narrator did not request the disposition section
    return {k: v for k, v in payload.items() if k != "disposition_value"}


register_rule("query_npc", _rule_query_npc)
