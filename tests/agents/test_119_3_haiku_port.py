"""Story 119-3 RED — AC3 (Haiku): single-shot structured-extraction fidelity.

The four single-shot Haiku call sites port off PAYG onto ``claude-agent-sdk``
through the same choke point (spec §6.4). The Agent SDK exposes **no
``tool_choice``** and **always executes a called tool's handler**, so the three
forced-extraction sites (Intent Router, unseeded-objective classifier,
archetype inference) can't transliterate the raw "force one tool, read its
``.input``" mechanism. The VERIFIED replacement is ``output_format``
JSON-schema structured output read from ``ResultMessage.structured_output``
(Path A, §6.4.2) — at **``max_turns=2``** (``max_turns=1`` fails closed with
``subtype='error_max_turns'``; the +1 is mandatory, OQ-16). The aside is the
easy one — already no-tools, also ``max_turns=2``.

Each site preserves its structured-payload contract (``dict`` / ``str`` /
``None``), its caller-tagged ``llm.request`` / ``llm.sdk.usage`` telemetry, and
its ``session_id``-keyed ``check_ceiling`` / ``record_call`` cost-safety. All
tests drive the fake ``query`` seam (OQ-9) — no live subscription.

**RED/GREEN GUARDRAIL (spec §9):** Dev MUST NOT hardcode ``max_turns=1`` from
any stale spec text — it fails closed. The options tests pin ``max_turns=2`` +
``output_format`` + ``allowed_tools=[]``; the ``max_turns_one`` tests pin that
the verified fail-closed shape raises the site's loud error.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.agents.claude_client import LlmClientError
from tests.agents.fakes.fake_agent_sdk import (
    FakeQuery,
    converged_text_stream,
    max_turns_one_stream,
    structured_output_stream,
)

_HAIKU = "claude-haiku-4-5-20251001"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["intent"],
    "additionalProperties": False,
}

_CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
_VALID_JUNGIAN = "hero"
_VALID_RPG_ROLE = "tank"
_OUT_OF_ENUM = "starlord"
_FREEFORM = (
    "I was born in the slag-quarters under the foundry stacks. I protect what "
    "is mine, and I stand in front when it counts."
)


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture
def fresh_ledger():
    from sidequest.agents import cost_safety

    cost_safety.ledger().reset_for_tests()
    yield cost_safety.ledger()
    cost_safety.ledger().reset_for_tests()


def _patch_query(monkeypatch: pytest.MonkeyPatch, stream: list[Any]) -> FakeQuery:
    from sidequest.agents import llm_factory

    fake = FakeQuery(stream)
    monkeypatch.setattr(llm_factory, "query", fake, raising=False)
    return fake


async def _drive_emit_tool(adapter: Any, *, tool_schema: dict[str, Any] | None = None) -> Any:
    return await adapter.emit_tool(
        system="ROUTER-SYS",
        user="attack the bandit captain",
        tool_name="emit_dispatch_package",
        tool_description="Decompose the action into a DispatchPackage.",
        tool_schema=tool_schema if tool_schema is not None else _TOOL_SCHEMA,
    )


def _load_heavy_metal_axes():
    from sidequest.genre.loader import GenreLoader

    if not (_CONTENT_ROOT / "heavy_metal").is_dir():
        pytest.skip("heavy_metal content not found")
    pack = GenreLoader(search_paths=[_CONTENT_ROOT]).load("heavy_metal")
    assert pack.base_archetypes is not None and pack.archetype_constraints is not None
    return pack.base_archetypes, pack.archetype_constraints


# ===========================================================================
# Forced-extraction contract — structured_output dict-or-raise
# ===========================================================================


async def test_intent_router_emit_tool_returns_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"intent": "attack", "target": "bandit captain", "confidence": 0.95}
    _patch_query(monkeypatch, structured_output_stream(payload))
    from sidequest.agents import llm_factory

    adapter = llm_factory.build_intent_router_llm(session_id=None)
    out = await _drive_emit_tool(adapter)
    assert out == payload, (
        "the router must return ResultMessage.structured_output verbatim where "
        f"the forced tool's .input went; got {out!r}"
    )


async def test_intent_router_emit_tool_raises_on_none_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``structured_output=None`` must raise ``IntentRouterEmptyResponse`` —
    the loud-raise contract is preserved across the transport (§6.4.2)."""
    from sidequest.agents.llm_factory import IntentRouterEmptyResponse

    _patch_query(monkeypatch, structured_output_stream(None))
    from sidequest.agents import llm_factory

    adapter = llm_factory.build_intent_router_llm(session_id=None)
    with pytest.raises(IntentRouterEmptyResponse):
        await _drive_emit_tool(adapter)


