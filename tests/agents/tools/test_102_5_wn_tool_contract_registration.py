"""RED tests for Story 102-5 (AC1) — the WN narrator tool contract: registration.

SWN module design §8 enumerates the narrator's WN moves: attack, skill check,
save, and dead-premise adjudication. This story registers one FAMILY-SHAPED
tool per move (epic 102 guardrail: "one contract, four modules — avoid four
parallel tool sets; the module seam (ADR-117) does the differentiation"):

    wn_attack(attacker, target, weapon)
    wn_skill_check(actor, skill, attribute, skill_level, difficulty)
    wn_save(actor, save, effect)
    wn_adjudicate_dead_premise(actor, action, gone_target, ruling, reason)

Naming decision (TEA, 102-5 RED): §8 spells the SWN-era names ``swn_*``; the
epic's family guardrail supersedes that prefix — the tools are shared across
swn/wwn/cwn/awn with the BOUND module slug parameterizing resolution, so the
registered names carry the family prefix ``wn_``. ``skill_level`` is supplied
by the narrator (sheet-derived) mirroring the existing dice-path contract
(``protocol/messages.py`` CheckThrowPayload.skill_level) — skill levels are
not stored on CreatureCore.

Registration contract pinned here:

* All four tools are registered on ``default_registry`` by importing
  ``sidequest.agents.tools`` (the production registration path).
* ``tool_definitions(ruleset=<slug>)`` advertises all four for EVERY WN slug
  (swn/wwn/cwn/awn) and NONE of them for ``native``. Today's filter
  (``_RegisteredTool.ruleset: str | None``, exact-match, Story 73-15) cannot
  express a four-slug family — these tests force the seam extension.
* The family declaration is data-driven on the Registry seam (a tuple of
  slugs), not a name allowlist (mirrors 73-15 AC-4).
* Typed payloads: each tool's input_schema requires the §8 call-shape fields.
* ADR-111: the rules ride in the tool DESCRIPTIONS (recency-zone migration) —
  each description is substantive, not a placeholder.

All tests FAIL until 102-5 is implemented: the tools do not exist yet.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

# Importing the tools package wires every adapter onto default_registry —
# the production registration path (no test-only registration).
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.tool_registry import (
    Registry,
    ToolCategory,
    ToolContext,
    ToolResult,
    default_registry,
    tool,
)

WN_TOOLS = {
    "wn_attack",
    "wn_skill_check",
    "wn_save",
    "wn_adjudicate_dead_premise",
}
WN_SLUGS = ("swn", "wwn", "cwn", "awn")


def _names(defs: list[Any]) -> set[str]:
    return {d.name for d in defs}


def _schema(tool_name: str) -> dict[str, Any]:
    defs = {d.name: d for d in default_registry.tool_definitions()}
    assert tool_name in defs, f"{tool_name} not registered"
    return defs[tool_name].input_schema


# ---------------------------------------------------------------------------
# AC1 — contract completeness: the spec-derived list is registered
# ---------------------------------------------------------------------------


def test_wn_contract_tools_are_registered() -> None:
    """Every WN move §8 enumerates has a registered tool (AC1)."""
    names = set(default_registry.list_names())
    missing = WN_TOOLS - names
    assert not missing, f"§8 contract tools missing from default_registry: {missing}"


@pytest.mark.parametrize("slug", WN_SLUGS)
def test_wn_tools_advertised_to_every_wn_ruleset(slug: str) -> None:
    """One contract, four modules: a narrator bound to ANY WN-family ruleset
    sees the full §8 toolset. Today's exact-match filter cannot express this —
    the family declaration is the seam extension this story lands."""
    names = _names(default_registry.tool_definitions(ruleset=slug))
    missing = WN_TOOLS - names
    assert not missing, f"ruleset {slug!r} is not advertised WN tools: {missing}"


def test_wn_tools_hidden_from_native() -> None:
    """ADR-117 tightening (73-15): a native pack's narrator never sees the WN
    contract — these tools could only fail there."""
    names = _names(default_registry.tool_definitions(ruleset="native"))
    leaked = WN_TOOLS & names
    assert not leaked, f"native pack advertises WN contract tools: {leaked}"


def test_wn_tools_in_unfiltered_catalog_back_compat() -> None:
    """The no-arg catalog (diagnostic call site, self-guard backstop) still
    lists the WN tools — filtering is advertisement-only, not dispatchability."""
    names = _names(default_registry.tool_definitions())
    assert names >= WN_TOOLS


# ---------------------------------------------------------------------------
# The Registry seam: family declaration is data-driven (mirrors 73-15 AC-4)
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    pass


def test_family_ruleset_declaration_is_data_driven() -> None:
    """A tool declared for a FAMILY of slugs is advertised to each member and
    hidden from non-members. Pinned on a fresh registry with a novel name so
    an allowlist keyed on the four real tool names cannot fake it."""
    reg = Registry()

    @tool(
        name="novel_family_widget",
        description="x",
        category=ToolCategory.WRITE,
        registry=reg,
        ruleset=("wwn", "cwn"),
    )
    async def _w(args: _NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({})

    for member in ("wwn", "cwn"):
        assert "novel_family_widget" in _names(reg.tool_definitions(ruleset=member))
    for outsider in ("native", "swn"):
        assert "novel_family_widget" not in _names(reg.tool_definitions(ruleset=outsider))
    # Unfiltered back-compat unchanged.
    assert "novel_family_widget" in _names(reg.tool_definitions())


def test_single_slug_declaration_still_works() -> None:
    """The 73-15 single-slug form survives the family extension — the five
    already-gated tools keep their exact-match behavior."""
    names = _names(default_registry.tool_definitions(ruleset="native"))
    assert "commit_effort" not in names
    assert "veterans_luck" not in names
    wwn_names = _names(default_registry.tool_definitions(ruleset="wwn"))
    assert "commit_effort" in wwn_names


# ---------------------------------------------------------------------------
# Typed payloads — §8 call shapes are pinned in each tool's input schema
# ---------------------------------------------------------------------------


def test_wn_attack_schema_requires_attacker_target_weapon() -> None:
    schema = _schema("wn_attack")
    required = set(schema.get("required", []))
    assert {"attacker", "target", "weapon"} <= required, (
        f"§8: swn_attack(attacker, target, weapon); got required={required}"
    )


def test_wn_skill_check_schema_requires_call_shape() -> None:
    schema = _schema("wn_skill_check")
    required = set(schema.get("required", []))
    assert {"actor", "skill", "attribute", "difficulty"} <= required, (
        f"§8: swn_skill_check(actor, skill, attribute, difficulty); got required={required}"
    )
    # skill_level rides the call (sheet-derived, narrator-supplied) — mirrors
    # the dice-path CheckThrowPayload contract. Present in the schema either
    # required or defaulted.
    assert "skill_level" in schema.get("properties", {}), (
        "wn_skill_check must carry skill_level (the engine stores no skill sheet)"
    )


def test_wn_save_schema_requires_actor_save_effect() -> None:
    schema = _schema("wn_save")
    required = set(schema.get("required", []))
    assert {"actor", "save"} <= required, (
        f"§8: swn_save(actor, save, effect); got required={required}"
    )
    assert "effect" in schema.get("properties", {}), (
        "wn_save must carry the effect being saved against (span + narration evidence)"
    )


def test_wn_adjudicate_dead_premise_schema_pins_ruling_enum() -> None:
    """§6/§8: the narrator picks redirect-vs-fizzle; the payload types the
    ruling closed so the narrator cannot invent a third outcome. The reason
    rides the call — the span records WHY (§8 OTEL column)."""
    schema = _schema("wn_adjudicate_dead_premise")
    required = set(schema.get("required", []))
    assert {"actor", "action", "gone_target", "ruling"} <= required, (
        f"dead-premise adjudication call shape incomplete; got required={required}"
    )
    props = schema.get("properties", {})
    assert "reason" in props, "the span records redirect-vs-fizzle AND WHY — reason is typed"
    ruling_schema = json.dumps(props.get("ruling", {}))
    assert "redirect" in ruling_schema and "fizzle" in ruling_schema, (
        f"ruling must be a closed redirect|fizzle enum; got {ruling_schema}"
    )


# ---------------------------------------------------------------------------
# ADR-111 — the rules live in the tool descriptions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(WN_TOOLS))
def test_wn_tool_descriptions_carry_the_contract(tool_name: str) -> None:
    """ADR-111 moved recency-zone guardrails into tool descriptions — a WN
    tool's description is the rule surface, not a placeholder."""
    defs = {d.name: d for d in default_registry.tool_definitions()}
    desc = defs[tool_name].description
    assert len(desc) >= 60, (
        f"{tool_name} description is too thin to carry ADR-111 guardrails: {desc!r}"
    )


def test_wn_tool_descriptions_are_distinct() -> None:
    defs = {d.name: d for d in default_registry.tool_definitions()}
    descs = [defs[n].description for n in sorted(WN_TOOLS)]
    assert len(set(descs)) == len(descs), "WN tool descriptions must be tool-specific"