async def test_unseeded_classifier_emit_tool_returns_objective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"has_objective": True, "objective": "Find the missing caravan"}
    _patch_query(monkeypatch, structured_output_stream(payload))
    from sidequest.agents import llm_factory

    adapter = llm_factory.build_unseeded_objective_classifier_llm(session_id=None)
    out = await _drive_emit_tool(adapter)
    assert out == payload


async def test_unseeded_classifier_raises_on_none_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_query(monkeypatch, structured_output_stream(None))
    from sidequest.agents import llm_factory

    adapter = llm_factory.build_unseeded_objective_classifier_llm(session_id=None)
    with pytest.raises(LlmClientError):
        await _drive_emit_tool(adapter)


async def test_archetype_inference_returns_enum_validated_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, constraints = _load_heavy_metal_axes()
    _patch_query(
        monkeypatch,
        structured_output_stream(
            {"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE}
        ),
    )
    from sidequest.agents.llm_factory import infer_archetype_from_freeform

    result = await infer_archetype_from_freeform(
        freeform_text=_FREEFORM,
        base=base,
        constraints=constraints,
        existing_hints={"jungian_hint": None, "rpg_role_hint": None},
        session_id="119-3-archetype",
    )
    assert result == {"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE}, (
        "archetype inference must read structured_output and return the "
        f"enum-validated axes; got {result!r}"
    )


async def test_archetype_inference_none_on_out_of_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-enum invalidates the WHOLE inference → ``None`` (the existing
    No-Silent-Fallbacks reject), unchanged by the transport."""
    base, constraints = _load_heavy_metal_axes()
    _patch_query(
        monkeypatch,
        structured_output_stream({"jungian_hint": _OUT_OF_ENUM, "rpg_role_hint": _VALID_RPG_ROLE}),
    )
    from sidequest.agents.llm_factory import infer_archetype_from_freeform

    result = await infer_archetype_from_freeform(
        freeform_text=_FREEFORM,
        base=base,
        constraints=constraints,
        existing_hints={"jungian_hint": None, "rpg_role_hint": None},
        session_id="119-3-archetype",
    )
    assert result is None, f"out-of-enum must reject the whole inference (None); got {result!r}"


# ===========================================================================
# Aside — plain no-tools completion
# ===========================================================================


async def test_aside_complete_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_AsideLlm.complete`` ports to a plain no-tools ``max_turns=2``
    ``query()`` returning the assistant text (§6.4.2)."""
    text = "Your pack holds rope, a lantern, and three days of rations."
    _patch_query(monkeypatch, converged_text_stream(text=text))
    from sidequest.agents import llm_factory

    adapter = llm_factory.build_aside_llm(session_id=None)
    out = await adapter.complete(system="ASIDE-SYS", user="how big is my pack?")
    assert out == text, f"aside must return the converged completion text; got {out!r}"


# ===========================================================================
# max_turns=2 + output_format canonical surface (the +1 guardrail)
# ===========================================================================


@pytest.mark.parametrize("site", ["router", "classifier"])
async def test_forced_extraction_sites_use_output_format_at_max_turns_two(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """The forced-extraction sites must build
    ``ClaudeAgentOptions(max_turns=2, allowed_tools=[],
    output_format={'type':'json_schema','schema': <tool_schema>})`` — the
    VERIFIED Path A surface. ``max_turns`` MUST be 2, never 1 (fails closed)."""
    from sidequest.agents import llm_factory

    fake = _patch_query(monkeypatch, structured_output_stream({"intent": "attack"}))
    builders = {
        "router": llm_factory.build_intent_router_llm,
        "classifier": llm_factory.build_unseeded_objective_classifier_llm,
    }
    adapter = builders[site](session_id=None)
    await _drive_emit_tool(adapter, tool_schema=_TOOL_SCHEMA)

    opts = fake.last_options
    assert getattr(opts, "max_turns", None) == 2, (
        "max_turns MUST be 2 — the SDK spends an internal finalize turn, so "
        f"max_turns=1 fails closed with error_max_turns (OQ-16); got {getattr(opts, 'max_turns', None)!r}"
    )
    assert getattr(opts, "allowed_tools", "MISSING") == [], (
        "the structured-output path advertises NO tools (allowed_tools=[])"
    )
    output_format = getattr(opts, "output_format", None)
    assert isinstance(output_format, dict), (
        f"output_format must be set (Path A structured output); got {output_format!r}"
    )
    assert output_format.get("type") == "json_schema"
    assert output_format.get("schema") == _TOOL_SCHEMA, (
        "the tool schema must round-trip into output_format.schema verbatim — "
        f"got {output_format.get('schema')!r}"
    )


async def test_aside_uses_no_tools_at_max_turns_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """The aside is a plain completion: no tools, no output_format, max_turns=2."""
    from sidequest.agents import llm_factory

    fake = _patch_query(monkeypatch, converged_text_stream(text="ok"))
    adapter = llm_factory.build_aside_llm(session_id=None)
    await adapter.complete(system="S", user="U")

    opts = fake.last_options
    assert getattr(opts, "max_turns", None) == 2, "aside also needs the +1 (max_turns=2)"
    assert getattr(opts, "allowed_tools", "MISSING") == [], "aside advertises no tools"
    assert not getattr(opts, "output_format", None), (
        "the aside is a plain text completion — no output_format"
    )


# ===========================================================================
# Regression: structured-extraction calls disable extended thinking so they
# fit the mandatory max_turns=2 floor (the error_max_turns blocker, 2026-06-16).
#
# output_format is a synthetic ``StructuredOutput`` tool round-trip that already
# consumes both mt=2 turns; the claude CLI defaults thinking ON, so a long think
# pushes the finalize past 2 turns and the call fails error_max_turns
# intermittently — which took the intent-router spine dark on the 119-3 port.
# ===========================================================================


@pytest.mark.parametrize("site", ["router", "classifier"])
async def test_forced_extraction_sites_disable_thinking(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """Every output_format site must build options with thinking disabled —
    a structured classifier cannot afford a thinking turn at the mt=2 floor."""
    from sidequest.agents import llm_factory

    fake = _patch_query(monkeypatch, structured_output_stream({"intent": "attack"}))
    builders = {
        "router": llm_factory.build_intent_router_llm,
        "classifier": llm_factory.build_unseeded_objective_classifier_llm,
    }
    adapter = builders[site](session_id=None)
    await _drive_emit_tool(adapter, tool_schema=_TOOL_SCHEMA)

    opts = fake.last_options
    assert getattr(opts, "thinking", None) == {"type": "disabled"}, (
        "output_format calls MUST disable thinking — the synthetic StructuredOutput "
        "tool round-trip already costs both mt=2 turns, so a thinking pass blows the "
        f"budget (error_max_turns); got thinking={getattr(opts, 'thinking', None)!r}"
    )


async def test_aside_leaves_thinking_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-structured callers (the aside, the narrator tool-loop) are untouched:
    no output_format ⇒ thinking stays None (the CLI default applies)."""
    from sidequest.agents import llm_factory

    fake = _patch_query(monkeypatch, converged_text_stream(text="ok"))
    adapter = llm_factory.build_aside_llm(session_id=None)
    await adapter.complete(system="S", user="U")

    assert getattr(fake.last_options, "thinking", "MISSING") is None, (
        "a plain completion (no output_format) must NOT be forced to disabled-thinking — "
        "the auto-disable is scoped to structured extraction only"
    )


def test_build_options_thinking_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct unit on the builder: output_format ⇒ thinking disabled by default;
    no output_format ⇒ thinking None; an explicit thinking value always wins."""
    from sidequest.agents.anthropic_sdk_client import build_agent_sdk_options

    of = {"type": "json_schema", "schema": {"type": "object"}}

    structured = build_agent_sdk_options(
        model="m", system_prompt="s", max_turns=2, allowed_tools=[], output_format=of
    )
    assert structured.thinking == {"type": "disabled"}

    plain = build_agent_sdk_options(model="m", system_prompt="s", max_turns=2)
    assert plain.thinking is None

    override = build_agent_sdk_options(
        model="m",
        system_prompt="s",
        max_turns=2,
        output_format=of,
        thinking={"type": "enabled", "budget_tokens": 1024},
    )
    assert override.thinking == {"type": "enabled", "budget_tokens": 1024}


@pytest.mark.parametrize("site", ["router", "classifier"])
async def test_max_turns_one_fail_closed_shape_raises(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """The empirical ``max_turns=1`` shape (``error_max_turns`` /
    ``structured_output=None``) must raise the site's loud error — the guard
    against a stale ``max_turns=1`` regression silently returning ``None``."""
    from sidequest.agents import llm_factory

    _patch_query(monkeypatch, max_turns_one_stream())
    builders = {
        "router": llm_factory.build_intent_router_llm,
        "classifier": llm_factory.build_unseeded_objective_classifier_llm,
    }
    adapter = builders[site](session_id=None)
    with pytest.raises(LlmClientError):
        await _drive_emit_tool(adapter)


# ===========================================================================
# Per-site OTEL + caller tag + session cost-safety (the [COST-1] axis)
# ===========================================================================


class _SpyLedger:
    """Records ``check_ceiling`` / ``record_call`` so the cost-safety wiring is
    asserted independent of the usage-dict adaptation (OQ-2)."""

    def __init__(self) -> None:
        self.checked: list[str] = []
        self.recorded: list[SimpleNamespace] = []

    def check_ceiling(self, session_id: str, *, ceiling_usd: float) -> None:
        self.checked.append(session_id)

    def record_call(
        self,
        *,
        session_id: str,
        caller: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        ceiling_usd: float,
    ) -> None:
        self.recorded.append(SimpleNamespace(session_id=session_id, caller=caller, model=model))


def _llm_request_spans(exporter: Any, *, caller: str) -> list[Any]:
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name == "llm.request" and (s.attributes or {}).get("llm.caller") == caller
    ]


@pytest.mark.parametrize(
    ("site", "caller"),
    [
        ("router", "intent_router"),
        ("aside", "aside"),
        ("classifier", "unseeded_objective_classifier"),
    ],
)
async def test_haiku_site_emits_caller_tagged_span_and_records_cost(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any, site: str, caller: str
) -> None:
    """Each ported Haiku site must open an ``llm.request`` span carrying its
    ``llm.caller`` tag (the [COST-1] attribution axis that splits router from
    aside from classifier) and fire ``check_ceiling`` + ``record_call`` against
    the per-session ledger."""
    from sidequest.agents import cost_safety, llm_factory

    spy = _SpyLedger()
    monkeypatch.setattr(cost_safety, "ledger", lambda: spy)

    if site == "aside":
        _patch_query(monkeypatch, converged_text_stream(text="ok"))
        adapter = llm_factory.build_aside_llm(session_id="119-3-cost")
        await adapter.complete(system="S", user="U")
    else:
        _patch_query(monkeypatch, structured_output_stream({"intent": "attack"}))
        builders = {
            "router": llm_factory.build_intent_router_llm,
            "classifier": llm_factory.build_unseeded_objective_classifier_llm,
        }
        adapter = builders[site](session_id="119-3-cost")
        await _drive_emit_tool(adapter)

    spans = _llm_request_spans(otel_capture, caller=caller)
    assert spans, (
        f"the {site} site must open an llm.request span carrying "
        f"llm.caller={caller!r} — span-based cost attribution depends on it"
    )

    assert spy.checked == ["119-3-cost"], (
        "the pre-flight ceiling check must fire for a session-bearing call"
    )
    assert [r.caller for r in spy.recorded] == [caller], (
        f"the call must record to the session ledger under caller={caller!r} "
        f"(the [COST-1] attribution); recorded {[r.caller for r in spy.recorded]!r}"
    )
